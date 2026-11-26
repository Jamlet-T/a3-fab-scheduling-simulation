"""减少重复检查时，仍须在真实交接事件边界发现物料丢失/重复。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes
from fabcosim.ports.model import Lot, Port
from fabcosim.transport.fleet import FleetTransport, Gate


@pytest.mark.parametrize("cargo_count", [0, 2])
def test_material_audit_rejects_missing_or_duplicate_cargo(cargo_count):
    lot = Lot("lot", "oht")
    vehicles = {str(i): {"cargo": lot} for i in range(cargo_count)}
    with pytest.raises(RuntimeError, match="物料守恒错误"):
        full._check_material({lot.id: lot}, {}, vehicles)


def test_material_audit_rejects_port_and_vehicle_double_ownership():
    lot = Lot("lot", "input.port")
    port = Port("input.port", lot)
    with pytest.raises(RuntimeError, match="物料守恒错误"):
        full._check_material({lot.id: lot}, {"input": port}, {"oht": {"cargo": lot}})


def test_active_region_grants_keep_historical_order_after_empty_queue():
    engine = FleetTransport.__new__(FleetTransport)
    engine.ids = ["a", "b"]
    engine.config = {"traffic": {"lookahead_m": 8}}
    engine.owners = {}
    engine.waiting = {vid: None for vid in engine.ids}
    gates = {vid: Gate(vid, 10, 15, "edge", 8, 8, "queued", True) for vid in engine.ids}
    engine.plans = {vid: {"gates": [gates[vid]]} for vid in engine.ids}
    # b区域先出现；中间区域已为空。重新有车辆等待的a区域不能抢到b之前。
    engine.queues = {"b": [("b", gates["b"])], "empty": [], "a": [("a", gates["a"])]}
    engine.queue_order = {"b": 0, "empty": 1, "a": 2}
    engine.queued_zones = {"a", "b"}
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(
            getDistance=lambda _: 0,
            getSpeed=lambda _: 0,
            getLeader=lambda *_: None,
            replaceStop=lambda *_: None,
        )
    )
    granted = []
    engine.record = lambda kind, **fields: granted.append(fields["zone"])
    engine.control(0)
    assert granted == ["b", "a"]
    assert engine.queued_zones == set()
    assert all(g.state == "granted" for g in gates.values())


def test_event_audit_matches_every_step_through_actual_delivery(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (root / settings["network"]["workbook"]).exists():
        pytest.skip("缺少原始路网")
    resolved = compile_full_routes(settings, root)
    op = copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][369])
    op.update(
        source_step=1,
        processing_s=1,
        interval_s=0,
        sample_probability=1,
        rework_probability=0,
        dedication_step=0,
        cqt_step=0,
    )
    resolved["routes"]["Route_Product_1"]["operations"] = [op]
    resolved["lots"] = resolved["lots"][:1]
    resolved["observation"] = {"warmup_s": 0, "end_s": 6000}
    resolved["max_time_s"] = 6000
    resolved["replay"] = {"enabled": False}
    results, audit_calls = {}, {}
    original = full._check_material
    for mode in ("every_step", "events", "stream"):
        calls = []

        def check(*args):
            calls.append(1)
            original(*args)

        resolved["performance"] = {
            "material_audit": "events" if mode == "stream" else mode,
            "stream_events": mode == "stream",
        }
        monkeypatch.setattr(full, "compile_full_routes", lambda *args: copy.deepcopy(resolved))
        monkeypatch.setattr(full, "_check_material", check)
        output = tmp_path / mode
        summary = full.run_full(resolved, output)
        assert summary["completed_lots"] == 1
        results[mode] = (summary, json.loads((output / "events.json").read_text("utf-8")))
        audit_calls[mode] = len(calls)
    assert results["events"] == results["every_step"]
    assert results["stream"] == results["events"]
    assert audit_calls["events"] < audit_calls["every_step"] / 10


def test_streamed_events_are_closed_after_material_failure(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少原始路网")
    config["lots"] = config["lots"][:1]
    config["production"]["batch_policy"] = "queue_batch"
    config["replay"] = {"enabled": False}
    config["performance"] = {"stream_events": True, "route_event_geometry": False}

    def fail(*args):
        raise RuntimeError("injected material failure")

    monkeypatch.setattr(full, "_check_material", fail)
    output = tmp_path / "failed-stream"
    with pytest.raises(RuntimeError, match="injected material failure"):
        full.run_full(config, output)
    assert json.loads((output / "summary.json").read_text("utf-8"))["status"] == "failed"
    events = json.loads((output / "events.json").read_text("utf-8"))
    assert events and any(e["kind"] == "lot.released" for e in events)
