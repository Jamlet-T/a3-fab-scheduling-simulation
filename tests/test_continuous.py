"""持续投片的唯一性、混流合法组批及窗口截止保留WIP。"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes
from fabcosim.production.batching import select_queue_batch
from fabcosim.production.releases import prepare_releases


def test_release_calendar_mix_and_cutoff_without_padding():
    settings = {
        "step_s": 0.1,
        "production": {},
        "observation": {"warmup_s": 2, "end_s": 10},
        "release_stream": {
            "interval_s": 3,
            "route_pattern": ["A", "B"],
            "bootstrap_lots_per_route": 1,
        },
    }
    config = prepare_releases(settings)
    assert [d["release_s"] for d in config["lots"]] == [0, 0, 3, 6, 9]
    assert [d["route"] for d in config["lots"]] == ["A", "B", "A", "B", "A"]
    assert len({d["id"] for d in config["lots"]}) == 5
    assert all(d["wafer_count"] == 25 for d in config["lots"])
    assert "lots" not in settings
    assert config["production"]["allow_underfilled_after_timeout"] is False


def test_incomplete_batch_head_does_not_block_another_product():
    queue = []
    for route, count in (("A", 3), ("B", 4)):
        for index in range(count):
            queue.append(
                {
                    "route": route,
                    "lot_id": f"{route}{index}",
                    "ready_s": len(queue),
                    "op": {"source_step": 1, "batch_min": 100, "batch_max": 125},
                }
            )
    quantities = {j["lot_id"]: 25 for j in queue}
    selected = select_queue_batch(queue, quantities, 100, 30, False)
    assert {j["lot_id"] for j in selected} == {f"B{i}" for i in range(4)}
    assert select_queue_batch(queue[:3], quantities, 100000, 30, False) == []


@pytest.mark.parametrize("end", [5, 6000])
def test_observation_stops_at_horizon_without_forcing_lot(monkeypatch, tmp_path, end):
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
    # 专用单步测试输入，不作为完整源路线的验证。
    resolved["routes"]["Route_Product_1"]["operations"] = [op]
    resolved["lots"] = resolved["lots"][:1]
    resolved["observation"] = {"warmup_s": 1, "end_s": end}
    resolved["max_time_s"] = end
    if end == 6000:
        resolved["replay"] = {"enabled": True, "start_s": 1000, "end_s": 1100}
    monkeypatch.setattr(full, "compile_full_routes", lambda *args: resolved)
    output = tmp_path / f"window-{end}"
    summary = full.run_full(resolved, output)
    assert summary["status"] == "window_completed"
    assert summary["end_time_s"] == end and summary["completion_s"] is None
    assert summary["released_lots"] == 1
    assert summary["wip_lots"] + summary["completed_lots"] == 1
    assert summary["collisions"] == summary["teleports"] == []
    if end == 5:
        assert summary["completed_lots"] == summary["completed_tasks"] == 0
        assert summary["lot_results"][0]["completion_s"] is None
        assert summary["lot_results"][0]["location"] != "completed"
    else:
        assert summary["completed_lots"] == 1 and summary["wip_lots"] == 0
        assert summary["lot_results"][0]["completion_s"] < end
        frames = json.loads((output / "frames.json").read_text("utf-8"))
        assert frames[0]["time"] == 1000 and frames[-1]["time"] == 1100
        assert summary["metrics"]["end_wip_lots"] == 0
