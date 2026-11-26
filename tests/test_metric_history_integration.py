"""MetricHistory可选接入引擎的字节级对照。

memory档（默认）保持原有内存事件统计；sqlite档只把统计类事件交给派生缓存。
两档必须写出逐字节相同的完整events与完整summary，包含观测窗口、额外分析窗口
与运输右删失。
"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes

ROOT = Path(__file__).resolve().parents[1]


def scenario() -> dict:
    """两步短路线，两lot先后投片；沿用现有小场景构造方式。"""
    config = json.loads((ROOT / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (ROOT / config["network"]["workbook"]).exists():
        pytest.skip("缺少原始路网")
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
        dict(template, id="L0", release_s=0),
        dict(template, id="L1", release_s=1500),
    ]
    resolved["max_time_s"] = 20000
    resolved["replay"] = {"enabled": False}
    # 完整事件仍由EventLog原样落盘；sqlite档只替代统计缓存。
    resolved["performance"] = {
        "stream_events": True,
        "route_event_geometry": False,
        "compact_json": True,
    }
    return resolved


def run(resolved: dict, output: Path, monkeypatch, mode: str) -> dict:
    config = copy.deepcopy(resolved)
    config["performance"]["metric_history"] = mode
    def compiled(prepared, root):
        value = copy.deepcopy(config)
        value["max_time_s"] = prepared["max_time_s"]
        return value

    monkeypatch.setattr(full, "compile_full_routes", compiled)
    return full.run_full(copy.deepcopy(config), output)


def assert_same_artifacts(memory_output: Path, sqlite_output: Path) -> None:
    for name in ("events.json", "summary.json"):
        assert (sqlite_output / name).read_bytes() == (memory_output / name).read_bytes(), name
    assert not (memory_output / "metric-history.sqlite3").exists()
    assert (sqlite_output / "metric-history.sqlite3").is_file()


def test_observation_and_analysis_windows_match_memory_bytes(tmp_path, monkeypatch):
    """主窗口与分析窗口共用缓存查询，含运输右删失。"""
    resolved = scenario()
    # L1在1500投片：主窗口正好在投片时刻结束，其运输请求被右删失。
    resolved["observation"] = {"warmup_s": 0, "end_s": 1500}
    resolved["analysis_windows"] = [
        {"name": "later", "warmup_s": 500, "end_s": 1500},
    ]

    memory_summary = run(resolved, tmp_path / "memory", monkeypatch, "memory")
    sqlite_summary = run(resolved, tmp_path / "sqlite", monkeypatch, "sqlite")

    assert_same_artifacts(tmp_path / "memory", tmp_path / "sqlite")
    assert memory_summary["status"] == sqlite_summary["status"] == "window_completed"
    assert sqlite_summary["end_time_s"] == 1500
    assert sqlite_summary["metrics"]["transport_wait"]["right_censored_count"] > 0
    later = sqlite_summary["analysis_windows"]["later"]["transport_wait"]
    assert later["requested_count"] > 0
    json.dumps(sqlite_summary, ensure_ascii=False)


def test_finite_run_without_observation_matches_memory_bytes(tmp_path, monkeypatch):
    """无观测窗口的有限场景同样走缓存计数，不写metrics但仍给同摘要。"""
    resolved = scenario()
    resolved["observation"] = None
    resolved["lots"][1]["release_s"] = 300

    memory_summary = run(resolved, tmp_path / "memory", monkeypatch, "memory")
    sqlite_summary = run(resolved, tmp_path / "sqlite", monkeypatch, "sqlite")

    assert_same_artifacts(tmp_path / "memory", tmp_path / "sqlite")
    assert sqlite_summary["status"] == "completed"
    assert "metrics" not in sqlite_summary
    assert sqlite_summary["completed_lots"] == 2
    assert sqlite_summary["reworks"] == memory_summary["reworks"] == 0


@pytest.mark.parametrize(
    ("performance", "replay", "message"),
    [
        ({"metric_history": "disk"}, {"enabled": False}, "memory或sqlite"),
        ({"metric_history": "sqlite", "stream_events": False}, {"enabled": False}, "stream_events=true"),
        ({"metric_history": "sqlite", "stream_events": True}, {"enabled": True}, "需要"),
    ],
)
def test_invalid_combinations_fail_before_output(
    tmp_path, monkeypatch, performance, replay, message
):
    resolved = scenario()
    resolved["observation"] = {"warmup_s": 0, "end_s": 1000}
    resolved["replay"] = replay
    resolved["performance"] = dict(resolved["performance"], **performance)
    output = tmp_path / "invalid-run"
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kwargs: resolved)

    with pytest.raises(ValueError, match=message):
        full.run_full(copy.deepcopy(resolved), output)
    assert not output.exists()


def test_cache_close_failure_cannot_report_success(tmp_path, monkeypatch):
    original = full.MetricHistory

    class FailingClose(original):
        def close(self):
            super().close()
            raise RuntimeError("injected-cache-close")

    monkeypatch.setattr(full, "MetricHistory", FailingClose)
    output = tmp_path / "close-failure"
    resolved = scenario()
    resolved["observation"] = {"warmup_s": 0, "end_s": 100}
    with pytest.raises(RuntimeError, match="injected-cache-close"):
        run(resolved, output, monkeypatch, "sqlite")
    assert json.loads((output / "summary.json").read_text("utf-8"))["status"] == "failed"
    assert isinstance(json.loads((output / "events.json").read_text("utf-8")), list)


def test_original_error_survives_cache_close_error(tmp_path, monkeypatch):
    original = full.MetricHistory

    class FailingBoth(original):
        def observe(self, event):
            raise ValueError("injected-primary-error")

        def close(self):
            super().close()
            raise RuntimeError("injected-secondary-close")

    monkeypatch.setattr(full, "MetricHistory", FailingBoth)
    output = tmp_path / "primary-failure"
    with pytest.raises(ValueError, match="injected-primary-error"):
        run(scenario(), output, monkeypatch, "sqlite")
    summary = json.loads((output / "summary.json").read_text("utf-8"))
    assert summary["status"] == "failed"
    assert "injected-primary-error" in summary["error"]
    assert isinstance(json.loads((output / "events.json").read_text("utf-8")), list)
