"""断点续算的IO组件：分段封口的事件日志。

主控把普通 ``Routine`` 与 cloudpickle 闭包一起 pickle，IO 对象按外部ID排除；本模块
给出的快照只含绝对路径、sha256、字节数与计数等可pickle字段，恢复时按这些字段重建
对象。全量事件不驻留内存，恢复不重算仿真，也不改写旧run的任何文件。

事件先写进目标日志同目录下的 ``events.parts/``：``snapshot()`` 把当前段封口成完整
JSON数组并记账，下一次 ``append`` 才懒开新段；``close()`` 流式拼接各段数组内部字节，
一次成文为 ``events.json(.gz)``，与同事件一次性 ``EventLog`` 的写出逐字节相同。

边界：本模块只负责事件日志的分段与合并，不保存仿真引擎、RNG或运输状态。
"""

import gzip
import hashlib
import os
import uuid
from pathlib import Path

from fabcosim.cosim.event_log import (
    DEFAULT_GZIP_LEVEL,
    GZIP_SUFFIX,
    EventLog,
    event_log_name,
    validate_gzip_level,
)

STATE_VERSION = 1
PART_DIR_SUFFIX = ".parts"
PART_FILE_SUFFIX = ".json"
# 合并与校验都按块流式处理，不把整段或整份事件读进内存。
COPY_CHUNK_BYTES = 1024 * 1024


def _event_path(path: Path, compression: str) -> Path:
    """目标日志路径：给目录时补标准文件名，gzip档缺后缀时补``.gz``。"""
    if path.is_dir():
        return path / event_log_name(compression)
    if compression == "gzip" and path.suffix != GZIP_SUFFIX:
        return path.with_name(path.name + GZIP_SUFFIX)
    return path


def _part_dir(event_path: Path) -> Path:
    # 同一运行目录的两种压缩档共用``events.parts``，混用时靠part名的存在性检查拒绝覆盖。
    base = event_path.name.split(".")[0]
    return event_path.parent / (base + PART_DIR_SUFFIX)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_part_inner(path: Path, compression: str):
    """逐块产出part数组去掉首尾方括号后的内部字节。

    首尾括号可能落在不同的read块上，所以最后一个字节始终留到下一次读取再写；
    空段``[]``不产出任何字节。
    """
    opener = gzip.open if compression == "gzip" else open
    with opener(path, "rb") as stream:
        pending = b""
        started = False
        while chunk := stream.read(COPY_CHUNK_BYTES):
            if not started:
                if chunk[:1] != b"[":
                    raise ValueError(f"part不是完整的JSON数组: {path}")
                chunk = chunk[1:]
                started = True
            data = pending + chunk
            if not data:
                continue
            pending = data[-1:]
            body = data[:-1]
            if body:
                yield body
        if not started:
            raise ValueError(f"part为空文件: {path}")
        if pending != b"]":
            raise ValueError(f"part缺少收尾括号: {path}")


class CheckpointEventLog:
    """可按``snapshot``/``restore``断点续写的事件日志。

    ``path``是本次运行最终的完整事件日志文件；给已存在目录则按压缩方式补``events.json``
    或``events.json.gz``。构造时拒绝覆盖已存在的日志文件，与运行目录的既有约定一致。
    """

    def __init__(
        self, path: Path, compression: str = "none", level: int = DEFAULT_GZIP_LEVEL
    ) -> None:
        event_log_name(compression)
        level = validate_gzip_level(level)
        self.compression = compression
        self.level = level
        self.path = _event_path(Path(path), compression)
        if self.path.exists():
            raise FileExistsError(f"事件日志路径已存在，拒绝覆盖: {self.path}")
        self.part_dir = _part_dir(self.path)
        self.count = 0
        self._sealed: list[dict] = []
        self._next_part_index = 0
        self._current: EventLog | None = None
        self._current_path: Path | None = None
        self._current_count = 0
        self._closed = False

    # ------------------------------------------------------------------ 追加
    def _open_part(self) -> None:
        self.part_dir.mkdir(parents=True, exist_ok=True)
        suffix = GZIP_SUFFIX if self.compression == "gzip" else ""
        part_path = self.part_dir / (
            f"part-{self._next_part_index:06d}{PART_FILE_SUFFIX}{suffix}"
        )
        if part_path.exists():
            raise FileExistsError(f"part已存在，拒绝覆盖: {part_path}")
        self._next_part_index += 1
        self._current_path = part_path
        self._current = EventLog(part_path, self.compression, self.level)
        self._current_count = 0

    def append(self, event: dict) -> None:
        if self._closed:
            raise RuntimeError("已close，不能再append")
        if self._current is None:
            self._open_part()
        self._current.append(event)
        self._current_count += 1
        self.count += 1

    # ------------------------------------------------------------------ 封口
    def _seal(self) -> None:
        if self._current is None:
            return
        self._current.close()
        part_path = self._current_path
        self._sealed.append(
            {
                "path": str(part_path.resolve()),
                "sha256": _sha256_file(part_path),
                "bytes": part_path.stat().st_size,
                "count": self._current_count,
            }
        )
        self._current = None
        self._current_path = None
        self._current_count = 0

    def _state(self) -> dict:
        return {
            "version": STATE_VERSION,
            "compression": self.compression,
            "level": self.level,
            "target": str(self.path.resolve()),
            "count": self.count,
            "parts": [dict(part) for part in self._sealed],
        }

    def snapshot(self) -> dict:
        """封口当前part并返回可pickle状态；已封口的part不会被改写，可重复调用。"""
        if not self._closed:
            self._seal()
        return self._state()

    # ------------------------------------------------------------------ 恢复
    @classmethod
    def restore(cls, state: dict, new_output_path: Path) -> "CheckpointEventLog":
        """校验封口part并复用其前缀；新事件只写进新run的part目录。"""
        normalized = _validate_state(state)
        for part in normalized["parts"]:
            _verify_part(part)
        log = cls(new_output_path, normalized["compression"], normalized["level"])
        source_dirs = {Path(part["path"]).parent.resolve() for part in normalized["parts"]}
        if log.part_dir.resolve() in source_dirs:
            raise ValueError(f"恢复目标与源part同目录，会改写旧run: {log.part_dir}")
        log._sealed = [dict(part) for part in normalized["parts"]]
        log._next_part_index = len(log._sealed)
        log.count = normalized["count"]
        return log

    # ------------------------------------------------------------------ 合并
    def _write_merged(self, stream) -> None:
        stream.write(b"[")
        wrote = False
        for part in self._sealed:
            part_wrote = False
            for chunk in _iter_part_inner(Path(part["path"]), self.compression):
                if not part_wrote:
                    if wrote:
                        stream.write(b",")
                    wrote = True
                    part_wrote = True
                stream.write(chunk)
        stream.write(b"]")

    def close(self) -> None:
        """把全部part流式合并成一份完整日志；重复close是空操作。"""
        if self._closed:
            return
        # close同样先封口：合并必须只读完整数组，且失败后重试不会重复写事件。
        self._seal()
        target = self.path
        if target.exists():
            raise FileExistsError(f"事件日志路径已存在，拒绝覆盖: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        try:
            raw = temporary.open("xb", buffering=1024 * 1024)
            try:
                if self.compression == "none":
                    self._write_merged(raw)
                else:
                    # 与EventLog同参数：不写文件名、mtime=0、同压缩等级，字节可复现。
                    with gzip.GzipFile(
                        filename="",
                        mode="wb",
                        compresslevel=self.level,
                        fileobj=raw,
                        mtime=0,
                    ) as stream:
                        self._write_merged(stream)
            finally:
                raw.close()
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._closed = True


def _validate_state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("事件日志快照必须是字典")
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"不支持的事件日志快照版本: {state.get('version')}")
    compression = state.get("compression")
    event_log_name(compression)
    level = validate_gzip_level(state.get("level"))
    parts = state.get("parts")
    if not isinstance(parts, list):
        raise ValueError("事件日志快照缺少parts列表")
    total = state.get("count")
    if type(total) is not int or total < 0:
        raise ValueError("事件日志快照count必须为非负整数")
    seen: set[str] = set()
    counted = 0
    normalized_parts = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("事件日志快照的part必须是字典")
        path = part.get("path")
        sha256 = part.get("sha256")
        size = part.get("bytes")
        count = part.get("count")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("part路径必须为绝对路径")
        if path in seen:
            raise ValueError(f"part路径重复: {path}")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"part缺少合法的sha256: {path}")
        if type(size) is not int or size < 0:
            raise ValueError(f"part字节数非法: {path}")
        if type(count) is not int or count < 0:
            raise ValueError(f"part事件数非法: {path}")
        seen.add(path)
        counted += count
        normalized_parts.append(
            {"path": path, "sha256": sha256, "bytes": size, "count": count}
        )
    if counted != total:
        raise ValueError(f"part事件数合计{counted}与快照count={total}不一致")
    return {
        "version": STATE_VERSION,
        "compression": compression,
        "level": level,
        "parts": normalized_parts,
        "count": total,
    }


def _verify_part(part: dict) -> None:
    path = Path(part["path"])
    if not path.is_file():
        raise FileNotFoundError(f"快照part缺失: {path}")
    if path.stat().st_size != part["bytes"]:
        raise ValueError(f"part字节数与快照不符: {path}")
    if _sha256_file(path) != part["sha256"]:
        raise ValueError(f"part校验失败，内容已被改动: {path}")
