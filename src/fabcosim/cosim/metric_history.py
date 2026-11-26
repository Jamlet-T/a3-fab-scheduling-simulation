"""运输等待、CQT违例与停机时长的磁盘增量统计缓存。

本模块只替代 ``full.py`` 中随仿真长期增长的 metric_events 统计缓存：
完整 events 日志仍由 EventLog 原样写盘，这里不删除任何业务事件。

SQLite保存运输等待（含终态未取货行）、违例CQT时刻与原序停机时长；内存只保留未配对请求
（``task_id -> (seq, requested)``）以及 lot 释放/完工的最小投影。因此
内存随lot投影和活动请求增长，不长期保存全部历史任务ID或完整事件字典。

边界：这是派生统计缓存，不提供仿真续算，也不是全事件独立审计器。
"""

from __future__ import annotations

import hashlib
import itertools
import shutil
import sqlite3
from pathlib import Path
from statistics import mean

_CACHE_SIZE_KIB = -32 * 1024  # 负值表示 KiB：约 32 MiB
SNAPSHOT_VERSION = 1
_HASH_CHUNK_BYTES = 1024 * 1024


class MetricHistory:
    def __init__(self, path: str | Path, batch_size: int = 4096) -> None:
        if batch_size < 1:
            raise ValueError("batch_size必须为正整数")
        self.path = Path(path)
        if self.path.exists():
            raise FileExistsError(f"统计缓存路径已存在，拒绝覆盖: {self.path}")
        self._connection = sqlite3.connect(self.path)
        self._connection.execute(f"PRAGMA cache_size={_CACHE_SIZE_KIB}")
        self._connection.execute("PRAGMA temp_store=FILE")
        # 数值列不声明类型：保留 Python 的 int/float 原始类型，避免 REAL 亲和
        # 把整数等待值转成浮点，进而改变 statistics.mean 的返回类型。
        self._connection.executescript(
            """
            CREATE TABLE transport_wait(
                seq INTEGER PRIMARY KEY,
                requested,
                pickup,
                wait
            );
            CREATE TABLE cqt_violations(time);
            CREATE TABLE availability_down(duration);
            """
        )
        self._connection.commit()
        self._batch_size = batch_size
        self._rows: list[tuple] = []
        self._violation_rows: list[tuple] = []
        self._outage_rows: list[tuple] = []
        self._pending: dict[str, tuple[int, float]] = {}
        self._next_seq = 1
        # 仅保留最小投影，供summary窗口统计使用；不保存原事件字典。
        self.lot_events: list[tuple[str, float, str]] = []
        self._visited_tools: set[str] = set()
        self._processing_end = 0
        self._delay_end = 0
        self._batch_count = 0
        self._underfilled_batches = 0
        self._availability_outages = 0
        self._reworks = 0
        self._cqt_violations = 0
        # 一旦开始finalize就禁止observe，失败重试期间同样禁止。
        self._finalizing = False
        self._finalized = False
        self._closed = False

    # ------------------------------------------------------------------ 观测
    def observe(self, event: dict) -> None:
        if self._finalized:
            raise RuntimeError("已finalize，不能再observe")
        if self._finalizing:
            raise RuntimeError("finalize已开始，不能再observe")
        if self._closed:
            raise RuntimeError("已close，不能再observe")
        kind = event["kind"]
        time = event["time"]
        if kind == "transport.requested":
            task_id = event["task_id"]
            if task_id in self._pending:
                raise ValueError(f"重复的pending运输请求: {task_id}")
            self._pending[task_id] = (self._next_seq, time)
            self._next_seq += 1
        elif kind == "transport.pickup_start":
            task_id = event["task_id"]
            if task_id not in self._pending:
                raise ValueError(f"未知的运输取货请求: {task_id}")
            seq, requested = self._pending.pop(task_id)
            # 等待值用Python原始减法，与window_metrics保持一致。
            self._rows.append((seq, requested, time, time - requested))
            if len(self._rows) >= self._batch_size:
                self._flush()
        elif kind == "processing.start":
            self._visited_tools.add(event["equipment"])
        elif kind == "processing.end":
            self._processing_end += 1
        elif kind == "delay.end":
            self._delay_end += 1
        elif kind == "batch.formed":
            self._batch_count += 1
            if event["underfilled"]:
                self._underfilled_batches += 1
        elif kind == "availability.down":
            self._availability_outages += 1
            # 原summary用内置sum一次求和；逐次+=在本机有末位差异，不能替换算法。
            self._outage_rows.append((event["duration_s"],))
            if len(self._outage_rows) >= self._batch_size:
                self._flush()
        elif kind == "lot.rework":
            self._reworks += 1
        elif kind == "cqt.checked":
            if event["violated"]:
                self._cqt_violations += 1
                self._violation_rows.append((time,))
                if len(self._violation_rows) >= self._batch_size:
                    self._flush()
        elif kind == "lot.released" or kind == "lot.completed":
            self.lot_events.append((kind, time, event["lot_id"]))

    @property
    def pending_count(self) -> int:
        """当前RAM中尚未配对的请求数；finalize后为空，不代表没有右删失行。"""
        return len(self._pending)

    @property
    def request_count(self) -> int:
        """全程运输请求数，与观察窗口内请求数不同。"""
        return self._next_seq - 1

    @property
    def pending_requests(self) -> dict[str, tuple[int, float]]:
        """未配对请求的只读副本；数量受在途任务数约束。"""
        return dict(self._pending)

    # ------------------------------------------------------------------ 落盘
    def _flush(self) -> None:
        if not self._rows and not self._violation_rows and not self._outage_rows:
            return
        connection = self._connection
        try:
            if self._rows:
                connection.executemany(
                    "INSERT INTO transport_wait(seq, requested, pickup, wait) VALUES (?, ?, ?, ?)",
                    self._rows,
                )
            if self._violation_rows:
                connection.executemany(
                    "INSERT INTO cqt_violations(time) VALUES (?)", self._violation_rows
                )
            if self._outage_rows:
                connection.executemany("INSERT INTO availability_down(duration) VALUES (?)", self._outage_rows)
            connection.commit()
        except BaseException:
            # 回滚本批未提交插入，否则重试会撞seq主键或重复计数；
            # 缓冲保留给调用方重试。回滚自身失败不得覆盖原异常。
            self._rollback_silently()
            raise
        self._rows.clear()
        self._violation_rows.clear()
        self._outage_rows.clear()

    def _rollback_silently(self) -> None:
        try:
            self._connection.rollback()
        except BaseException:
            pass

    def finalize(self) -> None:
        """小批写入未取货请求（pickup/wait为NULL）；失败可重试，成功后幂等。

        已配对的 ``_rows`` 先整体落盘；pending 按 ``batch_size`` 分批直接写入，
        每批提交成功后才从 pending 移除，失败时回滚并保留该批，便于重试。
        进入本方法后即使失败也永久拒绝 ``observe``：已提交的右删失行不能再被
        后续取货改写。
        """
        if self._closed:
            raise RuntimeError("已close，不能finalize")
        if self._finalized:
            return
        self._finalizing = True
        self._flush()
        while self._pending:
            # 插入顺序即请求顺序（seq单调递增），islice只取一批，不复制全部pending。
            batch = list(itertools.islice(self._pending.items(), self._batch_size))
            rows = [(seq, requested, None, None) for _, (seq, requested) in batch]
            try:
                self._connection.executemany(
                    "INSERT INTO transport_wait(seq, requested, pickup, wait) VALUES (?, ?, ?, ?)",
                    rows,
                )
                self._connection.commit()
            except BaseException:
                # 本批整体丢弃，pending保留供重试；已提交的前序批次不重复写入。
                self._rollback_silently()
                raise
            for task_id, _ in batch:
                del self._pending[task_id]
        self._finalized = True

    def close(self) -> None:
        """提交已完成行；不把未配对请求伪造成配对。幂等。"""
        if self._closed:
            return
        self._flush()
        self._connection.commit()
        self._connection.close()
        self._closed = True

    # ------------------------------------------------------------------ 快照
    def snapshot(self, database_path: str | Path) -> dict:
        """只flush并备份当前SQLite，不finalize：未配对请求不能提前右删失。

        备份落到全新路径，返回除连接等IO之外的全部内存状态，供``restore``在另一个
        run里接着观察。原库不做任何改写，snapshot前后可照常``observe``/``finalize``。
        """
        if self._closed:
            raise RuntimeError("已close，不能snapshot")
        backup_path = Path(database_path)
        if backup_path.exists():
            raise FileExistsError(f"统计快照路径已存在，拒绝覆盖: {backup_path}")
        self._flush()
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(backup_path)
        try:
            self._connection.backup(target)
        except BaseException:
            target.close()
            backup_path.unlink(missing_ok=True)
            raise
        target.close()
        return {
            "version": SNAPSHOT_VERSION,
            "source_path": str(self.path.resolve()),
            "backup_path": str(backup_path.resolve()),
            "backup_sha256": _sha256_file(backup_path),
            "backup_bytes": backup_path.stat().st_size,
            "batch_size": self._batch_size,
            "transport_rows": [tuple(row) for row in self._rows],
            "violation_rows": [tuple(row) for row in self._violation_rows],
            "outage_rows": [tuple(row) for row in self._outage_rows],
            "pending": {task_id: tuple(value) for task_id, value in self._pending.items()},
            "next_seq": self._next_seq,
            "lot_events": [tuple(event) for event in self.lot_events],
            "visited_tools": sorted(self._visited_tools),
            "processing_end": self._processing_end,
            "delay_end": self._delay_end,
            "batch_count": self._batch_count,
            "underfilled_batches": self._underfilled_batches,
            "availability_outages": self._availability_outages,
            "reworks": self._reworks,
            "cqt_violations": self._cqt_violations,
            "finalizing": self._finalizing,
            "finalized": self._finalized,
            "closed": self._closed,
        }

    @classmethod
    def restore(cls, state: dict, new_database_path: str | Path) -> "MetricHistory":
        """把快照备份复制到新run并重建连接与内存状态；原备份和原库都不改动。"""
        normalized = _validate_snapshot(state)
        new_path = Path(new_database_path)
        if new_path.exists():
            raise FileExistsError(f"统计缓存路径已存在，拒绝覆盖: {new_path}")
        backup_path = Path(normalized["backup_path"])
        if not backup_path.is_file():
            raise FileNotFoundError(f"统计快照备份缺失: {backup_path}")
        if (
            backup_path.stat().st_size != normalized["backup_bytes"]
            or _sha256_file(backup_path) != normalized["backup_sha256"]
        ):
            raise ValueError(f"统计快照备份校验失败，内容已被改动: {backup_path}")
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup_path, new_path)
        history = cls.__new__(cls)
        history.path = new_path
        history._batch_size = normalized["batch_size"]
        history._connection = sqlite3.connect(new_path)
        # 与``__init__``相同的连接参数；schema由备份原样带过来，不再重建。
        history._connection.execute(f"PRAGMA cache_size={_CACHE_SIZE_KIB}")
        history._connection.execute("PRAGMA temp_store=FILE")
        history._rows = [tuple(row) for row in normalized["transport_rows"]]
        history._violation_rows = [tuple(row) for row in normalized["violation_rows"]]
        history._outage_rows = [tuple(row) for row in normalized["outage_rows"]]
        history._pending = {
            task_id: (int(seq), requested)
            for task_id, (seq, requested) in normalized["pending"].items()
        }
        history._next_seq = normalized["next_seq"]
        history.lot_events = [tuple(event) for event in normalized["lot_events"]]
        history._visited_tools = set(normalized["visited_tools"])
        history._processing_end = normalized["processing_end"]
        history._delay_end = normalized["delay_end"]
        history._batch_count = normalized["batch_count"]
        history._underfilled_batches = normalized["underfilled_batches"]
        history._availability_outages = normalized["availability_outages"]
        history._reworks = normalized["reworks"]
        history._cqt_violations = normalized["cqt_violations"]
        history._finalizing = normalized["finalizing"]
        history._finalized = normalized["finalized"]
        history._closed = False
        return history

    # ------------------------------------------------------------------ 查询
    def transport_wait(self, start: float, end: float) -> dict:
        self._require_readable()
        self._flush()
        connection = self._connection
        connection.execute("DROP TABLE IF EXISTS temp.transport_wait_uncensored")
        connection.execute("CREATE TEMP TABLE transport_wait_uncensored(value)")
        connection.execute("DROP TABLE IF EXISTS temp.transport_wait_all")
        connection.execute("CREATE TEMP TABLE transport_wait_all(value)")
        requested_count = 0
        uncensored_count = 0
        all_rows: list[tuple] = []
        uncensored_rows: list[tuple] = []
        cursor = connection.execute("SELECT requested, pickup FROM transport_wait ORDER BY seq")
        for requested, pickup in cursor:
            if not (start < requested <= end):
                continue
            requested_count += 1
            # 原始公式：min(pickup或end, end) - requested；不外推、不用SQL聚合。
            observed = end if pickup is None else min(pickup, end)
            all_rows.append((observed - requested,))
            if pickup is not None and pickup <= end:
                uncensored_count += 1
                uncensored_rows.append((pickup - requested,))
            if len(all_rows) >= self._batch_size:
                connection.executemany(
                    "INSERT INTO transport_wait_all(value) VALUES (?)", all_rows
                )
                all_rows.clear()
            if len(uncensored_rows) >= self._batch_size:
                connection.executemany(
                    "INSERT INTO transport_wait_uncensored(value) VALUES (?)", uncensored_rows
                )
                uncensored_rows.clear()
        connection.executemany("INSERT INTO transport_wait_all(value) VALUES (?)", all_rows)
        connection.executemany(
            "INSERT INTO transport_wait_uncensored(value) VALUES (?)", uncensored_rows
        )
        return {
            "definition": "运输需求提出到实际开始取货，含输出端口等待与空驶取货",
            "requested_count": requested_count,
            "uncensored": _distribution(connection, "transport_wait_uncensored", uncensored_count),
            "right_censored_count": requested_count - uncensored_count,
            "all_observed_lower_bound": _distribution(
                connection, "transport_wait_all", requested_count
            ),
        }

    def cqt_count(self, start: float, end: float) -> int:
        self._require_readable()
        self._flush()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM cqt_violations WHERE time > ? AND time <= ?", (start, end)
        ).fetchone()
        return row[0]

    def summary_counts(self, assets: dict) -> dict:
        """返回与full.py summary同名同口径的计数，供集成时直接展开。"""
        self._require_readable()
        availability_down_s = sum(
            value for (value,) in self._connection.execute("SELECT duration FROM availability_down ORDER BY rowid")
        )
        return {
            "visited_tool_count": len(self._visited_tools),
            "visited_tool_groups": sorted(
                {assets[name]["tool_group"] for name in self._visited_tools}
            ),
            "physical_processing_operations": self._processing_end,
            "delay_operations": self._delay_end,
            "batch_count": self._batch_count,
            "underfilled_batches": self._underfilled_batches,
            "availability_outages": self._availability_outages,
            "availability_down_s": availability_down_s,
            "reworks": self._reworks,
            "cqt_violations": self._cqt_violations,
        }

    def _require_readable(self) -> None:
        if self._closed:
            raise RuntimeError("已close，不能查询")
        if not self._finalized:
            raise RuntimeError("必须先成功finalize，再查询包含右删失的完整窗口")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_snapshot(state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("统计快照必须是字典")
    if state.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"不支持的统计快照版本: {state.get('version')}")
    required = (
        "backup_path",
        "backup_sha256",
        "backup_bytes",
        "batch_size",
        "transport_rows",
        "violation_rows",
        "outage_rows",
        "pending",
        "next_seq",
        "lot_events",
        "visited_tools",
        "processing_end",
        "delay_end",
        "batch_count",
        "underfilled_batches",
        "availability_outages",
        "reworks",
        "cqt_violations",
        "finalizing",
        "finalized",
        "closed",
    )
    missing = [key for key in required if key not in state]
    if missing:
        raise ValueError(f"统计快照缺少字段: {', '.join(missing)}")
    if not isinstance(state["backup_path"], str) or not Path(state["backup_path"]).is_absolute():
        raise ValueError("统计快照备份路径必须为绝对路径")
    if not isinstance(state["backup_sha256"], str) or len(state["backup_sha256"]) != 64:
        raise ValueError("统计快照缺少合法的sha256")
    if type(state["backup_bytes"]) is not int or state["backup_bytes"] < 0:
        raise ValueError("统计快照备份字节数非法")
    if type(state["batch_size"]) is not int or state["batch_size"] < 1:
        raise ValueError("统计快照batch_size必须为正整数")
    if type(state["next_seq"]) is not int or state["next_seq"] < 1:
        raise ValueError("统计快照next_seq必须为正整数")
    if state["closed"]:
        raise ValueError("已close的统计缓存不能恢复")
    for key in ("finalizing", "finalized"):
        if not isinstance(state[key], bool):
            raise ValueError(f"统计快照{key}必须为布尔值")
    for key in ("transport_rows", "violation_rows", "outage_rows", "lot_events", "visited_tools"):
        if not isinstance(state[key], list):
            raise ValueError(f"统计快照{key}必须为列表")
    if not isinstance(state["pending"], dict):
        raise ValueError("统计快照pending必须为字典")
    for key in (
        "processing_end",
        "delay_end",
        "batch_count",
        "underfilled_batches",
        "availability_outages",
        "reworks",
        "cqt_violations",
    ):
        if type(state[key]) is not int or state[key] < 0:
            raise ValueError(f"统计快照{key}必须为非负整数")
    return state


def _distribution(connection: sqlite3.Connection, table: str, count: int) -> dict:
    """与metrics.distribution同公式，但只流式遍历并捕获所需排序位置。"""
    if not count:
        return {"count": 0, "mean_s": None, "p50_s": None, "p95_s": None}
    p50_position = (count - 1) * 0.5
    p95_position = (count - 1) * 0.95
    left50 = int(p50_position)
    left95 = int(p95_position)
    wanted = {
        left50,
        min(left50 + 1, count - 1),
        left95,
        min(left95 + 1, count - 1),
    }
    captured: dict[int, float] = {}

    def ordered_values():
        index = 0
        for (value,) in connection.execute(f"SELECT value FROM {table} ORDER BY value"):
            if index in wanted:
                captured[index] = value
            yield value
            index += 1

    total_mean = mean(ordered_values())

    def percentile(left: int, position: float) -> float:
        low = captured[left]
        high = captured[min(left + 1, count - 1)]
        return low + (high - low) * (position - left)

    return {
        "count": count,
        "mean_s": total_mean,
        "p50_s": percentile(left50, p50_position),
        "p95_s": percentile(left95, p95_position),
    }
