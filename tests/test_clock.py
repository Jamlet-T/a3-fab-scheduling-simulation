"""240日独立核查发现的10.5毫秒映射漂移回归。"""

import json
from pathlib import Path

import pytest

from fabcosim.production.model import grid_delay, grid_time_at_or_after, next_grid_time
from fabcosim.transport.fleet import FleetTransport


def test_late_global_ticks_do_not_accumulate_float_drift():
    now = 16000000.0
    for _ in range(100000):
        now = next_grid_time(now, 0.2)
    assert now == 16020000.0


def test_late_processing_deadline_does_not_add_an_extra_tick():
    assert grid_delay(20000000.6, 0.6, 0.2) == pytest.approx(0.6, abs=1e-8)
    assert grid_time_at_or_after(20000001.2, 0.2) == 20000001.2
    # 真正不在网格的完成时刻仍须向上对齐，不能提前加工/交付。
    assert grid_time_at_or_after(20000000.61, 0.2) == 20000000.8


def test_native_pause_and_resume_at_late_global_clock(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/multi_merge.json").read_text("utf-8"))
    config.update(step_s=0.2, static_time_skip=True, max_time_s=20736000)
    config["vehicles"] = config["vehicles"][:1]
    config["traffic"].update(use_route_ids=True, fit_native_stops=True)
    engine = FleetTransport(config, tmp_path, lambda *args, **kwargs: None)
    try:
        now = 0.2
        engine.step(now)
        vid = engine.ids[0]
        engine.park(vid)
        for _ in range(1000):
            now = next_grid_time(now, 0.2)
            engine.step(now)
            if engine.api.vehicle.isStopped(vid):
                break
        else:
            pytest.fail("车队未原生停稳")
        position, distance = engine.position(vid), engine.api.vehicle.getDistance(vid)
        # 连续静止推进应始终重建精确映射，不改变位置或里程。
        for index in range(10000):
            now = 16000000.0 + index * 0.2
            engine.advance_stationary(now)
            assert engine.api.simulation.getTime() + engine.time_offset == pytest.approx(
                now, rel=0, abs=1e-7
            )
        assert engine.position(vid) == position
        assert engine.api.vehicle.getDistance(vid) == distance
        engine.plan(vid, config["ports"]["B"])
        for _ in range(2000):
            before = engine.api.simulation.getTime()
            now = next_grid_time(now, 0.2)
            engine.step(now)
            assert engine.api.simulation.getTime() - before == pytest.approx(0.2, abs=1e-8)
            assert engine.api.simulation.getTime() + engine.time_offset == pytest.approx(
                now, rel=0, abs=1e-7
            )
            if engine.arrived(vid, config["ports"]["B"]):
                break
        else:
            pytest.fail("恢复后未实际抵达目标")
        assert engine.collisions == engine.teleports == []
    finally:
        engine.close()
