"""断点续算IO：事件日志分段合并与统计缓存快照/恢复。

事件日志用``snapshot``封口分段、``restore``复用封口前缀，最终``close``必须与同事件
一次性``EventLog``逐字节相同；统计缓存``snapshot``只flush不finalize，恢复后继续
observe/finalize的统计必须与不中断运行完全一致。
"""

import gzip
import hashlib
import json
import pickle
import sqlite3
from pathlib import Path

import pytest

from fabcosim.cosim import checkpoint_io
from fabcosim.cosim.checkpoint_io import CheckpointEventLog
from fabcosim.cosim.event_log import EventLog
from fabcosim.cosim.metric_history import MetricHistory

EVENTS = [
    {"time": 0.0, "kind": "lot.released", "lot_id": "lot0"},
    {"time": 1e-09, "kind": "destination.selected", "equipment": "设备A"},
    {"time": 0.1 + 0.2, "kind": "task.assigned", "vehicle_id": "oht_0", "priority": None},
    {"time": 2.5, "kind": "transport.requested", "task_id": "T1"},
    {"time": 3.5, "kind": "transport.pickup_start", "task_id": "T1"},
]

COMPRESSIONS = [("none", 1), ("gzip", 1), ("gzip", 6)]


def one_shot(path: Path, events, compression: str, level: int) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = EventLog(path, compression, level)
    for event in events:
        log.append(event)
    log.close()
    return path.read_bytes()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 事件日志分段
@pytest.mark.parametrize(("compression", "level"), COMPRESSIONS)
def test_snapshotted_parts_merge_to_the_one_shot_bytes(tmp_path, compression, level):
    log = CheckpointEventLog(tmp_path / "run" / "events.json", compression, level)
    for event in EVENTS[:2]:
        log.append(event)
    state = log.snapshot()
    assert state["count"] == 2 and len(state["parts"]) == 1
    for event in EVENTS[2:]:
        log.append(event)
    assert log.snapshot()["count"] == len(EVENTS)
    log.close()

    expected = one_shot(tmp_path / "expected" / "events.json", EVENTS, compression, level)
    assert log.path.read_bytes() == expected
    if compression == "gzip":
        assert gzip.decompress(log.path.read_bytes()) == gzip.decompress(expected)


@pytest.mark.parametrize(("compression", "level"), COMPRESSIONS)
def test_part_is_a_complete_json_array_after_snapshot(tmp_path, compression, level):
    log = CheckpointEventLog(tmp_path / "run" / "events.json", compression, level)
    for event in EVENTS[:2]:
        log.append(event)
    part = Path(log.snapshot()["parts"][0]["path"])
    payload = part.read_bytes()
    if compression == "gzip":
        payload = gzip.decompress(payload)
    assert json.loads(payload) == EVENTS[:2]
    # snapshot本身不落最终日志，也不创建空的新part。
    assert not log.path.exists()
    suffix = ".gz" if compression == "gzip" else ""
    assert [path.name for path in log.part_dir.iterdir()] == [f"part-000000.json{suffix}"]


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_cross_chunk_brackets_and_empty_segments(tmp_path, monkeypatch, compression):
    monkeypatch.setattr(checkpoint_io, "COPY_CHUNK_BYTES", 3)
    log = CheckpointEventLog(tmp_path / "run" / "events.json", compression)
    for event in EVENTS[:2]:
        log.append(event)
    log.snapshot()
    for event in EVENTS[2:]:
        log.append(event)
    log.close()
    assert log.path.read_bytes() == one_shot(
        tmp_path / "expected" / "events.json", EVENTS, compression, 1
    )


def test_snapshot_is_repeatable_picklable_and_lazy(tmp_path):
    log = CheckpointEventLog(tmp_path / "run" / "events.json")
    for event in EVENTS[:2]:
        log.append(event)
    first = log.snapshot()
    part_files = sorted(path.name for path in log.part_dir.iterdir())
    second = log.snapshot()
    assert second == first
    assert sorted(path.name for path in log.part_dir.iterdir()) == part_files
    assert pickle.loads(pickle.dumps(second)) == second
    # 封口后不立刻开新part，下一次append才懒开。
    log.append(EVENTS[2])
    assert len(list(log.part_dir.iterdir())) == len(part_files) + 1
    log.close()
    log.close()
    assert log.path.read_bytes() == one_shot(
        tmp_path / "expected" / "events.json", EVENTS[:3], "none", 1
    )


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_empty_log_merges_to_an_empty_array(tmp_path, compression):
    log = CheckpointEventLog(tmp_path / "run" / "events.json", compression)
    state = log.snapshot()
    assert state["count"] == 0 and state["parts"] == []
    log.close()
    assert log.path.read_bytes() == one_shot(
        tmp_path / "expected" / "events.json", [], compression, 1
    )


def test_merge_skips_a_sealed_empty_part(tmp_path):
    """手工造一个``[]``段：合并必须跳过它，不能写出``[,``这样的非法数组。"""
    part = tmp_path / "source" / "part-000000.json"
    part.parent.mkdir()
    empty = EventLog(part)
    empty.close()
    state = {
        "version": 1,
        "compression": "none",
        "level": 1,
        "target": str(part.parent / "events.json"),
        "count": 0,
        "parts": [
            {
                "path": str(part.resolve()),
                "sha256": sha256(part),
                "bytes": part.stat().st_size,
                "count": 0,
            }
        ],
    }
    log = CheckpointEventLog.restore(state, tmp_path / "run" / "events.json")
    for event in EVENTS:
        log.append(event)
    log.close()
    assert log.path.read_bytes() == one_shot(
        tmp_path / "expected" / "events.json", EVENTS, "none", 1
    )


# ---------------------------------------------------------------- 事件日志恢复
@pytest.mark.parametrize(("compression", "level"), COMPRESSIONS)
def test_restore_reuses_prefix_without_touching_the_old_run(tmp_path, compression, level):
    original = CheckpointEventLog(tmp_path / "run1" / "events.json", compression, level)
    for event in EVENTS[:2]:
        original.append(event)
    state = original.snapshot()
    old_parts = {item["path"]: Path(item["path"]).read_bytes() for item in state["parts"]}

    resumed = CheckpointEventLog.restore(state, tmp_path / "run2" / "events.json")
    assert resumed.count == 2
    for event in EVENTS[2:]:
        resumed.append(event)
    resumed.close()

    assert resumed.path.read_bytes() == one_shot(
        tmp_path / "expected" / "events.json", EVENTS, compression, level
    )
    for path, content in old_parts.items():
        assert Path(path).read_bytes() == content
    assert not original.path.exists()
    # 新run的追加只落在自己的part目录里。
    assert len(list(resumed.part_dir.iterdir())) == 1


def test_restore_rejects_a_tampered_part(tmp_path):
    log = CheckpointEventLog(tmp_path / "run" / "events.json")
    log.append(EVENTS[0])
    state = log.snapshot()
    part = Path(state["parts"][0]["path"])
    data = bytearray(part.read_bytes())
    data[-2] ^= 0x01  # 保持字节数不变，只能靠sha256发现
    part.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="校验失败"):
        CheckpointEventLog.restore(state, tmp_path / "new" / "events.json")


def test_restore_rejects_a_truncated_or_missing_part(tmp_path):
    log = CheckpointEventLog(tmp_path / "run" / "events.json")
    log.append(EVENTS[0])
    state = log.snapshot()
    part = Path(state["parts"][0]["path"])
    part.write_bytes(part.read_bytes()[:-1])
    with pytest.raises(ValueError, match="字节数"):
        CheckpointEventLog.restore(state, tmp_path / "new" / "events.json")

    part.unlink()
    with pytest.raises(FileNotFoundError, match="part缺失"):
        CheckpointEventLog.restore(state, tmp_path / "new" / "events.json")


def test_restore_rejects_inconsistent_count_and_same_part_directory(tmp_path):
    log = CheckpointEventLog(tmp_path / "run" / "events.json")
    log.append(EVENTS[0])
    state = log.snapshot()
    broken = dict(state, count=2)
    with pytest.raises(ValueError, match="不一致"):
        CheckpointEventLog.restore(broken, tmp_path / "new" / "events.json")
    # 目标与源part同目录等于往旧run里追加，必须拒绝。
    with pytest.raises(ValueError, match="同目录"):
        CheckpointEventLog.restore(state, tmp_path / "run" / "events.json")


# ---------------------------------------------------------------- 事件日志边界
def test_existing_target_and_part_files_are_not_overwritten(tmp_path):
    target = tmp_path / "run" / "events.json"
    target.parent.mkdir()
    target.write_bytes(b"keep-me")
    with pytest.raises(FileExistsError):
        CheckpointEventLog(target)
    assert target.read_bytes() == b"keep-me"

    log = CheckpointEventLog(tmp_path / "other" / "events.json")
    log.part_dir.mkdir(parents=True)
    (log.part_dir / "part-000000.json").write_bytes(b"[]")
    with pytest.raises(FileExistsError):
        log.append(EVENTS[0])
    assert (log.part_dir / "part-000000.json").read_bytes() == b"[]"


def test_unknown_compression_and_level_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="event_compression"):
        CheckpointEventLog(tmp_path / "events.json", "zstd")
    with pytest.raises(ValueError, match="event_compression_level"):
        CheckpointEventLog(tmp_path / "events.json.gz", "gzip", 10)


def test_append_after_close_is_rejected(tmp_path):
    log = CheckpointEventLog(tmp_path / "run" / "events.json")
    log.append(EVENTS[0])
    log.close()
    with pytest.raises(RuntimeError, match="close"):
        log.append(EVENTS[1])
    assert log.count == 1


# ---------------------------------------------------------------- 统计缓存快照
def ev(kind: str, time: float, **fields) -> dict:
    return {"time": time, "kind": kind, **fields}


METRIC_EVENTS = [
    ev("transport.requested", 0.0, task_id="A"),
    ev("transport.pickup_start", 1.5, task_id="A"),
    ev("processing.start", 2.0, equipment="TOOL_A"),
    ev("transport.requested", 3.0, task_id="B"),
    ev("cqt.checked", 4.0, violated=True),
    ev("transport.pickup_start", 5.0, task_id="B"),
    ev("processing.end", 6.0, equipment="TOOL_A"),
    ev("transport.requested", 7.0, task_id="C"),
    ev("availability.down", 8.0, duration_s=2.5),
    ev("batch.formed", 9.0, underfilled=True),
    ev("lot.released", 10.0, lot_id="L1"),
    ev("transport.pickup_start", 11.0, task_id="C"),
    ev("lot.completed", 12.0, lot_id="L1"),
    ev("delay.end", 13.0),
    ev("lot.rework", 14.0),
    ev("batch.formed", 15.0, underfilled=False),
    ev("cqt.checked", 16.0, violated=False),
    ev("processing.start", 17.0, equipment="TOOL_B"),
    ev("processing.end", 18.0, equipment="TOOL_B"),
]
ASSETS = {"TOOL_A": {"tool_group": "G1"}, "TOOL_B": {"tool_group": "G2"}}
SPLIT = 11


def results(history: MetricHistory) -> dict:
    return {
        "wait_0_20": history.transport_wait(0.0, 20.0),
        "wait_5_15": history.transport_wait(5.0, 15.0),
        "cqt_0_20": history.cqt_count(0.0, 20.0),
        "summary": history.summary_counts(ASSETS),
        "lot_events": history.lot_events,
        "request_count": history.request_count,
    }


def test_snapshot_flushes_without_finalizing_or_right_censoring(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite3")
    for event in METRIC_EVENTS[:SPLIT]:
        history.observe(event)
    state = history.snapshot(tmp_path / "snapshot.sqlite3")

    # C在快照时仍未取货：备份里没有它的右删失行，也没有提前finalize。
    with sqlite3.connect(state["backup_path"]) as connection:
        rows = connection.execute(
            "SELECT seq, requested, pickup FROM transport_wait ORDER BY seq"
        ).fetchall()
    assert rows == [(1, 0.0, 1.5), (2, 3.0, 5.0)]
    assert state["pending"] == {"C": (3, 7.0)}
    assert state["finalized"] is False and state["finalizing"] is False
    assert state["lot_events"] == [("lot.released", 10.0, "L1")]
    with pytest.raises(RuntimeError, match="finalize"):
        history.transport_wait(0, 20)

    # snapshot不改变原对象的后续行为：C可以正常配对，不会留下右删失。
    history.observe(ev("transport.pickup_start", 11.0, task_id="C"))
    history.finalize()
    assert history.transport_wait(0, 20)["right_censored_count"] == 0
    history.close()


def test_snapshot_flushes_buffered_rows_into_the_backup(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite3", batch_size=64)
    history.observe(ev("transport.requested", 1.0, task_id="A"))
    history.observe(ev("transport.pickup_start", 2.5, task_id="A"))
    assert history._rows  # 未达批次阈值，仍在内存缓冲
    state = history.snapshot(tmp_path / "snapshot.sqlite3")
    with sqlite3.connect(state["backup_path"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transport_wait").fetchone()[0] == 1
    assert history._rows == []
    # 同一对象可再次快照；缓冲区已清空且不会重复写行。
    second = history.snapshot(tmp_path / "snapshot2.sqlite3")
    with sqlite3.connect(second["backup_path"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transport_wait").fetchone()[0] == 1
    history.finalize()
    assert history.transport_wait(0, 10)["requested_count"] == 1
    history.close()


def test_resumed_history_matches_an_uninterrupted_run_exactly(tmp_path):
    continuous = MetricHistory(tmp_path / "continuous.sqlite3", batch_size=2)
    for event in METRIC_EVENTS:
        continuous.observe(event)
    continuous.finalize()
    expected = results(continuous)
    continuous.close()

    interrupted = MetricHistory(tmp_path / "interrupted.sqlite3", batch_size=2)
    for event in METRIC_EVENTS[:SPLIT]:
        interrupted.observe(event)
    state = interrupted.snapshot(tmp_path / "snapshot.sqlite3")
    source_sha = sha256(Path(state["source_path"]))
    backup_sha = sha256(Path(state["backup_path"]))
    interrupted.close()

    resumed = MetricHistory.restore(state, tmp_path / "resumed.sqlite3")
    assert resumed.pending_requests == {"C": (3, 7.0)}
    assert resumed.request_count == 3
    assert resumed.lot_events == [("lot.released", 10.0, "L1")]
    for event in METRIC_EVENTS[SPLIT:]:
        resumed.observe(event)
    resumed.finalize()
    assert results(resumed) == expected
    resumed.close()

    # 恢复只读旧库、只写新库。
    assert sha256(Path(state["source_path"])) == source_sha
    assert sha256(Path(state["backup_path"])) == backup_sha
    with sqlite3.connect(tmp_path / "continuous.sqlite3") as left, sqlite3.connect(
        tmp_path / "resumed.sqlite3"
    ) as right:
        for table, columns in (
            ("transport_wait", "seq, requested, pickup, wait"),
            ("cqt_violations", "time"),
            ("availability_down", "duration"),
        ):
            query = f"SELECT {columns} FROM {table} ORDER BY rowid"
            assert left.execute(query).fetchall() == right.execute(query).fetchall()


def test_snapshot_state_is_picklable_and_restore_refuses_existing_path(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite3")
    for event in METRIC_EVENTS[:SPLIT]:
        history.observe(event)
    state = history.snapshot(tmp_path / "snapshot.sqlite3")
    assert pickle.loads(pickle.dumps(state)) == state

    existing = tmp_path / "already-there.sqlite3"
    existing.write_bytes(b"keep-me")
    with pytest.raises(FileExistsError):
        history.snapshot(existing)
    with pytest.raises(FileExistsError):
        MetricHistory.restore(state, existing)
    assert existing.read_bytes() == b"keep-me"
    history.close()


def test_restore_rejects_a_tampered_backup(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite3")
    for event in METRIC_EVENTS[:SPLIT]:
        history.observe(event)
    state = history.snapshot(tmp_path / "snapshot.sqlite3")
    history.close()

    backup = Path(state["backup_path"])
    data = bytearray(backup.read_bytes())
    data[-2] ^= 0x01
    backup.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="校验失败"):
        MetricHistory.restore(state, tmp_path / "resumed.sqlite3")
    assert not (tmp_path / "resumed.sqlite3").exists()


def test_closed_history_cannot_snapshot(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite3")
    history.close()
    with pytest.raises(RuntimeError, match="close"):
        history.snapshot(tmp_path / "snapshot.sqlite3")
    assert not (tmp_path / "snapshot.sqlite3").exists()
