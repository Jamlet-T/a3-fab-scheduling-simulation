"""多车规则的关键端到端行为与真实重规划出口回归。"""

import json
import math
from pathlib import Path

import pytest

from fabcosim.cosim.multi import run_multi


@pytest.fixture(scope="module", params=["multi_merge", "smat_multi"])
def multi_run(request, tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / f"configs/{request.param}.json").read_text("utf-8"))
    if "workbook" in config["network"] and not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少SMAT2022原始Excel")
    output = tmp_path_factory.mktemp(request.param) / "run"
    summary = run_multi(config, output)
    return (
        request.param,
        summary,
        json.loads((output / "events.json").read_text("utf-8")),
        json.loads((output / "frames.json").read_text("utf-8")),
    )


def test_all_lots_delivered_before_target_processing(multi_run):
    _, summary, events, frames = multi_run
    assert summary["completed_tasks"] == 4
    assert summary["completed_operations"] == 8
    assert summary["vehicle_count"] == 3
    assert summary["teleports"] == summary["collisions"] == []
    for task_id in [f"task{i}" for i in range(4)]:
        task_events = [e for e in events if e.get("task_id") == task_id]
        assert sum(e["kind"] == "task.assigned" for e in task_events) == 1
        delivered = next(e["time"] for e in task_events if e["kind"] == "transport.delivered")
        processing = [e["time"] for e in task_events if e["kind"] == "processing.start"]
        assert len(processing) == 2 and processing[1] >= delivered
    assert all(lot["location"] == "completed" for lot in frames[-1]["lots"])
    assert any(e["kind"] == "task.waiting_destination" for e in events)


def test_zone_and_port_reservations_are_exclusive(multi_run):
    _, summary, events, _ = multi_run
    zones, ports, busy = {}, {}, {}
    for event in events:
        kind, vid = event["kind"], event.get("vehicle_id")
        if kind == "zone.granted":
            assert event["zone"] not in zones
            zones[event["zone"]] = vid
        elif kind == "zone.released":
            assert zones.pop(event["zone"]) == vid
            assert event["distance_m"] + 1e-8 >= event["exit_distance_m"]
        elif kind == "zone.reservation_cancelled":
            assert zones.pop(event["zone"]) == vid
        elif kind == "task.assigned":
            assert event["port"] not in ports and vid not in busy
            ports[event["port"]] = event["task_id"]
            busy[vid] = event["task_id"]
        elif kind == "port.released":
            assert ports.pop(event["port"]) == event["task_id"]
        elif kind == "transport.delivered":
            assert busy.pop(vid) == event["task_id"]
    assert ports == busy == {}
    assert summary["zone_grants"] > 0
    assert summary["zone_wait_vehicle_s"] > 0


def test_vehicle_motion_and_same_lane_gap(multi_run):
    name, _, events, frames = multi_run
    minimum_gap = float("inf")
    for previous, frame in zip(frames, frames[1:]):
        assert len(frame["vehicles"]) == 3
        for a, b in zip(previous["vehicles"], frame["vehicles"]):
            assert a["id"] == b["id"]
            assert math.hypot(b["x"] - a["x"], b["y"] - a["y"]) <= 0.150001
        for a in frame["vehicles"]:
            for b in frame["vehicles"]:
                if a["edge"] == b["edge"] and a["position_m"] > b["position_m"]:
                    minimum_gap = min(minimum_gap, a["position_m"] - b["position_m"] - 0.784)
    assert minimum_gap >= 0.125 - 1e-6
    if name == "smat_multi":
        # 曾在闲车重规划经过另一RESET出口时按旧出口提前释放，真实场景会越界失败。
        assert any(
            e["kind"] == "zone.exit_replanned" and e["new_exit_m"] > e["old_exit_m"] + 0.1
            for e in events
        )
