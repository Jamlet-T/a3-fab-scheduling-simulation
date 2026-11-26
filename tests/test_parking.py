"""单向轨道停车目标不能排在已停车前车前方。"""

from types import SimpleNamespace

import pytest
import sumolib

from fabcosim.transport.fleet import FleetTransport, Gate, build_merge_network


@pytest.mark.parametrize("speed", [0, 1.5])
@pytest.mark.parametrize("front_offset", [0, 0.09])
def test_parking_goal_stays_behind_reserved_front_vehicle(tmp_path, speed, front_offset):
    config = {
        "oht": {"max_speed_m_s": 1.5, "length_m": 0.784, "min_gap_m": 0.125},
        "traffic": {"lookahead_m": 8},
    }
    engine = FleetTransport.__new__(FleetTransport)
    engine.config = config
    engine.net = sumolib.net.readNet(str(build_merge_network(config, tmp_path)))
    engine.ids = ["front", "rear"]
    engine.parking = {"front": {"edge": "e0", "position_m": 18.5}}
    engine.owners = {}
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(getDistance=lambda _: 2, getSpeed=lambda _: speed)
    )
    engine.position = lambda v: {
        "edge": "e0",
        "position_m": 18.5 + front_offset if v == "front" else 2,
    }
    engine.cycle = lambda _: (["e0", "e1", "e2", "e3", "e4", "e0"], 120)
    engine.make_gates = lambda *args: []
    goals = []
    engine.plan = lambda vid, goal: goals.append(goal)
    engine.record = lambda *args, **kwargs: None
    goal = engine.park("rear")
    assert goal["edge"] == "e0"
    assert 2 < goal["position_m"] <= 18.5 - 0.784 - 0.125
    assert goals == [goal]


def test_stopped_follower_can_wait_here_after_merge(tmp_path):
    """汇入后只剩安全间距，不能要求后车倒退以取得额外停车余量。"""
    config = {
        "oht": {"max_speed_m_s": 1.5, "length_m": 0.784, "min_gap_m": 0.125},
        "traffic": {"lookahead_m": 8},
    }
    engine = FleetTransport.__new__(FleetTransport)
    engine.config = config
    engine.net = sumolib.net.readNet(str(build_merge_network(config, tmp_path)))
    engine.ids = ["front", "rear"]
    engine.parking = {"front": {"edge": "e0", "position_m": 18.5}}
    engine.owners = {}
    position = 18.5 - 0.784 - 0.125 - 0.001
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(getDistance=lambda _: position, getSpeed=lambda _: 0)
    )
    engine.position = lambda _: {"edge": "e0", "position_m": position}
    engine.cycle = lambda _: (["e0", "e1", "e2", "e3", "e4", "e0"], 120)
    engine.make_gates = lambda *args: []
    engine.plan = lambda *args: None
    engine.record = lambda *args, **kwargs: None
    assert engine.park("rear") == {"edge": "e0", "position_m": position}


def test_native_same_position_stop_preserves_position(tmp_path):
    config = {
        "step_s": 0.1,
        "max_time_s": 100,
        "oht": {
            "max_speed_m_s": 1.5,
            "length_m": 0.784,
            "min_gap_m": 0.125,
            "accel_m_s2": 2,
            "decel_m_s2": 3,
        },
        "traffic": {"lookahead_m": 8, "stop_margin_m": 0.2, "junction_clearance_m": 2},
        "vehicles": [{"id": "rear", "edge": "e0", "position_m": 2}],
        "ports": {},
    }
    engine = FleetTransport(config, tmp_path, lambda *args, **kwargs: None)
    try:
        engine.api.simulationStep(0.1)
        position = engine.position("rear")
        distance = engine.api.vehicle.getDistance("rear")
        engine.plan("rear", position)
        engine.step(0.2)
        assert engine.arrived("rear", position)
        assert engine.position("rear") == position
        assert engine.api.vehicle.getDistance("rear") == distance
        assert engine.collisions == engine.teleports == []
    finally:
        engine.close()


def test_vehicle_ahead_can_pass_followers_future_parking_goal(tmp_path):
    config = {
        "oht": {"max_speed_m_s": 1.5, "length_m": 0.784, "min_gap_m": 0.125},
        "traffic": {"lookahead_m": 8},
    }
    engine = FleetTransport.__new__(FleetTransport)
    engine.config = config
    engine.net = sumolib.net.readNet(str(build_merge_network(config, tmp_path)))
    engine.ids = ["front", "rear"]
    engine.parking = {"rear": {"edge": "e0", "position_m": 8.66}}
    engine.owners = {}
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(getDistance=lambda _: 6.95, getSpeed=lambda _: 1.5)
    )
    engine.position = lambda vid: (
        {"edge": "e0", "position_m": 6.95} if vid == "front" else {"edge": "e4", "position_m": 18}
    )
    engine.cycle = lambda _: (["e0", "e1", "e2", "e3", "e4", "e0"], 120)
    engine.make_gates = lambda *args: []
    engine.plan = lambda *args: None
    engine.record = lambda *args, **kwargs: None
    goal = engine.park("front")
    assert goal["edge"] == "e0" and goal["position_m"] > 8.66


def test_parking_leader_yields_when_follower_cannot_clear_zone(tmp_path):
    config = {
        "oht": {"max_speed_m_s": 1.5, "length_m": 0.784, "min_gap_m": 0.125},
        "traffic": {"lookahead_m": 8},
    }
    engine = FleetTransport.__new__(FleetTransport)
    engine.config = config
    engine.net = sumolib.net.readNet(str(build_merge_network(config, tmp_path)))
    engine.ids = ["front", "rear"]
    engine.parking = {"front": {"edge": "e0", "position_m": 11.5}}
    held = Gate("zone", 0, 11, "e0", 0, 0, state="granted")
    engine.owners = {"zone": ("rear", held)}
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(getDistance=lambda _: 2, getSpeed=lambda _: 0)
    )
    engine.position = lambda v: {"edge": "e0", "position_m": 11.5 if v == "front" else 2}
    engine.cycle = lambda _: (["e0", "e1", "e2", "e3", "e4", "e0"], 120)
    engine.make_gates = lambda *args: []
    replans = []
    engine.plan = lambda v, goal: replans.append((v, goal))
    engine.record = lambda *args, **kwargs: None
    assert engine.park("rear") is None
    assert replans == [("front", None)]
    assert engine.owners["zone"] == ("rear", held)
