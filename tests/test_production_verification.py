"""production_only核查器的专项测试：真实小运行通过，各类篡改必须被拒绝。"""

import copy
import gzip
import json
import shutil

import pytest

from fabcosim.cosim import full
from fabcosim.production.verification import main, verify_production_run


def production_config(count=2, capacity=2, step_s=0.3, fixed_transfer_s=2.0):
    """两步纯生产：步骤1严格成批加工，步骤2是虚拟delay。"""
    op = dict(
        source_step=1, tool_group="G", unit="Batch", processing_s=5, offset_s=0,
        interval_s=0, virtual=False, sample_probability=1, rework_probability=0,
        rework_step=0, dedication_step=0, cqt_step=0, cqt_s=0, setup="",
        setup_when="need", setup_s=0, batch_min=50, batch_max=50,
    )
    ops = [op, {**op, "source_step": 2, "unit": "Lot", "virtual": True, "processing_s": 3}]
    assets = {
        name: dict(kind=kind, tool_group=group, input_port=name + ".in",
                   output_port=name + ".out")
        for name, kind, group in [
            ("C", "commit", "COMMIT"), ("M", "tool", "G"), ("E", "complete", "COMPLETE"),
        ]
    }
    return dict(
        name="verify-fixture", seed=3, step_s=step_s, max_time_s=200,
        transport={"model": "production_only"}, replay={"enabled": False},
        production=dict(
            routes=["R"], processing_distribution="nominal_mean",
            input_buffer_lots=capacity, fixed_transfer_s=fixed_transfer_s,
            batch_wait_s=5, allow_underfilled_after_timeout=False,
            queue_penalty_m=1, max_visits_per_lot=10, dispatch_mode="deferred",
        ),
        lots=[dict(id=f"L{i}", route="R", wafer_count=25, release_s=i * 3)
              for i in range(count)],
        routes={"R": dict(operations=ops, remaining_s={1: 10, 2: 5}, rpt_s=10)},
        source_catalog={"routes": {"R": 2}}, assets=assets, ports={},
        candidates={"COMMIT": ["C"], "G": ["M"], "COMPLETE": ["E"]},
        toolgroups={"G": {"CASCADINGTOOL": None, "LOADINGTIME": 0, "UNLOADINGTIME": 0}},
        setups=[], performance={},
    )


def cascading_config():
    """第一步改成级联Lot工序，触发module.enter/module.exit。"""
    config = production_config()
    config["routes"]["R"]["operations"][0].update(
        unit="Lot", interval_s=1, virtual=False, processing_s=10
    )
    config["toolgroups"]["G"]["CASCADINGTOOL"] = "YES"
    return config


def build_run(monkeypatch, root, config):
    def forbidden(*args, **kwargs):
        raise AssertionError("纯生产不得调用运输/端口/路由")

    monkeypatch.setattr(full, "compile_full_routes", lambda prepared, base: copy.deepcopy(config))
    for name in ("DelayTransport", "FleetTransport", "Port", "route_between", "route_shapes"):
        monkeypatch.setattr(full, name, forbidden)
    summary = full.run_full(config, root / "run")
    return root / "run", summary


@pytest.fixture
def valid_run(monkeypatch, tmp_path):
    run_dir, summary = build_run(monkeypatch, tmp_path, production_config())
    assert summary["status"] == "completed"
    assert summary["completed_lots"] == 2
    return run_dir


@pytest.fixture
def cascading_run(monkeypatch, tmp_path):
    run_dir, summary = build_run(monkeypatch, tmp_path, cascading_config())
    assert summary["status"] == "completed"
    return run_dir


def copy_run(run_dir, root, name):
    target = root / name
    shutil.copytree(run_dir, target)
    return target


def rewrite(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_valid_run_passes_all_checks(valid_run):
    result = verify_production_run(valid_run)
    assert result["passed"] is True, result["errors"]
    assert result["errors"] == []
    assert result["counts"]["lot.completed"] == 2
    assert result["counts"]["batch.formed"] == 1
    assert result["counts"]["delay.start"] == 2
    assert result["counts"]["input_tokens_open"] == 0
    assert result["counts"]["processing_open"] == 0
    names = {check["name"] for check in result["checks"]}
    assert {
        "summary.counts",
        "summary.processed_steps",
        "transfer.pairing",
        "transfer.input_token",
        "processing.pairing",
        "scope.no_logistics_state",
    } <= names


def test_cascading_modules_are_paired(cascading_run):
    result = verify_production_run(cascading_run)
    assert result["passed"] is True, result["errors"]
    assert result["counts"]["module.enter"] > 0
    assert result["counts"]["module_open"] == 0


def test_tampered_processing_start_before_arrival_rejected(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "tampered-arrival")
    events = json.loads((case / "events.json").read_text("utf-8"))
    start = next(event for event in events if event["kind"] == "processing.start")
    start["equipment"] = "E"
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("processing.arrived_at_target") for error in result["errors"])


def test_tampered_summary_counts_rejected(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "tampered-summary")
    summary = json.loads((case / "summary.json").read_text("utf-8"))
    summary["completed_lots"] += 1
    rewrite(case / "summary.json", summary)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("summary.counts") for error in result["errors"])


def test_invalid_batches_rejected(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "invalid-batch")
    events = json.loads((case / "events.json").read_text("utf-8"))
    position = next(i for i, event in enumerate(events)
                    if event["kind"] == "lot.released") + 1
    common = dict(kind="batch.formed", equipment="M", source_step=1,
                  minimum=50, maximum=50, underfilled=False)
    events[position:position] = [
        {**common, "time": events[position - 1]["time"],
         "lot_ids": ["L0", "L0"], "wafers": 50},
        {**common, "time": events[position - 1]["time"],
         "lot_ids": ["L0", "L1"], "wafers": 999},
    ]
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("batch.identity") for error in result["errors"])
    assert any(error.startswith("batch.wafer_bounds") for error in result["errors"])


def test_logistics_events_rejected(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "logistics-event")
    events = json.loads((case / "events.json").read_text("utf-8"))
    position = next(i for i, event in enumerate(events)
                    if event["kind"] == "lot.released") + 1
    events.insert(position, dict(time=events[position - 1]["time"],
                                 kind="transport.requested", lot_id="L0", task_id="t0"))
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("events.forbidden_kinds") for error in result["errors"])


def test_gzip_event_log_supported(valid_run, tmp_path):
    case = tmp_path / "gzip-run"
    case.mkdir()
    for name in ("config.json", "summary.json"):
        shutil.copy(valid_run / name, case / name)
    with gzip.open(case / "events.json.gz", "wb") as stream:
        stream.write((valid_run / "events.json").read_bytes())
    result = verify_production_run(case)
    assert result["passed"] is True, result["errors"]


def test_transfer_end_must_match_grid_exactly(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "tampered-transfer-end")
    events = json.loads((case / "events.json").read_text("utf-8"))
    end = next(event for event in events if event["kind"] == "production.transfer_end")
    end["time"] += 1e-9
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(
        error.startswith("transfer.pairing") and "精确" in error
        for error in result["errors"]
    )


def test_duplicate_transfer_start_rejected_by_token_check(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "duplicate-transfer-start")
    events = json.loads((case / "events.json").read_text("utf-8"))
    position = next(
        i for i, event in enumerate(events) if event["kind"] == "production.transfer_start"
    )
    events.insert(position + 1, dict(events[position]))
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(
        error.startswith("transfer.input_token") and "已持有" in error
        for error in result["errors"]
    )


def test_duplicate_processing_start_rejected(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "duplicate-processing-start")
    events = json.loads((case / "events.json").read_text("utf-8"))
    position = next(
        i for i, event in enumerate(events) if event["kind"] == "processing.start"
    )
    events.insert(position + 1, dict(events[position]))
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("processing.pairing") for error in result["errors"])


def test_module_overlap_rejected(cascading_run, tmp_path):
    case = copy_run(cascading_run, tmp_path, "module-overlap")
    events = json.loads((case / "events.json").read_text("utf-8"))
    position = next(
        i for i, event in enumerate(events) if event["kind"] == "module.enter"
    )
    intruder = dict(events[position])
    intruder["lot_id"] = next(
        lot_id for lot_id in ("L0", "L1") if lot_id != intruder["lot_id"]
    )
    events.insert(position + 1, intruder)
    rewrite(case / "events.json", events)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("module.pairing") for error in result["errors"])


def test_logistics_state_fields_rejected(valid_run, tmp_path):
    case = copy_run(valid_run, tmp_path, "tasks-in-config")
    config = json.loads((case / "config.json").read_text("utf-8"))
    config["tasks"] = [{"id": "t0"}]
    rewrite(case / "config.json", config)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("scope.no_logistics_state") for error in result["errors"])

    case = copy_run(valid_run, tmp_path, "vehicle-in-summary")
    summary = json.loads((case / "summary.json").read_text("utf-8"))
    summary["vehicle_count"] = 1
    rewrite(case / "summary.json", summary)
    result = verify_production_run(case)
    assert result["passed"] is False
    assert any(error.startswith("scope.no_logistics_state") for error in result["errors"])


def test_cli_writes_new_report_and_refuses_overwrite(valid_run, tmp_path):
    report = tmp_path / "reports" / "production-verification.json"
    assert main([str(valid_run), "--report", str(report)]) == 0
    assert json.loads(report.read_text("utf-8"))["passed"] is True
    assert main([str(valid_run), "--report", str(report)]) == 2

def test_per_lot_terminal_swap_is_rejected(valid_run,tmp_path):
    case=copy_run(valid_run,tmp_path,'wrong-terminal')
    summary=json.loads((case/'summary.json').read_text('utf-8'))
    summary['lot_results'][0]['phase']='waiting_tool'
    rewrite(case/'summary.json',summary)
    result=verify_production_run(case)
    assert not result['passed']
    assert any(error.startswith('summary.lot_terminal') for error in result['errors'])


def test_missing_predecessor_cannot_be_hidden_by_matching_transfer(valid_run,tmp_path):
    case=copy_run(valid_run,tmp_path,'missing-predecessor')
    events=json.loads((case/'events.json').read_text('utf-8'))
    events=[e for e in events if not (e.get('lot_id')=='L0' and e.get('source_step')==1)]
    rewrite(case/'events.json',events)
    result=verify_production_run(case)
    assert not result['passed']
    assert any(error.startswith('route.sequence') for error in result['errors'])
