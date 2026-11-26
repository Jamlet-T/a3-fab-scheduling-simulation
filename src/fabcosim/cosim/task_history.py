"""把从不释放的config["tasks"]审计行搬到磁盘，同时保住原配置的逐字节格式。

`full.py` 在每次任务下发时把 `{id, lot_id, source, target}` 追加进 `config["tasks"]`，
这个列表随运行单调增长却从不被读取。本模块给出它的磁盘替身：

- `append` 随即写进 gzip 事件数组（复用 `EventLog`），进程内只留一个计数；
- `len` 返回累计条数，语义与列表长度一致；
- `write_config` 在收尾时按原顶层键序重建 `config.json`，`tasks` 键的内容直接从
  上面那份 gzip 流式解压复制，因此结果与"把全部任务行留在内存再
  `json.dump(..., ensure_ascii=False, separators=(",", ":"))`"逐字节相同。

只支持 `performance.compact_json=true`：缩进档的 `tasks` 数组需要逐行缩进，
无法靠一段原样文本拼接复现，故直接拒绝而不是悄悄换成空列表或元数据。
"""

from __future__ import annotations

import codecs
import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from fabcosim.cosim.event_log import EventLog, validate_gzip_level

TASK_HISTORY_NAME = "task-history.json.gz"
# 任务行恰好四字段；写盘顺序固定为这个元组，保证同一份数据得到同一串字节。
TASK_FIELDS = ("id", "lot_id", "source", "target")
_COPY_CHUNK = 1024 * 1024


class DiskTaskHistory:
    """`config["tasks"]` 的磁盘替身：只实现引擎用到的 append 与 len。

    不可迭代、不提供 replay：本对象是审计证据的写出口，不是可回读的数据结构。
    """

    def __init__(self, path: Path, level: int = 1) -> None:
        level = validate_gzip_level(level)
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"{path}已存在，任务历史不覆盖已有产物")
        self.path = path
        self.level = level
        self._log = EventLog(path, compression="gzip", level=level)
        self._count = 0
        self._closed = False

    def append(self, row: Mapping[str, Any]) -> None:
        """按固定字段序追加一行；先成功写出，再让len加一。"""
        if self._closed:
            raise RuntimeError("任务历史已关闭，不能继续追加")
        if not isinstance(row, Mapping):
            raise TypeError("任务行必须是映射")
        if set(row.keys()) != set(TASK_FIELDS) or len(row) != len(TASK_FIELDS):
            raise ValueError(f"任务行必须恰好包含{TASK_FIELDS}四个字段")
        # 编码失败由EventLog在写逗号之前抛；写失败同样不落到计数上。
        self._log.append({name: row[name] for name in TASK_FIELDS})
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def close(self) -> None:
        """幂等关闭；重复调用不重复补右括号。"""
        self._log.close()
        self._closed = True

    def write_config(self, config: Mapping[str, Any], destination: Path) -> Path:
        """按原顶层键序写出配置，`tasks` 从gzip原样流式复制。"""
        if config.get("tasks") is not self:
            raise ValueError("config['tasks']必须就是本任务历史对象")
        performance = config.get("performance")
        if not isinstance(performance, Mapping) or performance.get("compact_json") is not True:
            raise ValueError("任务历史只支持performance.compact_json=true的配置")
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(f"{destination}已存在，配置不覆盖已有产物")
        # 必须先收尾：右括号未写时解压得到的数组文本不完整。
        self.close()
        with destination.open("w", encoding="utf-8", newline="") as stream:
            stream.write("{")
            first = True
            for key, value in config.items():
                if not first:
                    stream.write(",")
                first = False
                stream.write(json.dumps(key, ensure_ascii=False, separators=(",", ":")))
                stream.write(":")
                if key == "tasks":
                    self._copy_tasks(stream)
                else:
                    json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("}")
        return destination

    def _copy_tasks(self, stream: IO[str]) -> None:
        """有界块解压复制任务数组原文；解码器跨块保留不完整的多字节序列。"""
        decoder = codecs.getincrementaldecoder("utf-8")()
        with gzip.open(self.path, "rb") as source:
            while chunk := source.read(_COPY_CHUNK):
                stream.write(decoder.decode(chunk))
            tail = decoder.decode(b"", final=True)
            if tail:
                stream.write(tail)
