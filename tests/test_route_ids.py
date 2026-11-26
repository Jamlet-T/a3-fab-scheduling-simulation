"""共享路线及原生停车接口的逐步对照，不能改变路径、坐标或里程。"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.transport.fleet import FleetTransport


def trace(settings, output, shared, subscriptions=None, scheduled=None):
    config = copy.deepcopy(settings)
    config["traffic"]["use_route_ids"] = shared
    config["traffic"]["fit_native_stops"] = shared
    if subscriptions is not None:
        config.setdefault("performance", {})["vehicle_subscriptions"] = subscriptions
    if scheduled is not None:
        config.setdefault("performance", {}).update(gate_schedule=scheduled, arrival_cache=scheduled)
    output.mkdir()
    events = []
    engine = FleetTransport(config, output, lambda kind, **fields: events.append((kind, fields)))
    rows = []
    try:
        for index in range(1, 15001):
            now = round(index * config["step_s"], 9)
            if index == 2:
                for vid in engine.ids:
                    engine.plan(vid, None)
            if index in (300, 360, 1100, 1300):
                vid = engine.ids[0]
                if index in (300, 1100):
                    engine.plan(vid, config["ports"]["A"])
                else:
                    engine.plan(vid, None)
            engine.step(now)
            snapshots = [engine.snapshot(vid) for vid in engine.ids]
            if scheduled is not None:
                for row in snapshots:
                    # 同步重复读取，再经过途中重规划/停车变化，缓存不能造成提前到达。
                    row["arrived_A"] = engine.arrived(row["id"], config["ports"]["A"])
                    assert row["arrived_A"] == engine.arrived(row["id"], config["ports"]["A"])
            rows.append(snapshots)
        assert engine.collisions == engine.teleports == []
        return rows, events
    finally:
        engine.close()


@pytest.mark.parametrize("name", ["multi_merge", "smat_multi"])
def test_shared_native_routes_preserve_every_step(name, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / f"configs/{name}.json").read_text("utf-8"))
    config["max_time_s"] = 4000
    if name == "smat_multi" and not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少真实路网")
    ordinary, events = trace(config, tmp_path / "ordinary", False)
    shared, shared_events = trace(config, tmp_path / "shared", True)
    assert shared_events == events
    assert len(shared) == len(ordinary)
    for previous, current in zip(ordinary, shared):
        for a, b in zip(previous, current):
            assert a["id"] == b["id"] and a["edge"] == b["edge"]
            assert a["zone_wait"] == b["zone_wait"]
            for field in ("x", "y", "speed", "position_m", "distance_m"):
                assert b[field] == pytest.approx(a[field], abs=1e-7, rel=0)


def test_vehicle_subscriptions_preserve_each_step_and_replanning(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_multi.json").read_text("utf-8"))
    if not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少真实路网")
    config["max_time_s"] = 4000
    direct, direct_events = trace(config, tmp_path / "direct", True, False)
    subscribed, subscribed_events = trace(config, tmp_path / "subscribed", True, True)
    # 含途中重规划和停车增删；每步都必须与原生直接读取完全一致。
    assert direct_events == subscribed_events
    assert direct == subscribed


@pytest.mark.parametrize("name", ["multi_merge", "smat_multi"])
def test_distance_schedule_and_arrival_cache_preserve_every_step(name, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / f"configs/{name}.json").read_text("utf-8"))
    if name == "smat_multi" and not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少真实路网")
    config["max_time_s"] = 4000
    reference, reference_events = trace(config, tmp_path / "reference", True, scheduled=False)
    current, current_events = trace(config, tmp_path / "current", True, scheduled=True)
    assert current_events == reference_events
    assert current == reference


def test_old_stop_index_remains_in_bounds_after_short_shared_replan(tmp_path):
    """旧停靠在第64条边，新任务仅跨三条边；新route需容纳旧索引。"""
    root = Path(__file__).resolve().parents[1]
    traces = []
    for shared in (False, True):
        config = json.loads((root / "configs/multi_merge.json").read_text("utf-8"))
        config["vehicles"] = config["vehicles"][:1]
        config["traffic"].update(use_route_ids=shared, fit_native_stops=shared)
        output = tmp_path / str(shared)
        output.mkdir()
        engine = FleetTransport(config, output, lambda *args, **kwargs: None)
        vehicle = engine.api.vehicle
        vid = engine.ids[0]
        try:
            cycle = ["e0", "e1", "e2", "e3", "e4"]
            engine.api.route.add("long-history", cycle * 25 + ["e0"])
            vehicle.setRouteID(vid, "long-history")
            for _ in range(20000):
                engine.api.simulationStep()
                if vehicle.getRouteIndex(vid) >= 60:
                    break
            vehicle.setStop(vid, "e4", pos=15, duration=10000)
            for _ in range(20000):
                engine.api.simulationStep()
                if vehicle.isStopped(vid):
                    break
            assert vehicle.isStopped(vid)
            old_index = vehicle.getRouteIndex(vid)
            assert old_index == 64
            before = engine.snapshot(vid)
            goal = {"edge": "e1", "position_m": 15}
            engine.plan(vid, goal)
            after = engine.snapshot(vid)
            assert after["position_m"] == before["position_m"]
            assert after["distance_m"] == before["distance_m"]
            if shared:
                assert len(engine.plans[vid]["route"]) <= old_index
                assert len(vehicle.getRoute(vid)) > old_index
                assert tuple(vehicle.getRoute(vid))[:len(engine.plans[vid]["route"])] == tuple(
                    engine.plans[vid]["route"]
                )
            rows = []
            for _ in range(2000):
                now = round(engine.api.simulation.getTime() + config["step_s"], 9)
                engine.step(now)
                rows.append(engine.snapshot(vid))
                if engine.arrived(vid, goal):
                    break
            assert engine.arrived(vid, goal)
            assert engine.collisions == engine.teleports == []
            traces.append(rows)
        finally:
            engine.close()
    assert len(traces[0]) == len(traces[1])
    for a, b in zip(*traces):
        assert a["edge"] == b["edge"]
        for field in ("x", "y", "speed", "position_m", "distance_m"):
            assert b[field] == pytest.approx(a[field], abs=1e-7, rel=0)
