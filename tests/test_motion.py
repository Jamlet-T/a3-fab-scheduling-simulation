"""高频轨迹原值、停车保留与分块交界。"""

import json
from pathlib import Path

from fabcosim.transport.fleet import FleetTransport
from fabcosim.transport.recording import RECORD, MotionRecorder


def test_motion_is_lossless_and_static_gap_is_not_filled(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/multi_merge.json").read_text("utf-8"))
    config["vehicles"] = config["vehicles"][:1]
    engine = FleetTransport(config, tmp_path, lambda *a, **k: None)
    try:
        recorder = MotionRecorder(tmp_path, engine, 0.1, 600)

        def row(x, speed, phase, task):
            return {
                "id": engine.ids[0],
                "edge": "e0",
                "position_m": x,
                "x": x,
                "y": 0,
                "speed": speed,
                "distance_m": x - 2,
                "phase": phase,
                "task_id": task,
                "lot_id": None,
                "waiting_reason": None,
                "zone_wait": None,
            }

        inputs = [
            (0.1, row(2, 1.5, "empty", "a")),
            (0.2, row(2.15, 1.5, "empty", "a")),
            (0.3, row(2.2, 0, "parked", None)),
            (100, row(2.2, 0, "parked", None)),
            (600.1, row(2.2, 0, "parked", None)),
            (600.2, row(2.2, 0, "empty", "b")),
            (600.3, row(2.22, 0.2, "empty", "b")),
        ]
        for time, sample in inputs:
            recorder.record(time, [sample])
        recorder.close()
        index = json.loads((tmp_path / "motion-index.json").read_text("utf-8"))
        assert index["record_bytes"] == 60 and len(index["chunks"]) == 2
        blocks = [
            list(RECORD.iter_unpack((tmp_path / chunk["file"]).read_bytes()))
            for chunk in index["chunks"]
        ]
        assert [r[0] for r in blocks[0]] == [0.1, 0.2, 0.3]
        assert [r[0] for r in blocks[1]] == [0.3, 600.2, 600.3]
        assert blocks[0][-1] == blocks[1][0]
        assert blocks[1][-1][4:] == (2.22, 0, 0.2, 2.22 - 2, 2.22)
        assert index["states"][blocks[1][1][3]]["task_id"] == "b"
    finally:
        engine.close()
