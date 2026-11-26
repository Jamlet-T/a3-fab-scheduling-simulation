"""可视化平台适配层：只读已验收产物，汇总值与源文件逐项一致。"""

import json
from pathlib import Path

import pytest

from fabcosim.viz.datasets import DATASETS, REPO_ROOT, build_index, load_dataset

WEEKLY_METRICS = {
    "completed": "completed",
    "main_throughput_lots_per_day": "main_throughput",
    "main_mean_wip": "main_mean_wip",
    "main_cqt_rate": "main_cqt_rate",
    "tail_wip_growth": "tail_wip_growth",
}


def _source(dataset_id: str) -> dict:
    path = REPO_ROOT / DATASETS[dataset_id]["path"]
    if not path.is_file():
        pytest.skip(f"缺少已验收产物：{path}")
    return json.loads(path.read_text("utf-8"))


def test_index_registers_both_accepted_runs():
    index = build_index()
    assert [item["id"] for item in index["datasets"]] == ["weekly-60d", "extended-730"]
    for item in index["datasets"]:
        assert Path(item["path"]).name == "analysis.json"
        assert Path(item["path"]).is_relative_to(REPO_ROOT)


def test_weekly_series_matches_source_aggregate():
    raw = _source("weekly-60d")
    view = load_dataset("weekly-60d")
    assert len(view["cases"]) == len(raw["cases"])
    assert view["meta"]["seeds"] == [20261118, 20261119, 20261120]
    for rule in raw["context"]["rules"]:
        for raw_key, unified in WEEKLY_METRICS.items():
            source = raw["aggregate"][rule][raw_key]
            target = view["series"][rule][unified]
            assert target["mean"] == pytest.approx(source["mean"])
            assert target["min"] == pytest.approx(source["minimum"])
            assert target["max"] == pytest.approx(source["maximum"])
            assert [v["value"] for v in target["values"]] == pytest.approx(source["values"])


def test_weekly_phases_preserve_all_lots():
    raw = _source("weekly-60d")
    view = load_dataset("weekly-60d")
    for rule in raw["context"]["rules"]:
        raw_total = sum(int(r["lots"]) for r in raw["terminal_phases"] if r["rule"] == rule)
        view_total = sum(p["lots"] for p in view["phases"] if p["rule"] == rule)
        assert view_total == raw_total


def test_weekly_pair_keeps_one_row_per_seed():
    raw = _source("weekly-60d")
    view = load_dataset("weekly-60d")
    assert len(view["pair"]) == len(raw["c_q2_cqt"])
    for row in view["pair"]:
        source = next(r for r in raw["c_q2_cqt"] if int(r["seed"]) == row["seed"])
        assert row["c_violations"] == source["main"]["baseline_violations"]
        assert row["q2_violations"] == source["main"]["candidate_violations"]
        assert row["c_overdue"] == source["c_overdue_censored"]
        assert row["q2_overdue"] == source["q2_overdue_censored"]


def test_extended_keeps_unequal_samples_without_padding():
    raw = _source("extended-730")
    view = load_dataset("extended-730")
    assert len(view["cases"]) == len(raw["passed_cases"]) == 14
    coverage = {c["rule"]: c["n"] for c in view["coverage"]}
    assert coverage["fifo"] == 3
    assert coverage["c"] == 1
    assert coverage["q2"] == 1
    for item in view["coverage"]:
        series = view["series"][item["rule"]]["completed"]
        assert len(series["values"]) == item["n"]


def test_extended_trend_and_blocks_use_single_seed():
    _source("extended-730")
    view = load_dataset("extended-730")
    assert view["trend_seed"] == 20261118
    assert {row["seed"] for row in view["trends"]} == {20261118}
    assert {row["seed"] for row in view["blocks"]} == {20261118}


def test_extended_release_rate_uses_window_duration():
    raw = _source("extended-730")
    view = load_dataset("extended-730")
    row = next(r for r in raw["passed_cases"] if r["seed"] == 20261118 and r["rule"] == "fifo")
    main = row["windows"]["main"]
    expected = main["window_released_lots"] / (main["window"]["duration_s"] / 86400.0)
    assert view["release_rate"] == pytest.approx(expected)


def test_extended_phase_mix_sums_to_mean_wip():
    # 阶段均值之和应回到主窗平均在制，防止适配时漏段或重复计数。
    _source("extended-730")
    view = load_dataset("extended-730")
    for item in view["coverage"]:
        total = sum(p["mean_wip"] for p in view["phase_mix"] if p["rule"] == item["rule"])
        expected = view["series"][item["rule"]]["main_mean_wip"]["mean"]
        assert total == pytest.approx(expected, rel=1e-6)


def test_extended_devices_sorted_and_time_composition_complete():
    _source("extended-730")
    view = load_dataset("extended-730")
    for rule in view["meta"]["rules"]:
        rows = [d for d in view["devices"] if d["rule"] == rule["key"]]
        assert 0 < len(rows) <= 20
        assert all(a["waiting_mean_wip"] >= b["waiting_mean_wip"] for a, b in zip(rows, rows[1:]))
        for row in rows:
            total = (
                row["processing"]
                + row["setup"]
                + row["loading"]
                + row["unloading"]
                + row["down"]
                + row["idle"]
            )
            assert total == pytest.approx(1.0, abs=0.02)


def test_extended_priority_late_not_above_completed():
    _source("extended-730")
    view = load_dataset("extended-730")
    for row in view["priority"]:
        assert 0 <= row["late"] <= row["completed"]


def test_missing_artifact_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_dataset("weekly-60d", data_root=tmp_path)
