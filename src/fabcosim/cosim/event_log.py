"""边运行边写出完整JSON事件数组，避免全量交通日志常驻内存；可选gzip压缩。"""

import gzip
import json
from pathlib import Path

EVENT_LOG_NAME = "events.json"
GZIP_SUFFIX = ".gz"
COMPRESSIONS = ("none", "gzip")
# 流量日志的gzip档。默认取1是优先写入速度，代价是体积更大：同一份18.5MB真实日志上
# level 1写出2,928,364字节、level 6写出2,061,401字节（约小30%）但压缩更慢。
# 实测见 04-研究/实验脚本/benchmark_event_compression.py 与
# runs/20261016-event-compression/lead-review-benchmark/report.md。
# 改这个值要同时更新该基准记录与 configs/README.md，不要只改常量。
DEFAULT_GZIP_LEVEL = 1


def _validate(compression: str) -> str:
    if compression not in COMPRESSIONS:
        raise ValueError("performance.event_compression必须为none或gzip")
    return compression


def validate_gzip_level(level: int) -> int:
    """在创建文件前核对等级；布尔值不能误当整数等级。"""
    if type(level) is not int or not 0 <= level <= 9:
        raise ValueError("performance.event_compression_level必须为0到9的整数")
    return level


def event_log_name(compression: str) -> str:
    """按压缩方式给出事件日志文件名；none档保持既有events.json不变。"""
    _validate(compression)
    return EVENT_LOG_NAME + (GZIP_SUFFIX if compression == "gzip" else "")


def event_log_path(output: Path, compression: str) -> Path:
    return output / event_log_name(compression)


# 同一份事件日志的合法文件名，none在前，供消费者判断"事件日志是否存在"。
EVENT_LOG_NAMES = tuple(event_log_name(name) for name in COMPRESSIONS)


def existing_event_log(directory: Path) -> Path | None:
    """目录里实际存在的事件日志，两份都没有时返回None。

    两份同时存在说明目录里混着两次运行，直接报错而不猜哪份更新——读者拿到的是
    哪一次运行的事件必须唯一。
    """
    found = [directory / name for name in EVENT_LOG_NAMES if (directory / name).is_file()]
    if len(found) > 1:
        raise ValueError(f"{found[0]}与{found[1]}同时存在，无法判断使用哪一份事件日志")
    return found[0] if found else None


class EventLog:
    """逐条追加事件的JSON数组。

    compression="none"写出与挪出压缩支持之前完全相同的字节；"gzip"把同样的文本
    压进events.json.gz，逐字节解压后与none档一致。两档都是边运行边写，不驻留全量事件。
    """

    def __init__(
        self, path: Path, compression: str = "none", level: int = DEFAULT_GZIP_LEVEL
    ) -> None:
        _validate(compression)
        level = validate_gzip_level(level)
        self.compression = compression
        self._raw = None
        if compression == "none":
            self.stream = path.open("w", encoding="utf-8", buffering=1024 * 1024)
        else:
            # 底层文件同样按1MB缓冲，zlib自身再缓冲，不做逐事件落盘。
            self._raw = path.open("wb", buffering=1024 * 1024)
            # filename=""不写名字头、mtime=0不写时间戳：同环境重跑同一份日志得到同一串字节。
            self.stream = gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=level,
                fileobj=self._raw,
                mtime=0,
            )
        self._write("[")
        self.first = True
        self.count = 0

    def _write(self, text: str) -> None:
        # gzip流只接受bytes，两档写进去的文本完全相同。
        self.stream.write(text if self.compression == "none" else text.encode("utf-8"))

    def append(self, event: dict) -> None:
        # 编码失败时不能先写逗号，否则异常收尾后会得到无效的[...,]数组。
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self._write(payload if self.first else "," + payload)
        self.first = False
        self.count += 1

    def close(self) -> None:
        # 异常路径也会关闭数组，行为与压缩支持之前一致；重复close是空操作。
        if not self.stream.closed:
            self._write("]")
            self.stream.close()
            if self._raw is not None:
                self._raw.close()
