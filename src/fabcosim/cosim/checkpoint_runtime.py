"""纯生产的快照协调：封口输出、保存实际运行图，恢复到新的run目录。"""

import hashlib
import json
import math
import time
import uuid
from pathlib import Path

from fabcosim.cosim.checkpoint import (
    CheckpointError,
    load_checkpoint,
    prune_checkpoint_generations,
    save_checkpoint,
)
from fabcosim.cosim.checkpoint_io import CheckpointEventLog
from fabcosim.cosim.metric_history import MetricHistory


class CheckpointPaused(Exception):
    def __init__(self, path: Path, at: float):
        self.path, self.at = path, at
        super().__init__(f"完整运行状态已保存：{path}，仿真时刻{at}")


class SavableEventLog(CheckpointEventLog):
    def __reduce_ex__(self, protocol):
        return type(self).restore, (self._checkpoint_state, self.path)


class SavableMetricHistory(MetricHistory):
    def __reduce_ex__(self, protocol):
        return type(self).restore, (self._checkpoint_state, self.path)


def model_metadata() -> dict:
    """禁止用不同模型代码或数据静默接上旧状态；不依赖当前Git提交是否含文档改动。"""
    from fabcosim.cosim import full

    root = Path(full.__file__).resolve().parents[3]
    source = Path(__file__).resolve().parents[1]
    paths = list(source.rglob("*.py"))
    baseline = root / "03-数据/基准-v1"
    manifest = json.loads((baseline / "manifest.json").read_text("utf-8"))
    paths.append(baseline / "manifest.json")
    paths.extend(root / s["path"] for s in manifest["sources"] if s["path"].endswith(".xlsx"))
    paths.extend(root / s["path"] for s in manifest["derived"] if s["path"].endswith(".csv"))
    return {"production_checkpoint": 1, "source_and_data": {
        (str(Path("src/fabcosim") / p.relative_to(source)) if p.is_relative_to(source)
         else str(p.relative_to(root))): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }}


class CheckpointControl:
    def __init__(self, settings: dict, output: Path, event_log, history, wall_started: float):
        interval = settings["checkpoint"]["interval_s"]
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError("checkpoint.interval_s必须为有限正数")
        self.interval, self.next_s, self.number = interval, interval, 0
        self.stop_after_save = settings["checkpoint"].get("stop_after_save", False)
        self.output, self.directory = output, output / "checkpoints"
        self.event_log, self.history, self.wall_started = event_log, history, wall_started
        self.metadata = model_metadata()
        settings_file = output / "checkpoint-input.json"
        content = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
        with settings_file.open("xb") as stream:
            stream.write(content)
        self.input_asset = {"path": str(settings_file.resolve()), "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest()}
        self.metadata["input_settings"] = self.input_asset

    def save(self, payload: dict, now: float) -> None:
        self.number += 1
        self.next_s = (self.number + 1) * self.interval
        if self.next_s <= now:
            raise ValueError("checkpoint.interval_s小于当前仿真时刻的浮点分辨率")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.event_log._checkpoint_state = self.event_log.snapshot()
        if self.history is not None:
            self.history._checkpoint_state = self.history.snapshot(
                self.directory / f"metrics-{uuid.uuid4().hex}.sqlite3"
            )
        externals = {"output": self.output, "event_output": self.event_log.path,
                     "checkpoint_directory": self.directory, "wall_started": self.wall_started}
        if self.history is not None:
            externals["history_output"] = self.history.path
        path = self.directory / "latest.json"
        assets = [self.input_asset, *[
            {"path": part["path"], "size": part["bytes"], "sha256": part["sha256"]}
            for part in self.event_log._checkpoint_state["parts"]
        ]]
        if self.history is not None:
            state = self.history._checkpoint_state
            assets.append({"path": state["backup_path"], "size": state["backup_bytes"],
                           "sha256": state["backup_sha256"]})
        metadata = {**self.metadata, "time_s": now, "event_count": self.event_log.count,
                    "checkpoint_assets": assets}
        save_checkpoint(path, payload, metadata, externals=externals)
        try:
            retention = prune_checkpoint_generations(path)
        except (CheckpointError, OSError, KeyError, TypeError, ValueError) as exc:
            # 回收失败不破坏已提交快照，也不改变生产事件；磁盘保护仍然有效。
            retention = {"status": "skipped", "error": str(exc)}
        with (self.directory / "retention.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time_s": now, **retention}, ensure_ascii=False) + "\n")
        if self.stop_after_save or (self.output / "CHECKPOINT_STOP").exists():
            raise CheckpointPaused(path, now)


def resume_run(checkpoint: Path, output: Path) -> dict:
    """从可信本机快照恢复实际事件队列，不重复初始化或重跑投片历史。"""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    externals = {"output": output, "event_output": Path(str(output)),
                 "history_output": output / "metric-history.sqlite3",
                 "checkpoint_directory": output / "checkpoints", "wall_started": time.perf_counter()}
    payload, manifest = load_checkpoint(Path(checkpoint), model_metadata(), externals=externals)
    payload["control"].stop_after_save = False
    payload["after_restore"]()
    (output / "resume-provenance.json").write_text(json.dumps({
        "checkpoint": str(Path(checkpoint).resolve()), "manifest": manifest,
        "wall_clock": "本目录wall_elapsed_s从本次恢复开始，不含停机时间",
    }, ensure_ascii=False, indent=2), "utf-8")
    summary = payload["summary"]
    try:
        return payload["execute"]()
    except CheckpointPaused as exc:
        summary.update(status="checkpoint_saved", checkpoint=str(exc.path), end_time_s=exc.at)
        return summary
    except BaseException as exc:
        summary.update(status="failed", error=str(exc))
        raise
    finally:
        payload["cleanup"]()
