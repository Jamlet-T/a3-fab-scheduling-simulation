"""真实连续工艺片段：同一lot重入、交付因果、源时间与机台互斥。"""

import json
from pathlib import Path

import openpyxl
import pytest

from fabcosim.cosim.multi import run_multi


@pytest.fixture(scope="module")
def reentrant_run(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/smat_reentrant.json").read_text("utf-8"))
    sources = [config["network"]["workbook"], config["routes"]["product1_segment"]["source_csv"]]
    if not all((root / path).exists() for path in sources):
        pytest.skip("缺少SMAT路网或冻结生产路线")
    output = tmp_path_factory.mktemp("reentrant") / "run"
    summary = run_multi(config, output)
    return (
        root,
        summary,
        {
            name: json.loads((output / f"{name}.json").read_text("utf-8"))
            for name in ("config", "events", "frames")
        },
    )


def test_same_lot_returns_after_actual_delivery(reentrant_run):
    _, summary, data = reentrant_run
    events = data["events"]
    assert summary["completed_lots"] == 3 and summary["completed_tasks"] == 6
    assert summary["completed_operations"] == 9 and summary["reentries"] == 3
    assert summary["collisions"] == summary["teleports"] == []
    for lot_id in ("lot0", "lot1", "lot2"):
        lot_events = [e for e in events if e.get("lot_id") == lot_id]
        starts = [e for e in lot_events if e["kind"] == "processing.start"]
        ends = [e for e in lot_events if e["kind"] == "processing.end"]
        assert [e["source_step"] for e in starts] == [370, 371, 372]
        assert [e["equipment"] for e in starts] == ["DIE0134", "DIE0104", "DIE0134"]
        assert sum(e["kind"] == "lot.released" for e in lot_events) == 1
        assert sum(e["kind"] == "lot.completed" for e in lot_events) == 1
        for index in range(2):
            task_events = [e for e in lot_events if e.get("task_id") == f"{lot_id}.move{index + 1}"]
            ready = next(e["time"] for e in task_events if e["kind"] == "task.ready")
            delivered = next(e["time"] for e in task_events if e["kind"] == "transport.delivered")
            assert ends[index]["time"] <= ready < delivered <= starts[index + 1]["time"]
            assert sum(e["kind"] == "task.assigned" for e in task_events) == 1
        assert [round(b["time"] - a["time"], 1) for a, b in zip(starts, ends)] == [
            2989.5,
            2018.2,
            2368.1,
        ]
    for frame in data["frames"]:
        assert {lot["id"] for lot in frame["lots"]} == {"lot0", "lot1", "lot2"}
    assert all(
        lot["location"] == "completed" and lot["completed_operations"] == 3
        for lot in data["frames"][-1]["lots"]
    )


def test_shared_equipment_and_ports_remain_exclusive(reentrant_run):
    _, _, data = reentrant_run
    active, reservations = {}, {}
    for event in data["events"]:
        if event["kind"] == "processing.start":
            assert event["equipment"] not in active
            active[event["equipment"]] = (event["lot_id"], event["source_step"])
        elif event["kind"] == "processing.end":
            assert active.pop(event["equipment"]) == (event["lot_id"], event["source_step"])
        elif event["kind"] == "task.assigned":
            assert event["port"] not in reservations
            reservations[event["port"]] = event["task_id"]
        elif event["kind"] == "port.released":
            assert reservations.pop(event["port"]) == event["task_id"]
    assert active == reservations == {}


def test_processing_rows_match_frozen_workbook(reentrant_run):
    root, _, data = reentrant_run
    manifest = json.loads((root / "03-数据/基准-v1/manifest.json").read_text("utf-8"))
    source = next(s["path"] for s in manifest["sources"] if "SMT_2020_Model_Data" in s["path"])
    wb = openpyxl.load_workbook(root / source, read_only=True, data_only=True)
    try:
        sheet = wb["Route_Product_1"]
        header = next(sheet.iter_rows(values_only=True))
        rows = list(sheet.iter_rows(min_row=371, max_row=373, values_only=True))
        for op, row in zip(data["config"]["routes"]["product1_segment"]["operations"], rows):
            original = dict(zip(header, row))
            assert op["source_row"] == int(original["STEP"]) + 1
            assert op["tool_group"] == original["TOOLGROUP"]
            assert op["processing_s"] == pytest.approx(float(original["MEAN"]) * 60)
            assert op["source_record"]["CASCADING INTERVAL"] == str(original["CASCADING INTERVAL"])
    finally:
        wb.close()
