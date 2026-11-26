"""关键联合仿真行为：实车到达、时间顺序、物料守恒以及运输参数影响。"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim.demo import load_config, run_demo


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    config = load_config(Path(__file__).resolve().parents[1] / "configs/two_tools.json")
    root = tmp_path_factory.mktemp("demo")
    cases = {}
    for name in ("base", "slow", "non_grid"):
        cfg = copy.deepcopy(config)
        if name == "slow":
            cfg["oht"]["max_speed_m_s"] = 0.75
        if name == "non_grid":
            cfg["processing_s"]["A"] = 5.05
            cfg["pickup_s"] = 1.05
            cfg["dropoff_s"] = 1.05
        output = root / name
        summary = run_demo(cfg, output)
        cases[name] = (
            summary,
            json.loads((output / "events.json").read_text("utf-8")),
            json.loads((output / "trajectory.json").read_text("utf-8")),
        )
    return cases


def test_handoff_order_and_material_conservation(runs):
    summary, events, trajectory = runs["base"]
    times = {e["kind"]: e["time"] for e in events}
    assert times["A.processing_end"] <= times["transport.pickup_start"]
    assert times["transport.arrived_A"] <= times["transport.pickup_start"]
    assert times["transport.pickup_end"] <= times["transport.departed_A"]
    assert times["transport.departed_A"] < times["transport.arrived_B"]
    assert times["transport.arrived_B"] < times["transport.delivered"]
    assert times["transport.delivered"] <= times["B.processing_start"]
    assert times["B.processing_start"] < times["lot.completed"]
    assert all(a["time"] <= b["time"] for a, b in zip(events, events[1:]))
    assert all(row["holders"] == 1 for row in trajectory)
    assert [e["location"] for e in events if e["kind"] == "transport.pickup_end"] == ["oht0"]
    assert summary["completed_operations"] == 2
    assert summary["final_location"] == "completed"
    assert summary["vehicle_count_at_end"] == 1
    assert summary["teleports"] == summary["collisions"] == []
    assert summary["max_observed_speed_m_s"] <= 1.5 + 1e-8
    # 加工前后的真实轨迹覆盖两个端口，并保持连续运动。
    assert {row["edge"] for row in trajectory} >= {"e0", "e1"}
    assert all(
        ((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5 <= 0.151
        for a, b in zip(trajectory, trajectory[1:])
    )


def test_slower_transport_delays_production(runs):
    base, slow = runs["base"][0], runs["slow"][0]
    assert slow["loaded_travel_s"] > base["loaded_travel_s"]
    assert slow["completion_s"] > base["completion_s"]


def test_non_grid_events_complete_without_clock_drift(runs):
    summary, events, _ = runs["non_grid"]
    times = {e["kind"]: e["time"] for e in events}
    assert times["A.processing_end"] == pytest.approx(5.1)
    assert times["transport.pickup_end"] - times["transport.pickup_start"] == pytest.approx(1.1)
    assert times["transport.delivered"] - times["transport.dropoff_start"] == pytest.approx(1.1)
    assert summary["status"] == "completed"
