"""纯生产目的地负载信号：已授予与等待准入都计入，同一缓冲的语义不变。

回归目标：``destination`` 在纯生产下曾用 ``len(requests[target])`` 作负载，
它只看到已到站、尚未派工的job，看不到已在 ``Resource.users`` 的lot，也看不到
在 ``Resource.queue`` 里等准入的lot；同刻投片的lot因此全挤向按名字排序的第一台。
这里用现有 ``fixture_config`` + mock 路线跑最小场景，不启动真实日历。

旧信号下每台缓冲容量1、4个lot同刻选择时全部落在B0，64s窗口只完成2个；
新信号按``users + queue``分散为2/2，64s内4个全部完成。
"""

import copy
import json

import simpy
from test_production_only import fixture_config

from fabcosim.cosim import full
from fabcosim.production.transfer import ProductionTransferBuffers


def destination_config(first, second, *, count=4, capacity=1, end=64):
    """first/second = (工具组, 资产kind, 设备名, 秒数, virtual)。"""
    config = fixture_config(count=count, capacity=capacity)
    for op, (group, _, _, seconds, virtual) in zip(
        config["routes"]["R"]["operations"], (first, second)
    ):
        op.update(tool_group=group, processing_s=seconds, virtual=virtual, unit="Lot")
    assets = {
        "C": dict(kind="commit", tool_group="COMMIT", input_port="C.in", output_port="C.out"),
        "E": dict(kind="complete", tool_group="COMPLETE", input_port="E.in", output_port="E.out"),
    }
    candidates = {"COMMIT": ["C"], "COMPLETE": ["E"]}
    for group, kind, names, _, _ in (first, second):
        for name in names:
            assets[name] = dict(
                kind=kind, tool_group=group, input_port=name + ".in", output_port=name + ".out"
            )
        candidates[group] = list(names)
        config["toolgroups"][group] = dict(CASCADINGTOOL=None, LOADINGTIME=0, UNLOADINGTIME=0)
    config.update(
        assets=assets,
        candidates=candidates,
        lots=[dict(id=f"L{i}", route="R", wafer_count=25, release_s=0) for i in range(count)],
        max_time_s=end,
        observation=dict(warmup_s=0, end_s=end),
    )
    return config


def execute(monkeypatch, tmp_path, config):
    def forbidden(*args, **kwargs):
        raise AssertionError("纯生产不得调用运输/端口/路由")

    monkeypatch.setattr(full, "compile_full_routes", lambda prepared, root: copy.deepcopy(config))
    for name in ("DelayTransport", "FleetTransport", "Port", "route_between", "route_shapes"):
        monkeypatch.setattr(full, name, forbidden)
    summary = full.run_full(config, tmp_path / "run")
    events = json.loads((tmp_path / "run/events.json").read_text("utf-8"))
    return summary, events


def selected(events, step=1):
    return [e for e in events if e["kind"] == "destination.selected" and e["source_step"] == step]


def peak_buffer_occupancy(events):
    """从事件重建每个输入位的占用峰值：转运开始获准入，离开该缓冲queue时释放。"""
    holding, arrived, spans, last = {}, {}, [], 0.0
    for event in events:
        lot, kind, time = event.get("lot_id"), event["kind"], event["time"]
        last = max(last, time)
        if lot is None:
            continue
        if kind == "production.transfer_start":
            holding[lot] = (event["equipment"], time)
            arrived.pop(lot, None)
        elif kind == "production.transfer_end" and lot in holding:
            arrived[lot] = holding[lot][0]
        elif lot in holding and lot in arrived and event.get("location") != arrived[lot] + ".queue":
            target, start = holding.pop(lot)
            spans.append((target, start, time))
            arrived.pop(lot)
    for target, start in holding.values():
        spans.append((target, start, last))
    peaks = {}
    for target in {span[0] for span in spans}:
        level = peak = 0
        points = [(start, 1) for t, start, _ in spans if t == target]
        points += [(end, -1) for t, _, end in spans if t == target]
        for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
            level += delta
            peak = max(peak, level)
        peaks[target] = peak
    return peaks


def test_same_instant_lots_spread_over_equally_loaded_buffers(monkeypatch, tmp_path):
    # 两个虚拟缓冲各1位，4个lot同刻投片：旧信号全排B0，64s只完成2个；新信号2/2分流。
    config = destination_config(
        ("BUFFER", "buffer", ("B0", "B1"), 20, True), ("TOOL", "tool", ("P0",), 5, False)
    )
    summary, events = execute(monkeypatch, tmp_path, config)
    assert summary["completed_lots"] == 4
    assert sorted(e["equipment"] for e in selected(events)) == ["B0", "B0", "B1", "B1"]
    # 4个lot同刻申请准入，只有拿到位置的两个lot能开始转运，另两个在工艺等待。
    assert sum(e["kind"] == "production.transfer_requested" and e["time"] == 0 for e in events) == 4
    assert sum(e["kind"] == "production.transfer_start" and e["time"] == 0 for e in events) == 2
    peaks = peak_buffer_occupancy(events)
    assert peaks["B0"] == peaks["B1"] == peaks["P0"] == 1
    assert max(peaks.values()) <= config["production"]["input_buffer_lots"]
    ends = {
        (e["lot_id"], e["source_step"]): e["time"]
        for e in events
        if e["kind"] == "production.transfer_end"
    }
    for event in events:
        if event["kind"] in ("processing.start", "delay.start"):
            assert event["time"] >= ends[event["lot_id"], event["source_step"]]


def test_waiting_admission_counts_when_choosing_among_tools(monkeypatch, tmp_path):
    # 3台工具各1位、6个lot同刻：只看users时第4、5、6个会继续挤T0；
    # 计入queue后每个候选的已承诺负载为2/2/2，第6个lot必须去T2。
    config = destination_config(
        ("TOOL", "tool", ("T0", "T1", "T2"), 5, False),
        ("TOOL", "tool", ("T0", "T1", "T2"), 5, False),
        count=6,
        capacity=1,
        end=30,
    )
    summary, events = execute(monkeypatch, tmp_path, config)
    first = selected(events)
    assert sorted(e["equipment"] for e in first) == ["T0", "T0", "T1", "T1", "T2", "T2"]
    assert sum(e["kind"] == "production.transfer_start" and e["time"] == 0 for e in events) == 3
    peaks = peak_buffer_occupancy(events)
    assert peaks["T0"] == peaks["T1"] == peaks["T2"] == 1
    assert max(peaks.values()) <= config["production"]["input_buffer_lots"]
    assert summary["completed_lots"] == 6


def test_committed_lots_counts_granted_and_waiting_once():
    # 负载信号就是Resource自身：users里每个lot算一次，queue里每个等待lot算一次。
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["T"], capacity=1)
    granted = []

    def acquire(lot_id):
        yield from buffers.reserve(lot_id, "T")
        granted.append(lot_id)

    env.process(acquire("A"))
    env.run()
    assert granted == ["A"] and buffers.committed_lots("T") == 1
    env.process(acquire("B"))
    env.process(acquire("C"))
    env.run()
    # 已授予的A + 等待准入的B、C各计一次，不叠加同lot的job队列。
    assert buffers.committed_lots("T") == 3
    assert set(buffers.tokens) == {"A"} and buffers.verify()
    buffers.release("A")
    # 释放后B立即获准（users），C仍在queue；获准进程要等SimPy继续推进才恢复。
    assert buffers.committed_lots("T") == 2
    env.run()
    assert granted == ["A", "B"] and set(buffers.tokens) == {"B"} and buffers.verify()
    buffers.release("B")
    env.run()
    assert granted == ["A", "B", "C"] and buffers.committed_lots("T") == 1
    assert buffers.verify()
