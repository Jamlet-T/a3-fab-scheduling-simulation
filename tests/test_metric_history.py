"""MetricHistory与既有window_metrics的严格对照及边界测试。"""

import json
import sqlite3

import pytest

from fabcosim.cosim.metric_history import MetricHistory
from fabcosim.cosim.metrics import window_metrics


def ev(kind: str, time: float, **fields) -> dict:
    return {"time": time, "kind": kind, **fields}


def payload(value) -> bytes:
    # 不排序键：同时校验字典键的插入顺序与window_metrics一致。
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def reference(events: list[dict], start: float, end: float) -> dict:
    config = {"observation": {"warmup_s": start, "end_s": end}, "lots": [], "routes": []}
    return window_metrics(config, events, {})


def build(path, events, batch_size=4096):
    history = MetricHistory(path, batch_size=batch_size)
    for event in events:
        history.observe(event)
    history.finalize()
    return history


TRANSPORT_EVENTS = [
    ev("transport.requested", 0.0, task_id="A"),  # == start，窗口内排除
    ev("transport.pickup_start", 1.0, task_id="A"),
    ev("transport.requested", 2.0, task_id="B"),
    ev("transport.pickup_start", 2.0, task_id="B"),  # 零等待
    ev("transport.requested", 5.0, task_id="C"),
    ev("transport.pickup_start", 12.0, task_id="C"),  # 在(0,10)右删失
    ev("transport.requested", 10.0, task_id="D"),  # == end，窗口内保留
    ev("transport.pickup_start", 10.0, task_id="D"),
    ev("transport.requested", 20.0, task_id="E"),  # 未取货，由finalize写NULL
    ev("transport.requested", 8.0, task_id="F"),
    ev("transport.pickup_start", 9.5, task_id="F"),
]


@pytest.mark.parametrize(
    ("start", "end"),
    [(0.0, 10.0), (5.0, 15.0), (2.0, 10.0), (8.0, 8.5), (100.0, 200.0), (0.0, 100.0)],
)
def test_transport_wait_matches_window_metrics(tmp_path, start, end):
    history = build(tmp_path / "metrics.sqlite", TRANSPORT_EVENTS, batch_size=2)
    expected = reference(TRANSPORT_EVENTS, start, end)["transport_wait"]
    assert payload(history.transport_wait(start, end)) == payload(expected)
    history.close()


def test_cqt_count_matches_window_metrics(tmp_path):
    events = [
        ev("cqt.checked", 0.5, violated=True),
        ev("cqt.checked", 3.0, violated=True),
        ev("cqt.checked", 5.0, violated=False),
        ev("cqt.checked", 10.0, violated=True),
        ev("cqt.checked", 15.0, violated=True),
    ]
    history = build(tmp_path / "metrics.sqlite", events)
    for start, end in [(0.0, 10.0), (2.0, 3.0), (0.0, 20.0), (20.0, 30.0)]:
        assert history.cqt_count(start, end) == reference(events, start, end)[
            "window_cqt_violations"
        ]
    history.close()


def test_summary_counts_match_event_totals(tmp_path):
    assets = {"TOOL_A": {"tool_group": "G1"}, "TOOL_B": {"tool_group": "G2"}}
    events = [
        ev("processing.start", 1.0, equipment="TOOL_A"),
        ev("processing.start", 2.0, equipment="TOOL_B"),
        ev("processing.start", 3.0, equipment="TOOL_A"),
        ev("processing.end", 4.0, equipment="TOOL_A"),
        ev("processing.end", 5.0, equipment="TOOL_B"),
        ev("delay.end", 6.0),
        ev("batch.formed", 7.0, underfilled=True),
        ev("batch.formed", 8.0, underfilled=False),
        ev("availability.down", 9.0, duration_s=1.5),
        ev("availability.down", 10.0, duration_s=2.5),
        ev("lot.rework", 11.0),
        ev("cqt.checked", 12.0, violated=True),
        ev("cqt.checked", 13.0, violated=False),
        ev("lot.released", 14.0, lot_id="L1"),
        ev("lot.completed", 15.0, lot_id="L1"),
        ev("some.other.event", 16.0),
    ]
    history = build(tmp_path / "metrics.sqlite", events)
    counts = history.summary_counts(assets)
    assert counts == {
        "visited_tool_count": 2,
        "visited_tool_groups": ["G1", "G2"],
        "physical_processing_operations": 2,
        "delay_operations": 1,
        "batch_count": 2,
        "underfilled_batches": 1,
        "availability_outages": 2,
        "availability_down_s": 4.0,
        "reworks": 1,
        "cqt_violations": 1,
    }
    # 与直接按full.py现有口径扫描事件的结果一致。
    assert counts["visited_tool_count"] == len(
        {e["equipment"] for e in events if e["kind"] == "processing.start"}
    )
    assert counts["availability_down_s"] == sum(
        e["duration_s"] for e in events if e["kind"] == "availability.down"
    )
    assert history.lot_events == [("lot.released", 14.0, "L1"), ("lot.completed", 15.0, "L1")]
    history.close()


def test_finalize_writes_censored_rows_and_is_idempotent(tmp_path):
    path = tmp_path / "metrics.sqlite"
    history = MetricHistory(path)
    history.observe(ev("transport.requested", 3.0, task_id="P"))
    history.finalize()
    history.finalize()  # 幂等
    assert history.transport_wait(0.0, 10.0) == {
        "definition": "运输需求提出到实际开始取货，含输出端口等待与空驶取货",
        "requested_count": 1,
        "uncensored": {"count": 0, "mean_s": None, "p50_s": None, "p95_s": None},
        "right_censored_count": 1,
        "all_observed_lower_bound": {"count": 1, "mean_s": 7.0, "p50_s": 7.0, "p95_s": 7.0},
    }
    with pytest.raises(RuntimeError):
        history.observe(ev("processing.end", 11.0))
    history.close()
    history.close()  # 幂等


def test_close_commits_paired_rows_without_fabricating_pending(tmp_path):
    path = tmp_path / "metrics.sqlite"
    history = MetricHistory(path)
    history.observe(ev("transport.requested", 1.0, task_id="PAIRED"))
    history.observe(ev("transport.pickup_start", 2.5, task_id="PAIRED"))
    history.observe(ev("transport.requested", 3.0, task_id="STILL_PENDING"))
    history.close()
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT requested, pickup, wait FROM transport_wait ORDER BY seq"
        ).fetchall()
    assert rows == [(1.0, 2.5, 1.5)]


def test_pending_duplicate_and_unknown_pickup_are_rejected(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite")
    history.observe(ev("transport.requested", 1.0, task_id="T"))
    with pytest.raises(ValueError):
        history.observe(ev("transport.requested", 2.0, task_id="T"))
    with pytest.raises(ValueError):
        history.observe(ev("transport.pickup_start", 3.0, task_id="UNKNOWN"))
    assert history.pending_requests == {"T": (1, 1.0)}
    history.close()


def test_existing_path_is_not_overwritten(tmp_path):
    path = tmp_path / "metrics.sqlite"
    path.write_bytes(b"keep-me")
    with pytest.raises(FileExistsError):
        MetricHistory(path)
    assert path.read_bytes() == b"keep-me"


def test_large_non_integer_times_and_tiny_wait(tmp_path):
    events = [
        ev("transport.requested", 9_000_000.123456789, task_id="LARGE"),
        ev("transport.pickup_start", 9_000_000.12345679, task_id="LARGE"),
    ]
    history = build(tmp_path / "metrics.sqlite", events)
    expected = reference(events, 9_000_000.0, 9_000_000.2)["transport_wait"]
    assert payload(history.transport_wait(9_000_000.0, 9_000_000.2)) == payload(expected)
    assert history.transport_wait(9_000_000.0, 9_000_000.2)["uncensored"]["count"] == 1
    history.close()


class _FailOnceConnection:
    """代理连接：指定批次的executemany先真实插入若干行，再抛OperationalError。"""

    def __init__(self, real, fail_calls=(1,), fail_before=1):
        self._real = real
        self._fail_calls = set(fail_calls)
        self._fail_before = fail_before
        self.calls = 0

    def executemany(self, sql, rows):
        self.calls += 1
        rows = list(rows)
        if self.calls in self._fail_calls:
            self._real.executemany(sql, rows[: self._fail_before])
            raise sqlite3.OperationalError("注入的批量写失败")
        self._real.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_flush_rolls_back_partial_batch_and_retry_has_no_duplicates(tmp_path):
    path = tmp_path / "metrics.sqlite"
    history = MetricHistory(path, batch_size=16)
    for index, (requested, pickup) in enumerate([(1.0, 2.5), (3.0, 4.0), (5.0, 9.5)], 1):
        history.observe(ev("transport.requested", requested, task_id=f"T{index}"))
        history.observe(ev("transport.pickup_start", pickup, task_id=f"T{index}"))

    real = history._connection
    history._connection = _FailOnceConnection(real, fail_calls=(1,), fail_before=1)
    # 第一行已实际插入后抛错。未回滚时重试会撞seq主键；回滚后重试成功且不重复。
    with pytest.raises(sqlite3.OperationalError):
        history._flush()
    history._connection = real
    history._flush()
    assert history._rows == []
    history.close()

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT seq, requested, pickup, wait FROM transport_wait ORDER BY seq"
        ).fetchall()
    assert rows == [(1, 1.0, 2.5, 1.5), (2, 3.0, 4.0, 1.0), (3, 5.0, 9.5, 4.5)]


def test_finalize_retry_after_failure_keeps_censored_rows_exact_once(tmp_path):
    path = tmp_path / "metrics.sqlite"
    history = MetricHistory(path, batch_size=2)
    for index in range(1, 6):
        history.observe(ev("transport.requested", float(index), task_id=f"P{index}"))

    real = history._connection
    history._connection = _FailOnceConnection(real, fail_calls=(1,), fail_before=1)
    with pytest.raises(sqlite3.OperationalError):
        history.finalize()
    # 失败时已进入finalizing：observe必须拒绝；该批pending必须保留。
    with pytest.raises(RuntimeError):
        history.observe(ev("transport.requested", 99.0, task_id="LATE"))
    assert set(history.pending_requests) == {"P1", "P2", "P3", "P4", "P5"}

    history._connection = real
    history.finalize()
    history.finalize()  # 成功后仍幂等
    assert history.pending_requests == {}
    history.close()

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT seq, requested, pickup, wait FROM transport_wait ORDER BY seq"
        ).fetchall()
    assert rows == [
        (1, 1.0, None, None),
        (2, 2.0, None, None),
        (3, 3.0, None, None),
        (4, 4.0, None, None),
        (5, 5.0, None, None),
    ]


def test_query_cannot_silently_omit_unfinalized_requests(tmp_path):
    history = MetricHistory(tmp_path / "metrics.sqlite")
    history.observe(ev("transport.requested", 1.0, task_id="pending"))
    with pytest.raises(RuntimeError, match="finalize"):
        history.transport_wait(0, 10)
    with pytest.raises(RuntimeError, match="finalize"):
        history.cqt_count(0, 10)
    history.finalize()
    result = history.transport_wait(0, 10)
    assert result["requested_count"] == result["right_censored_count"] == 1
    history.close()


def test_outage_duration_uses_original_builtin_sum(tmp_path):
    values = [0.1] * 100
    events = [ev("availability.down", index + 1, duration_s=value)
              for index, value in enumerate(values)]
    history = build(tmp_path / "metrics.sqlite", events)
    assert payload(history.summary_counts({})["availability_down_s"]) == payload(sum(values))
    history.close()
