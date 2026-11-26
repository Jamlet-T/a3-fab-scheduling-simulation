"""七批键挤一缓冲的机制反例；不是冻结全工艺数据的验证。"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes

ROOT = Path(__file__).resolve().parents[1]


def scenario():
    config = json.loads((ROOT / "configs/scale1000_110d_optimized.json").read_text("utf-8"))
    config.pop("release_calendar")
    config = compile_full_routes(config, ROOT)
    op = copy.deepcopy(config["routes"]["Route_Product_2"]["operations"][0])
    op.update(processing_s=1, offset_s=0, interval_s=0, sample_probability=1,
              rework_probability=0, rework_step=0, dedication_step=0, cqt_step=0,
              setup="", setup_s=0)
    routes = [f"R{i}" for i in range(7)]
    config["routes"] = {name: {"operations": [copy.deepcopy(op)], "rpt_s": 1,
                               "remaining_s": {1: 1}} for name in routes}
    config["production"].update(routes=routes, input_buffer_lots=24, dispatch_mode="deferred")
    config["production"].pop("batch_policy", None)
    config["candidates"][op["tool_group"]] = config["candidates"][op["tool_group"]][:2]
    config["vehicles"] = config["vehicles"][:4]
    config["lots"] = [{"id": f"L{wave}_{index}", "route": name,
                       "wafer_count": 25, "release_s": wave}
                      for wave in range(5) for index, name in enumerate(routes)]
    config["observation"] = {"warmup_s": 0, "end_s": 3 * 86400}
    config["max_time_s"] = 3 * 86400
    config["performance"].update(delay_event_wakeup=True, event_compression="none",
                                 metric_history="sqlite", spool_task_history=True)
    return config


def execute(config, output, monkeypatch, policy):
    config = copy.deepcopy(config)
    if policy is not None:
        config["production"]["batch_host_policy"] = policy
    monkeypatch.setattr(full, "compile_full_routes", lambda prepared, root: prepared)
    return full.run_full(config, output)


def test_compatible_budget_removes_fragmentation_without_relaxing_constraints(monkeypatch, tmp_path):
    config = scenario()
    legacy = execute(config, tmp_path / "legacy", monkeypatch, "legacy")
    improved = execute(config, tmp_path / "compatible", monkeypatch, "compatible_buffer")
    assert legacy["completed_lots"] == 0
    assert legacy["physical_processing_operations"] == 0
    assert improved["completed_lots"] == 35
    assert improved["underfilled_batches"] == 0
    assert improved["batch_count"] == 7
    assert improved["vehicle_count"] == 4
    assert improved["batch_host_policy"] == "compatible_buffer"
    audit = json.loads((tmp_path / "compatible/batch-hosts.json").read_text("utf-8"))
    budgets = list(audit["per_host_minimum_lots"].values())
    assert len(budgets) == 2 and sum(budgets) == 35 and max(budgets) <= 24
    events = json.loads((tmp_path / "compatible/events.json").read_text("utf-8"))
    assert all(125 <= e["wafers"] <= 150 for e in events if e["kind"] == "batch.formed")


def test_default_and_legacy_business_output_match(monkeypatch, tmp_path):
    config = scenario()
    execute(config, tmp_path / "default", monkeypatch, None)
    execute(config, tmp_path / "legacy", monkeypatch, "legacy")
    for name in ("events.json", "summary.json"):
        assert (tmp_path / "default" / name).read_bytes() == (tmp_path / "legacy" / name).read_bytes()


@pytest.mark.parametrize("internal_transfer", [False, True])
@pytest.mark.parametrize("audit", ["every_step", "indexed"])
def test_queue_occupancy_index_preserves_batch_and_port_flow(
    monkeypatch, tmp_path, internal_transfer, audit
):
    config = scenario()
    if internal_transfer:
        first = copy.deepcopy(config["routes"]["R0"]["operations"][0])
        second = copy.deepcopy(first)
        first["source_step"], second["source_step"] = 1, 2
        config["routes"] = {
            "R0": {"operations": [first, second], "rpt_s": 2, "remaining_s": {1: 2, 2: 1}}
        }
        config["production"]["routes"] = ["R0"]
        config["lots"] = [lot for lot in config["lots"] if lot["route"] == "R0"]
        config["candidates"][first["tool_group"]] = config["candidates"][first["tool_group"]][:1]
    if audit == "every_step":
        config["performance"]["material_audit"] = "every_step"
    else:
        config["performance"].update(
            incremental_material_audit=True, material_audit_port_index=True
        )
    summaries = []
    for label, enabled in (("scan", False), ("index", True)):
        config["performance"]["queue_occupancy_index"] = enabled
        summaries.append(execute(config, tmp_path / label, monkeypatch, "compatible_buffer"))
    assert summaries[0] == summaries[1]
    assert summaries[1]["completed_lots"] == len(config["lots"])
    assert summaries[1]["underfilled_batches"] == 0
    for name in ("events.json", "summary.json"):
        assert (tmp_path / "scan" / name).read_bytes() == (tmp_path / "index" / name).read_bytes()
    if internal_transfer:
        events = json.loads((tmp_path / "index/events.json").read_text("utf-8"))
        assert any(event["kind"] == "transport.internal" for event in events)


@pytest.mark.parametrize("setting", ["wrong", "tiny_buffer", "mixed_wafers", "finite_cohort"])
def test_invalid_admission_is_rejected_before_output(monkeypatch, tmp_path, setting):
    config = scenario()
    policy = "compatible_buffer"
    if setting == "wrong":
        policy = "wrong"
    elif setting == "tiny_buffer":
        config["production"]["input_buffer_lots"] = 4
    elif setting == "mixed_wafers":
        config["lots"][0]["wafer_count"] = 24
    else:
        config["production"]["batch_policy"] = "finite_cohort"
    with pytest.raises(ValueError):
        execute(config, tmp_path / "invalid", monkeypatch, policy)
    assert not (tmp_path / "invalid").exists()
