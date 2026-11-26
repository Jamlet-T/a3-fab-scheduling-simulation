"""任务审计移出RAM后，原config任务数组与业务结果保持相同。"""

import copy
import gzip
import json

import pytest
from test_metric_history_integration import scenario

from fabcosim.cosim import full


@pytest.mark.parametrize("stats", ["memory", "sqlite"])
def test_spooled_tasks_preserve_all_original_artifacts(monkeypatch, tmp_path, stats):
    config = scenario()
    config["observation"] = {"warmup_s": 0, "end_s": 2000}
    config["performance"].update(metric_history=stats, event_compression="gzip")
    monkeypatch.setattr(full, "compile_full_routes", lambda prepared, root: prepared)
    old = tmp_path / "old"
    new = tmp_path / "new"
    full.run_full(copy.deepcopy(config), old)
    config["performance"]["spool_task_history"] = True
    full.run_full(copy.deepcopy(config), new)
    for name in ("events.json.gz", "summary.json"):
        assert (old / name).read_bytes() == (new / name).read_bytes()
    original = json.loads((old / "config.json").read_text("utf-8"))
    actual = json.loads((new / "config.json").read_text("utf-8"))
    actual["performance"].pop("spool_task_history")
    expected = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
    assert json.dumps(actual, ensure_ascii=False, separators=(",", ":")) == expected
    with gzip.open(new / "task-history.json.gz", "rt", encoding="utf-8") as stream:
        assert json.load(stream) == original["tasks"]


def test_spooling_rejects_pretty_output_before_creating_directory(monkeypatch, tmp_path):
    config = scenario()
    config["performance"].update(spool_task_history=True, compact_json=False)
    monkeypatch.setattr(full, "compile_full_routes", lambda prepared, root: prepared)
    with pytest.raises(ValueError, match="compact_json"):
        full.run_full(config, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()
