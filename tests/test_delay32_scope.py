"""Delay_32范围调整：源身份/约束不漂移，排除不等于加工或抽检跳步。"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_production_only import fixture_config

from fabcosim.cosim import full
from fabcosim.production.scope import apply_production_scope
from fabcosim.production.transfer import ProductionTransferBuffers
from fabcosim.production.verification import verify_production_run


def config():
    c = fixture_config(count=1)
    first, last = c["routes"]["R"]["operations"]
    delay = {
        **first,
        "source_step": 2,
        "tool_group": "Delay_32",
        "virtual": True,
        "processing_s": 100,
    }
    last["source_step"] = 3
    c["routes"]["R"].update(
        operations=[first, delay, last], rpt_s=110, remaining_s={1: 110, 2: 105, 3: 5}
    )
    c["assets"]["S"] = dict(
        kind="buffer", tool_group="STOCKER", input_port="S.in", output_port="S.out"
    )
    c["candidates"]["Delay_32"] = ["S"]
    c["max_time_s"] = 200
    return c


@pytest.fixture
def compiled(monkeypatch):
    monkeypatch.setattr(full, "compile_full_routes", lambda c, root: copy.deepcopy(c))


def test_default_excludes_without_visiting_buffer_or_counting_processing(
    compiled, monkeypatch, tmp_path
):
    reserve = ProductionTransferBuffers.reserve

    def checked(self, lid, target):
        assert target != "S", "已排除步骤不应申请Stocker输入位"
        return reserve(self, lid, target)

    monkeypatch.setattr(ProductionTransferBuffers, "reserve", checked)
    c = config()
    original = copy.deepcopy(c)
    out = tmp_path / "excluded"
    summary = full.run_full(c, out)
    assert summary["completed_lots"] == 1 and summary["end_time_s"] == 16
    assert summary["completed_operations"] == 2 and summary["delay_operations"] == 0
    assert summary["excluded_operations"] == 1
    assert summary["route_step_counts"] == {"R": 3} and summary["execution_route_step_counts"] == {
        "R": 2
    }
    assert summary["lot_results"][0]["processed_steps"] == [1, 3]
    assert summary["lot_results"][0]["excluded_steps"] == [2]
    assert summary["lot_results"][0]["skipped_steps"] == []
    stored = json.loads((out / "config.json").read_text("utf-8"))
    assert [o["source_step"] for o in stored["routes"]["R"]["operations"]] == [1, 2, 3]
    assert stored["routes"]["R"]["rpt_s"] == 10
    assert stored["routes"]["R"]["remaining_s"] == {"1": 10, "2": 5, "3": 5}
    events = json.loads((out / "events.json").read_text("utf-8"))
    assert sum(e["kind"] == "operation.excluded" for e in events) == 1
    assert not any(e.get("equipment") == "S" or e["kind"].startswith("delay.") for e in events)
    assert c == original
    assert verify_production_run(out)["passed"]
    # 核查器不能因“允许排除”就接受缺失审计事件。
    (out / "events.json").write_text(
        json.dumps([e for e in events if e["kind"] != "operation.excluded"]), "utf-8"
    )
    assert not verify_production_run(out)["passed"]


def test_explicit_retain_preserves_old_time(compiled, tmp_path):
    c = config()
    c["production"]["delay32_policy"] = "retain"
    s = full.run_full(c, tmp_path / "retained")
    assert s["end_time_s"] == 118 and s["completed_operations"] == 3
    assert s["delay_operations"] == 1 and s["excluded_operations"] == 0
    assert verify_production_run(tmp_path / "retained")["passed"]


@pytest.mark.parametrize("field", ["dedication_step", "cqt_step", "rework_step"])
def test_reference_to_excluded_stage_rejected_before_output(compiled, tmp_path, field):
    c = config()
    c["routes"]["R"]["operations"][0][field] = 2
    with pytest.raises(ValueError, match="指向被排除"):
        full.run_full(c, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_rework_and_cqt_keep_source_references(compiled, monkeypatch, tmp_path):
    c = config()
    first, _, last = c["routes"]["R"]["operations"]
    first.update(cqt_step=3, cqt_s=50, dedication_step=3)
    last.update(rework_probability=1, rework_step=1)
    instances = []

    class Choices:
        def __init__(self, seed):
            self.purpose = len(instances) % 3
            self.calls = 0
            instances.append(self)

        def random(self):
            self.calls += 1
            return 0 if self.purpose == 0 or self.purpose == 1 and self.calls == 2 else 1

    monkeypatch.setattr(full.random, "Random", Choices)
    s = full.run_full(c, tmp_path / "rework")
    assert s["reworks"] == 1 and s["completed_operations"] == 4
    assert s["lot_results"][0]["processed_steps"] == [1, 3, 1, 3]
    assert s["lot_results"][0]["excluded_steps"] == [2, 2]
    assert s["cqt_violations"] == 0
    assert verify_production_run(tmp_path / "rework")["passed"]


def test_joint_scope_keeps_input_identical():
    c = config()
    c["transport"]["model"] = "delay"
    original = copy.deepcopy(c)
    apply_production_scope(c)
    assert c == original


def test_new_scope_resumes_exactly_in_new_process(compiled, tmp_path):
    c = config()
    c["performance"]["stream_events"] = True
    baseline = tmp_path / "baseline"
    full.run_full(copy.deepcopy(c), baseline)
    c["checkpoint"] = {"interval_s": 8, "stop_after_save": True}
    paused = tmp_path / "paused"
    s = full.run_full(c, paused)
    assert s["status"] == "checkpoint_saved"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fabcosim",
            "full",
            "--resume",
            str(paused / "checkpoints/latest.json"),
            "--output",
            str(tmp_path / "resumed"),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ["events.json", "summary.json"]:
        assert (baseline / name).read_bytes() == (tmp_path / "resumed" / name).read_bytes()
