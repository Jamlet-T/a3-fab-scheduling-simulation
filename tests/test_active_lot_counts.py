"""active_lots / completed_lot_count 的语义边界。

full.py 现在用 active_lots 只做目标 .queue 计数，用 completed_lot_count 代替每轮
全lot扫描判完成。这里守住三条边界：
1. horizon 前尚未投片的 lot 不能让观察窗口提前结束；
2. 全部lot完工时确实提前退出（计数漏增会表现为TimeoutError）；
3. 不可排空时仍抛 TimeoutError，且 summary 计数与逐lot结果一致。
"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes

ROOT = Path(__file__).resolve().parents[1]


def scenario(releases: list[float], max_time_s: float = 200000) -> dict:
    """两步短路线 + 指定投片时刻；与test_release_rng_lifecycle构造方式一致。"""
    config = json.loads((ROOT / "configs/smat_full_product1.json").read_text("utf-8"))
    resolved = compile_full_routes(config, ROOT)
    operations = [
        copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][index])
        for index in (369, 370)
    ]
    for source_step, op in enumerate(operations, 1):
        op.update(
            source_step=source_step,
            processing_s=1,
            offset_s=0,
            interval_s=0,
            sample_probability=1,
            rework_probability=0,
            rework_step=0,
            dedication_step=0,
            cqt_step=0,
        )
    resolved["routes"]["Route_Product_1"]["operations"] = operations
    template = resolved["lots"][0]
    resolved["lots"] = [
        dict(template, id=f"L{index}", release_s=release) for index, release in enumerate(releases)
    ]
    resolved["max_time_s"] = max_time_s
    resolved["observation"] = None
    resolved["replay"] = {"enabled": False}
    resolved["performance"] = {
        "stream_events": False,
        "route_event_geometry": False,
        "compact_json": True,
    }
    return resolved


def run(resolved: dict, tmp_path: Path, name: str) -> dict:
    original = full.compile_full_routes
    full.compile_full_routes = lambda *args, **kwargs: resolved
    try:
        return full.run_full(copy.deepcopy(resolved), tmp_path / name)
    finally:
        full.compile_full_routes = original


def results(summary: dict) -> dict:
    return {item["lot_id"]: item for item in summary["lot_results"]}


def test_future_lot_does_not_end_the_observation_window(tmp_path):
    """L1 在窗口之后才投片：窗口必须跑满，不能因 active_lots 清空而提前结束。"""
    resolved = scenario([0, 100000], max_time_s=50000)
    resolved["observation"] = {"name": "window", "warmup_s": 0, "end_s": 50000}

    summary = run(resolved, tmp_path, "observation")

    assert summary["status"] == "window_completed"
    assert summary["end_time_s"] == 50000
    assert summary["completed_lots"] == 1
    assert summary["released_lots"] == 1
    assert summary["wip_lots"] == 0
    lot_results = results(summary)
    assert lot_results["L0"]["phase"] == "completed"
    # 未来lot未被计为已释放，也未计入完成。
    assert lot_results["L1"]["phase"] == "unreleased"
    assert lot_results["L1"]["actual_release_s"] is None


def test_all_lots_completed_breaks_before_horizon(tmp_path):
    """全部完工后按计数退出主循环，结束时刻就是最后一个lot的完工时刻。"""
    resolved = scenario([0], max_time_s=200000)

    summary = run(resolved, tmp_path, "early_exit")

    assert summary["status"] == "completed"
    assert summary["completed_lots"] == 1
    assert summary["wip_lots"] == 0
    assert summary["end_time_s"] < 200000
    lot_results = results(summary)
    assert summary["end_time_s"] == lot_results["L0"]["completion_s"]


def test_unschedulable_lots_still_time_out(tmp_path):
    """已投片但排不空、且仍有未来lot未投片时，不得被误判完成，仍抛 TimeoutError。"""
    resolved = scenario([0, 5], max_time_s=100)

    with pytest.raises(TimeoutError, match="完整工艺未排空"):
        run(resolved, tmp_path, "timeout")


def test_summary_counts_match_per_lot_results(tmp_path):
    """completed_lots/released_lots/wip_lots 与逐lot结果一致，防计数器漏增。"""
    resolved = scenario([0, 5, 20])

    summary = run(resolved, tmp_path, "counts")

    lot_results = results(summary)
    completed = sum(item["phase"] == "completed" for item in lot_results.values())
    released = sum(item["actual_release_s"] is not None for item in lot_results.values())
    assert summary["completed_lots"] == completed == 3
    assert summary["released_lots"] == released == 3
    assert summary["wip_lots"] == released - completed == 0
    assert summary["end_time_s"] == max(item["completion_s"] for item in lot_results.values())
