"""完整源路线覆盖与静止积分压缩的因果等价检查。"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.data.full_route import compile_full_routes
from fabcosim.production.batching import finite_cohorts
from fabcosim.transport.fleet import FleetTransport


def test_all_source_routes_have_every_step_and_physical_mapping():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少原始基准")
    names = [f"Route_Product_{i}" for i in range(1, 11)] + [
        f"Route_Product_E{i}" for i in range(1, 4)
    ]
    config["production"]["routes"] = names
    resolved = compile_full_routes(config, root)
    assert len(resolved["routes"]) == 13
    assert sum(len(r["operations"]) for r in resolved["routes"].values()) == 5646
    assert len(resolved["routes"]["Route_Product_1"]["operations"]) == 521
    for name, route in resolved["routes"].items():
        assert [op["source_step"] for op in route["operations"]] == list(
            range(1, len(route["operations"]) + 1)
        )
        for op in route["operations"]:
            assert resolved["candidates"][op["tool_group"]]
            assert op["source_sheet"] == name
            assert op["source_record"]["STEP"] == str(op["source_step"])
    assert sum(a["kind"] == "tool" for a in resolved["assets"].values()) == 1068
    assert all(
        resolved["assets"][name]["kind"] == "buffer" for name in resolved["candidates"]["Delay_32"]
    )


def test_finite_cohorts_obey_all_source_batch_bounds():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少原始基准")
    config = compile_full_routes(config, root)
    cohorts = finite_cohorts(config)
    ids = {lot["id"] for lot in config["lots"]}
    for op in config["routes"]["Route_Product_1"]["operations"]:
        if op["unit"] != "Batch":
            continue
        groups = {cohorts["Route_Product_1", op["source_step"], lid] for lid in ids}
        assert set(member for group in groups for member in group) == ids
        assert sum(map(len, groups)) == len(ids)
        assert all(op["batch_min"] <= len(group) * 25 <= op["batch_max"] for group in groups)
    config["lots"] = config["lots"][:4]
    with pytest.raises(ValueError, match="不降低批量下限"):
        finite_cohorts(config)


def test_static_time_skip_preserves_motion_and_delivery_time(tmp_path):
    root = Path(__file__).resolve().parents[1]
    base = json.loads((root / "configs/multi_merge.json").read_text("utf-8"))
    base["vehicles"] = base["vehicles"][:1]
    results = []
    for skip in (False, True):
        config = copy.deepcopy(base)
        config["static_time_skip"] = skip
        output = tmp_path / str(skip)
        output.mkdir()
        engine = FleetTransport(config, output, lambda *a, **k: None)
        try:
            now = 0.1
            engine.step(now)
            vid = engine.ids[0]
            with pytest.raises(RuntimeError, match="运动中的车队"):
                engine.advance_stationary(now + 1)
            engine.park(vid)
            for _ in range(1000):
                now = round(now + 0.1, 9)
                engine.step(now)
                if engine.api.vehicle.isStopped(vid):
                    break
            before = engine.snapshot(vid)
            now += 300
            engine.advance_stationary(now)
            after = engine.snapshot(vid)
            assert before["distance_m"] == after["distance_m"]
            engine.plan(vid, config["ports"]["B"])
            for _ in range(2000):
                now = round(now + 0.1, 9)
                engine.step(now)
                if engine.arrived(vid, config["ports"]["B"]):
                    break
            else:
                pytest.fail("恢复后未实际抵达目标")
            results.append((now, engine.snapshot(vid)["distance_m"]))
        finally:
            engine.close()
    assert results[0] == pytest.approx(results[1], abs=1e-7)


def test_replanning_after_front_crosses_reset_keeps_tail_clearance(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/multi_merge.json").read_text("utf-8"))
    config["vehicles"] = config["vehicles"][:1]
    engine = FleetTransport(config, tmp_path, lambda *a, **k: None)
    try:
        engine.nodes = {
            "merge": {"ZCU_NAME": "test", "ZCU_TYPE": "STOP"},
            "n1": {"ZCU_NAME": "test", "ZCU_TYPE": "RESET"},
        }
        vid, now = engine.ids[0], 0.1
        engine.step(now)
        engine.plan(vid, None)
        for _ in range(500):
            now = round(now + 0.1, 9)
            engine.step(now)
            p = engine.position(vid)
            if p["edge"] == "e2" and 0 < p["position_m"] < 0.8:
                break
        else:
            pytest.fail("未到达RESET后的车尾清出阶段")
        held = engine.owners["test"][1]
        exit_distance = held.exit
        assert held.exit - 0.909 <= engine.api.vehicle.getDistance(vid) < held.exit
        engine.plan(vid, config["ports"]["B"])
        assert engine.owners["test"][1].exit == exit_distance
        for _ in range(20):
            now = round(now + 0.1, 9)
            engine.step(now)
        assert "test" not in engine.owners
    finally:
        engine.close()
