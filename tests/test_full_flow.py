"""专用两步测试工艺，强制末工序返工；不将测试输入当作真实完整路线。"""

import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes


@pytest.mark.parametrize("lean", [False, True])
def test_terminal_rework_returns_and_transports_before_completion(monkeypatch, tmp_path, lean):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (root / config["network"]["workbook"]).exists():
        pytest.skip("缺少原始路网")
    resolved = compile_full_routes(config, root)
    a = resolved["routes"]["Route_Product_1"]["operations"][369]
    b = resolved["routes"]["Route_Product_1"]["operations"][370]
    a, b = copy.deepcopy(a), copy.deepcopy(b)
    for index, op in enumerate((a, b), 1):
        op.update(
            source_step=index,
            processing_s=1,
            offset_s=0,
            interval_s=0,
            sample_probability=1,
            rework_probability=1 if index == 2 else 0,
            rework_step=1,
            dedication_step=0,
            cqt_step=0,
        )
    resolved["routes"]["Route_Product_1"]["operations"] = [a, b]
    resolved["lots"] = resolved["lots"][:1]
    resolved["max_time_s"] = 10000
    if lean:
        resolved["replay"] = {"enabled": False}
        resolved["performance"] = {
            "stream_events": True, "route_event_geometry": False, "compact_json": True,
        }
    monkeypatch.setattr(full, "compile_full_routes", lambda *args: resolved)
    instances = []

    class Decisions:
        def __init__(self, seed):
            self.purpose = len(instances) % 3
            self.calls = 0
            instances.append(self)

        def random(self):
            self.calls += 1
            # 第一次末道触发返工；重复末道不再触发。
            return 0 if self.purpose == 0 or self.purpose == 1 and self.calls == 2 else 1

        def uniform(self, low, high):
            return (low + high) / 2

    monkeypatch.setattr(full.random, "Random", Decisions)
    output = tmp_path / "terminal-rework"
    summary = full.run_full(resolved, output)
    events = json.loads((output / "events.json").read_text("utf-8"))
    assert [e["source_step"] for e in events if e["kind"] == "processing.start"] == [1, 2, 1, 2]
    assert summary["reworks"] == 1 and summary["completed_lots"] == 1
    assert summary["completed_operations"] == 4 and summary["completed_tasks"] == 5
    completed = next(e["time"] for e in events if e["kind"] == "lot.completed")
    deliveries = [e["time"] for e in events if e["kind"] == "transport.delivered"]
    assert completed == deliveries[-1]
    assert summary["collisions"] == summary["teleports"] == []
