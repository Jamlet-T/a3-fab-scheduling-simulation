"""聚合运输档：一趟运输折算成一个到达时刻，不逐车积分。

覆盖时长公式、到达判定、待命语义与"不给位置"的约定（后者使统计层不再报同轨间距，
而不是报出假的0间距）。
"""

import json
from pathlib import Path

import pytest

from fabcosim.transport.delay import POSITIONLESS, DelayTransport


class FakeNet:
    """固定里程替身：不读真实路网，只按给定表返回距离。"""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def route(self, origin, goal):
        self.calls.append((origin["edge"], goal["edge"]))
        return self.table[(origin["edge"], goal["edge"])]


def engine_for(table, speed=1.0, vehicles=("v0",)):
    """构造只带接口所需最小状态的聚合档；绕过建网，直接注入里程表。"""
    engine = DelayTransport.__new__(DelayTransport)
    engine.config = {}
    engine.speed_m_s = speed
    engine.master_time = 0.0
    engine.time_offset = 0.0
    engine.parking, engine._parked = {}, {}
    engine.collisions, engine.teleports = [], []
    engine.geometry = {"lanes": [], "ports": {}}
    engine._routes = {}
    engine.ids = sorted(vehicles)
    engine.trips = {
        vid: {
            "location": {"edge": "start", "position_m": 0.0},
            "goal": None,
            "depart_s": 0.0,
            "arrive_s": 0.0,
            "travelled_m": 0.0,
            "leg_m": 0.0,
        }
        for vid in engine.ids
    }
    fake = FakeNet(table)
    engine.route_distance = lambda origin, goal: fake.route(origin, goal)
    engine._fake_net = fake
    return engine


GOAL = {"edge": "dest", "position_m": 5.0}


def test_travel_time_is_distance_over_speed():
    engine = engine_for({("start", "dest"): 100.0}, speed=2.0)
    engine.plan("v0", GOAL)
    assert engine.trips["v0"]["arrive_s"] == 50.0
    assert engine.speed("v0") == 2.0
    assert not engine.arrived("v0", GOAL)
    engine.step(49.999)
    assert not engine.arrived("v0", GOAL)
    engine.step(50.0)
    assert engine.arrived("v0", GOAL)
    assert engine.speed("v0") == 0.0


def test_only_the_planned_goal_counts_as_arrived():
    engine = engine_for({("start", "dest"): 10.0})
    engine.plan("v0", GOAL)
    engine.step(10.0)
    assert engine.arrived("v0", GOAL)
    assert not engine.arrived("v0", {"edge": "other", "position_m": 0.0})


def test_distance_is_measured_from_current_location_and_follows_arrival():
    engine = engine_for({("start", "dest"): 30.0, ("dest", "next"): 70.0})
    assert engine.distance("v0", GOAL) == 30.0
    engine.plan("v0", GOAL)
    # 未到达前仍在起点出发，里程不变。
    assert engine.distance("v0", GOAL) == 30.0
    engine.step(30.0)
    assert engine.distance("v0", {"edge": "next", "position_m": 0.0}) == 70.0


def test_plan_none_cancels_the_trip_and_next_arrival_tracks_transit():
    engine = engine_for({("start", "dest"): 40.0})
    assert engine.next_arrival() is None
    engine.plan("v0", GOAL)
    assert engine.next_arrival() == 40.0
    engine.plan("v0", None)
    assert engine.next_arrival() is None
    assert engine.speed("v0") == 0.0


def test_snapshot_reports_progress_rather_than_a_step_jump():
    """统计层按里程增量判断是否阻塞，一步一跳会被误判为全程阻塞。"""
    engine = engine_for({("start", "dest"): 100.0}, speed=10.0)
    engine.plan("v0", GOAL)
    engine.step(2.5)
    quarter = engine.snapshot("v0")["distance_m"]
    engine.step(5.0)
    half = engine.snapshot("v0")["distance_m"]
    engine.step(10.0)
    arrived = engine.snapshot("v0")["distance_m"]
    # 途中按匀速给里程，不是"出发记0、到达记满"，否则每步增量都是0会被判为阻塞。
    assert quarter == 25.0 and half == 50.0
    assert arrived == 100.0
    # 到达后不再增长，统计层不会看到里程回退。
    engine.step(20.0)
    assert engine.snapshot("v0")["distance_m"] == 100.0


def test_position_is_empty_so_the_metrics_layer_skips_lane_gaps():
    engine = engine_for({("start", "dest"): 1.0})
    assert engine.position("v0") == POSITIONLESS
    assert POSITIONLESS["edge"] == ""
    engine.plan("v0", GOAL)
    assert engine.snapshot("v0")["edge"] == ""


def test_parking_makes_the_vehicle_count_as_settled():
    """主循环的静止判定要求已停稳车辆arrived为真，聚合档没有停车点。"""
    engine = engine_for({("start", "dest"): 1.0})
    spot = engine.park("v0")
    assert engine.arrived("v0", spot)
    assert engine.parking["v0"] is spot


def test_no_zone_or_leader_state_is_exposed():
    engine = engine_for({("start", "dest"): 1.0})
    assert engine.zone_owners() == {} and engine.zone_queues() == {}
    assert engine.zone_state() == {"owners": {}, "queues": {}}
    assert engine.leader("v0", 10) is None
    assert engine.present() is True


def test_replay_must_be_disabled_because_there_is_no_trajectory(tmp_path):
    with pytest.raises(ValueError, match="关闭replay"):
        DelayTransport({"replay": {"enabled": True}, "vehicles": []}, tmp_path, lambda **_: None)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "03-数据/基准-v1/route_operations.csv").exists(),
    reason="缺少冻结路线",
)
def test_aggregate_tier_completes_and_preserves_material(monkeypatch, tmp_path):
    """同两步合成工艺：聚合档必须排空、守恒，且不报出假的碰撞或负间距。"""
    import copy

    from fabcosim.cosim import full
    from fabcosim.data.full_route import compile_full_routes

    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    resolved = compile_full_routes(config, root)
    ops = [
        copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][index])
        for index in (369, 370)
    ]
    for index, op in enumerate(ops, 1):
        op.update(
            source_step=index, processing_s=1, offset_s=0, interval_s=0,
            sample_probability=1, rework_probability=0, rework_step=0,
            dedication_step=0, cqt_step=0,
        )
    resolved["routes"]["Route_Product_1"]["operations"] = ops
    resolved["lots"] = resolved["lots"][:1]
    resolved["max_time_s"] = 10000
    resolved["transport"] = {"model": "delay"}
    resolved["replay"] = {"enabled": False}
    resolved["performance"] = {
        "stream_events": True, "route_event_geometry": False, "compact_json": True,
    }
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kw: resolved)
    output = tmp_path / "delay"
    summary = full.run_full(resolved, output)
    assert summary["completed_lots"] == 1
    assert summary["completed_tasks"] >= 2
    assert summary["collisions"] == summary["teleports"] == []
    # 聚合档不给车辆位置，因此不产生同轨间距，而不是产生一个假的0间距。
    assert summary.get("metrics", {}).get("traffic", {}).get("minimum_same_edge_gap_m") is None
