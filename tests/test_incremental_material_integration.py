"""可选增量审计不改变生产事件，every_step仍强制全量复查。"""

import copy
import json

import pytest
from test_active_lot_counts import scenario

from fabcosim.cosim import full


@pytest.mark.parametrize("observation", [False, True])
@pytest.mark.parametrize("port_index", [False, True])
def test_incremental_audit_preserves_complete_events_and_summary(monkeypatch, tmp_path, observation, port_index):
    config = scenario([0, 100000], max_time_s=200000)
    if observation:
        config["observation"] = {"warmup_s": 0, "end_s": 200000}
    monkeypatch.setattr(full, "compile_full_routes", lambda settings, root: settings)
    old = tmp_path / "full"
    new = tmp_path / "incremental"
    full.run_full(copy.deepcopy(config), old)
    config["performance"]["incremental_material_audit"] = True
    config["performance"]["material_audit_port_index"] = port_index
    full.run_full(copy.deepcopy(config), new)
    for name in ("events.json", "summary.json"):
        assert (old / name).read_bytes() == (new / name).read_bytes()
    counts = json.loads((new / "material-audit.json").read_text("utf-8"))
    assert counts["full_checks"] >= 3  # 初始化、跨24小时原检查边界、终态
    assert counts["checks"] > counts["full_checks"]
    if port_index:
        ports = len(json.loads((new / "config.json").read_text("utf-8"))["ports"])
        assert counts["ports_examined"] < counts["checks"] * ports


def test_every_step_keeps_full_checks(monkeypatch, tmp_path):
    config = scenario([0, 100000], max_time_s=50)
    config["observation"] = {"warmup_s": 0, "end_s": 50}
    config["performance"].update(incremental_material_audit=True, material_audit="every_step")
    monkeypatch.setattr(full, "compile_full_routes", lambda settings, root: settings)
    full.run_full(config, tmp_path / "run")
    counts = json.loads((tmp_path / "run/material-audit.json").read_text("utf-8"))
    assert counts["full_checks"] == counts["checks"]


def test_audit_flag_requires_boolean(tmp_path):
    config = scenario([0])
    config["performance"]["incremental_material_audit"] = "true"
    with pytest.raises(ValueError, match="必须为布尔值"):
        full.run_full(config, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()
