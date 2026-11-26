"""冻结基准的实际扩展点：自定义选择生效，生产资源与成批逻辑仍由引擎执行。"""

import copy

from test_production_destination_load import (
    destination_config,
    peak_buffer_occupancy,
    selected,
)
from test_production_only import execute, fixture_config

from fabcosim.policies import registry


def test_registered_destination_changes_real_assignment_with_capacity(monkeypatch, tmp_path):
    config = destination_config(
        ("G", "tool", ("M0", "M1"), 5, False),
        ("H", "tool", ("N",), 5, False),
        count=4, capacity=1, end=100,
    )
    choices = []

    def prefer_last(candidates, *, distance, queue_lengths, penalty_m, used_hosts):
        assert set(candidates) == set(queue_lengths)
        assert all(distance(name) == 0 for name in candidates)
        choices.append(tuple(candidates))
        return candidates[-1]

    monkeypatch.setitem(registry.RULES["destination"], "benchmark_last", prefer_last)
    baseline, baseline_events = execute(monkeypatch, tmp_path / "baseline", config)
    config["policies"] = {"destination": "benchmark_last"}
    custom, events = execute(monkeypatch, tmp_path / "custom", config)
    assert any(len(candidates) == 2 for candidates in choices)
    assert {e["equipment"] for e in selected(baseline_events)} == {"M0", "M1"}
    assert {e["equipment"] for e in selected(events)} == {"M1"}
    assert custom["policies"]["destination"] == "benchmark_last"
    assert baseline["completed_lots"] == custom["completed_lots"] == 4
    assert custom["completed_operations"] == 8
    assert max(peak_buffer_occupancy(events).values()) == 1


def test_registered_job_changes_real_dispatch_order_with_context(monkeypatch, tmp_path):
    config = fixture_config(count=3, capacity=3)
    op = config["routes"]["R"]["operations"][0] | {"processing_s": 20}
    config["routes"]["R"].update(operations=[op], remaining_s={1: 20}, rpt_s=20)
    config["source_catalog"]["routes"] = {"R": 1}
    candidates_seen = []

    def last_ready(queue, *, now, remaining):
        assert all(now >= job["ready_s"] and remaining(job) == 20 for job in queue)
        candidates_seen.append(tuple(job["lot_id"] for job in queue))
        return queue[-1] if queue else None

    monkeypatch.setitem(registry.RULES["production.job"], "benchmark_last", last_ready)
    baseline, first = execute(monkeypatch, tmp_path / "baseline", config)
    config["policies"] = {"production": {"job": "benchmark_last"}}
    custom, second = execute(monkeypatch, tmp_path / "custom", config)
    assert ("L1", "L2") in candidates_seen
    orders = [[e["lot_id"] for e in events if e["kind"] == "processing.start"]
              for events in (first, second)]
    assert orders == [["L0", "L1", "L2"], ["L0", "L2", "L1"]]
    assert baseline["completed_lots"] == custom["completed_lots"] == 3
    assert custom["completed_operations"] == 3
    assert custom["policies"]["production.job"] == "benchmark_last"
    assert [e["time"] for e in second] == sorted(e["time"] for e in second)
    starts = [e["time"] for e in second if e["kind"] == "processing.start"]
    assert all(b - a >= 20 for a, b in zip(starts, starts[1:]))


def test_queue_batch_keeps_strict_groups_outside_custom_rule(monkeypatch, tmp_path):
    config = fixture_config(batch=True, count=3, capacity=3)
    config["observation"] = {"warmup_s": 0, "end_s": 100}
    eligible = []

    def last_legal(queue, *, now, remaining):
        eligible.append(tuple(job["lot_id"] for job in queue))
        return queue[-1] if queue else None

    def excluded_group(*args, **kwargs):
        raise AssertionError("queue_batch基准不调用finite_cohort的开批组接口")

    monkeypatch.setitem(registry.RULES["production.job"], "benchmark_last", last_legal)
    monkeypatch.setitem(registry.RULES["production.group"], "earliest_ready", excluded_group)
    config["policies"] = {"production": {"job": "benchmark_last"}}
    before = copy.deepcopy(config)
    summary, events = execute(monkeypatch, tmp_path, config)
    assert config == before
    assert summary["completed_lots"] == 2 and summary["wip_lots"] == 1
    assert summary["underfilled_batches"] == 0
    batches = [e for e in events if e["kind"] == "batch.formed"]
    assert len(batches) == 1 and batches[0]["wafers"] == 50
    assert len(set(batches[0]["lot_ids"])) == 2
    assert () in eligible
