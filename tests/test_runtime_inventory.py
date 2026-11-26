"""运行时资源清单不增加时钟事件或保留原统计字典。"""

import copy
import json

from test_metric_history_integration import scenario

from fabcosim.cosim import full
from fabcosim.cosim.event_log import EventLog


def test_event_counter_only_counts_successful_writes(tmp_path):
    log = EventLog(tmp_path / "events.json")
    log.append({"kind": "ok"})
    try:
        log.append({"bad": object()})
    except TypeError:
        pass
    assert log.count == 1
    log.close()


def test_inventory_preserves_events_and_summary(monkeypatch, tmp_path):
    config = scenario()
    config["observation"] = {"warmup_s": 0, "end_s": 2000}
    config["performance"].update(metric_history="sqlite", spool_task_history=True)
    monkeypatch.setattr(full, "compile_full_routes", lambda prepared, root: prepared)
    old = tmp_path / "old"
    new = tmp_path / "new"
    full.run_full(copy.deepcopy(config), old)
    config["performance"]["runtime_inventory"] = True
    summary = full.run_full(copy.deepcopy(config), new)
    for name in ("events.json", "summary.json"):
        assert (old / name).read_bytes() == (new / name).read_bytes()
    rows = [json.loads(line) for line in (new / "runtime-inventory.jsonl").read_text("utf-8").splitlines()]
    final = rows[-1]
    assert rows[0]["status"] == "running"
    assert final["status"] == "window_completed"
    assert final["business_events"] == len(json.loads((new / "events.json").read_text("utf-8")))
    assert final["retained_event_dicts"] == 0
    assert final["active_lots"] == summary["wip_lots"]
    assert final["completed_lots"] == summary["completed_lots"]
    assert final["task_history_rows"] == len(json.loads((new / "config.json").read_text("utf-8"))["tasks"])
