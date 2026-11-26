"""production_only运行的独立事件/终态核查器（只读）。

边界：

- 只读`config.json`、`summary.json`与`events.json`/`events.json.gz`，不重新仿真；
- 只核对纯生产语义：输入转运、机台加工、虚拟delay与批次；纯生产运行中不存在
  运输/车辆/任务/物料路线事件，因此这里不复用也不替代物流侧的核查；
- 事件用ijson流式读取，内存只保留逐lot状态、未结束批次与计数；
- `verify_production_run`不写任何文件；CLI只写`--report`指定的新路径，且拒绝覆盖。
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from collections.abc import Iterator
from pathlib import Path

from fabcosim.cosim.event_log import existing_event_log
from fabcosim.production.model import grid_time_at_or_after

# 纯生产运行中不允许出现的事件族；"material.route"没有尾点，按前缀匹配。
FORBIDDEN_PREFIXES = ("transport.", "vehicle.", "task.", "material.route")
# 进入目标后才能开始的事件：真实加工与虚拟delay共用同一到达判据。
ARRIVAL_KINDS = ("processing.start", "delay.start")
# 记为"本lot一道工序完成"的终点事件。
PROCESS_END_KINDS = ("processing.end", "delay.end")
OK_STATUSES = ("completed", "window_completed")
MAX_EXAMPLES = 20
# 进入机台的事件：三者都会把lot从目标.queue移入.tool并释放输入token。
TOOL_ENTER_KINDS = ("setup.start", "tool.load_start", "processing.start")

CHECKS = (
    "inputs",
    "scope.production_only",
    "scope.no_logistics_state",
    "status.terminal",
    "events.shape",
    "events.time_order",
    "events.forbidden_kinds",
    "lot.release_not_early",
    "transfer.pairing",
    "transfer.start_after_previous_end",
    "transfer.input_token",
    "processing.arrived_at_target",
    "processing.pairing",
    "module.pairing",
    "batch.identity",
    "batch.wafer_bounds",
    "summary.counts",
    "summary.processed_steps",
    "summary.lot_terminal",
    "route.sequence",
    "completion.final_transfer",
)

COUNTER_KINDS = (
    "lot.released",
    "lot.completed",
    "production.transfer_requested",
    "production.transfer_start",
    "production.transfer_end",
    "processing.start",
    "processing.end",
    "delay.start",
    "delay.end",
    "module.enter",
    "module.exit",
    "batch.formed",
    "lot.rework",
    "operation.skipped",
    "operation.excluded",
    "lot.reentered",
    "production.dispatched",
)


class ProductionVerificationError(ValueError):
    """输入不满足核查前提，无法进入事件核对。"""


class _Report:
    """按检查项累积违规；每项只保留前若干条示例，同时记总数。"""

    def __init__(self) -> None:
        self.violations: dict[str, list[str]] = {}
        self.totals: dict[str, int] = {}
        self.executed: set[str] = set()
        self.counts: dict[str, int] = {}
        self.lot_processed: dict[str, list] = {}
        self.lot_excluded: dict[str, list] = {}
        self.lot_terminal: dict[str, tuple] = {}

    def mark(self, name: str) -> None:
        self.executed.add(name)

    def fail(self, name: str, message: str) -> None:
        self.mark(name)
        self.totals[name] = self.totals.get(name, 0) + 1
        bucket = self.violations.setdefault(name, [])
        if len(bucket) < MAX_EXAMPLES:
            bucket.append(message)


def _result(run_dir: Path, report: _Report) -> dict:
    checks, errors = [], []
    for name in CHECKS:
        if name not in report.executed:
            continue
        examples = report.violations.get(name, [])
        checks.append(
            {
                "name": name,
                "passed": not examples,
                "violation_count": report.totals.get(name, 0),
                "examples": examples,
            }
        )
        errors.extend(f"{name}: {message}" for message in examples)
    return {
        "run_dir": str(run_dir),
        "passed": bool(checks) and all(check["passed"] for check in checks),
        "checks": checks,
        "errors": errors,
        "counts": dict(report.counts),
    }


def verify_production_run(run_dir: Path) -> dict:
    """核查一次production_only运行，返回`checks/errors/passed/counts`。"""
    run_dir = Path(run_dir)
    report = _Report()
    try:
        config, summary, events_path = _load_inputs(run_dir)
        step = _positive_number(config.get("step_s"), "config.step_s")
        fixed_transfer_s = _transfer_duration(config, summary)
        capacity = _buffer_capacity(config)
        _stage_inputs(config, summary, report)
    except ProductionVerificationError as exc:
        report.fail("inputs", str(exc))
        return _result(run_dir, report)
    if report.violations.get("scope.production_only") or report.violations.get(
        "status.terminal"
    ):
        # 非纯生产或非正常终态的事件流不能按本模块语义核对，直接拒绝。
        return _result(run_dir, report)
    _scan_events(events_path, config, step, fixed_transfer_s, capacity, report)
    _check_summary(summary, config, report)
    return _result(run_dir, report)


def _load_inputs(run_dir: Path) -> tuple[dict, dict, Path]:
    if not run_dir.is_dir():
        raise ProductionVerificationError(f"运行目录不存在: {run_dir}")
    config_path, summary_path = run_dir / "config.json", run_dir / "summary.json"
    for path in (config_path, summary_path):
        if not path.is_file():
            raise ProductionVerificationError(f"缺少必需文件: {path}")
    try:
        config = json.loads(config_path.read_text("utf-8"))
        summary = json.loads(summary_path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionVerificationError(f"读取config/summary失败: {exc}") from exc
    try:
        events_path = existing_event_log(run_dir)
    except ValueError as exc:
        raise ProductionVerificationError(str(exc)) from exc
    if events_path is None:
        raise ProductionVerificationError(f"缺少events.json或events.json.gz: {run_dir}")
    return config, summary, events_path


def _positive_number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductionVerificationError(f"{where}必须是数值")
    if not math.isfinite(value) or value <= 0:
        raise ProductionVerificationError(f"{where}必须是有限正数")
    return float(value)


def _transfer_duration(config: dict, summary: dict) -> float:
    """取本次运行实际生效的fixed_transfer_s；summary优先于config的默认值。"""
    value = summary.get("fixed_transfer_s")
    if value is None:
        value = config.get("production", {}).get("fixed_transfer_s", 60.0)
    return _positive_number(value, "fixed_transfer_s")


def _buffer_capacity(config: dict) -> int:
    value = config.get("production", {}).get("input_buffer_lots")
    if type(value) is not int or value <= 0:
        raise ProductionVerificationError("config.production.input_buffer_lots必须是正整数")
    return value


def _stage_inputs(config: dict, summary: dict, report: _Report) -> None:
    report.mark("scope.production_only")
    model = config.get("transport", {}).get("model")
    if model != "production_only":
        report.fail("scope.production_only", f"config.transport.model={model!r}")
    for field in ("scope", "transport_model"):
        value = summary.get(field)
        if value != "production_only":
            report.fail("scope.production_only", f"summary.{field}={value!r}")
    report.mark("scope.no_logistics_state")
    # 纯生产没有任务表；摘要里端口/车辆/任务类字段只能是空值。
    if config.get("tasks") != []:
        report.fail(
            "scope.no_logistics_state",
            f"config.tasks应为空列表，实际为{config.get('tasks')!r}",
        )
    for field, value in summary.items():
        lowered = field.lower()
        # transport_model / transport_integrator_time_s 是模型标识与计时，不是物流状态。
        if lowered.startswith("transport_"):
            continue
        if not any(word in lowered for word in ("port", "vehicle", "task")):
            continue
        if value not in (None, 0, "", [], {}):
            report.fail(
                "scope.no_logistics_state",
                f"summary.{field}在纯生产运行中应为空，实际为{value!r}",
            )
    logistics = summary.get("logistics_metrics")
    if logistics not in (None, "not_available", "", [], {}):
        report.fail(
            "scope.no_logistics_state",
            f"summary.logistics_metrics在纯生产运行中应为空，实际为{logistics!r}",
        )
    report.mark("status.terminal")
    status = summary.get("status")
    if status not in OK_STATUSES:
        report.fail("status.terminal", f"summary.status={status!r}，要求completed或window_completed")


def _iter_events(path: Path) -> Iterator[dict]:
    try:
        import ijson
    except ImportError as exc:  # pragma: no cover - dev依赖缺失时给出明确提示
        raise ProductionVerificationError("事件流核查需要ijson（项目dev依赖）") from exc
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        # 默认把小数解析成Decimal；事件时间要与float网格比较，统一用float。
        yield from ijson.items(stream, "item", use_float=True)


def _lot_state() -> dict:
    return {
        "released": None,
        "arrived": None,
        "pending": None,
        "last_end": None,
        "last_transfer": None,
        "completed": None,
        "processed": [],
        "excluded": [],
        "next_index": 0,
        # 生产输入token：lot当前持有的目标设备；token_gate记住已为哪次进机释放过。
        "token": None,
        "token_gate": None,
    }


def _scan_events(
    events_path: Path,
    config: dict,
    step: float,
    transfer_s: float,
    capacity: int,
    report: _Report,
) -> None:
    lots = {lot["id"]: lot for lot in config["lots"]}
    routes = config["routes"]
    exclude_delay = config.get("production", {}).get("delay32_policy") == "exclude"
    for route_name, route in routes.items():
        for op in route["operations"]:
            expected = exclude_delay and op["tool_group"] == "Delay_32"
            if bool(op.get("excluded_from_execution", False)) != expected:
                report.fail("route.sequence", f"{route_name}步骤{op['source_step']}的排除标记与Delay_32范围不符")
    batch_ops = {
        (name, op["source_step"]): op
        for name, route in routes.items()
        for op in route["operations"]
        if op.get("unit") == "Batch"
    }
    exit_targets = set(config["candidates"]["COMPLETE"])
    allow_underfilled = config.get("production", {}).get("allow_underfilled_after_timeout")
    devices = set(config.get("assets", {}))
    state = {lid: _lot_state() for lid in lots}
    open_batches: dict[str, tuple] = {}
    open_processing: dict[tuple, dict] = {}
    open_modules: dict[tuple, dict] = {}
    token_usage: dict[str, int] = {}
    counts = {"events": 0, **{kind: 0 for kind in COUNTER_KINDS}}
    for name in (
        "events.shape",
        "events.time_order",
        "events.forbidden_kinds",
        "lot.release_not_early",
        "transfer.pairing",
        "transfer.start_after_previous_end",
        "transfer.input_token",
        "processing.arrived_at_target",
        "processing.pairing",
        "module.pairing",
        "batch.identity",
        "batch.wafer_bounds",
        "completion.final_transfer",
        "route.sequence",
    ):
        report.mark(name)

    previous_time = -math.inf
    for index, event in enumerate(_iter_events(events_path), start=1):
        counts["events"] = index
        if not isinstance(event, dict):
            report.fail("events.shape", f"events[{index}]不是对象")
            continue
        kind = event.get("kind")
        if not isinstance(kind, str):
            report.fail("events.shape", f"events[{index}]缺少str类型的kind")
            continue
        if kind in counts:
            counts[kind] += 1
        time = event.get("time")
        if (
            isinstance(time, bool)
            or not isinstance(time, (int, float))
            or not math.isfinite(time)
            or time < 0
        ):
            report.fail("events.shape", f"events[{index}] kind={kind!r}的time非法")
            continue
        if time < previous_time:
            report.fail(
                "events.time_order",
                f"events[{index}] kind={kind!r} time={time}早于上一条{previous_time}",
            )
        previous_time = max(previous_time, time)
        if any(kind.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            report.fail(
                "events.forbidden_kinds",
                f"events[{index}]出现纯生产不应存在的{kind!r}",
            )
        lot_id = event.get("lot_id")
        if lot_id is None:
            lot_state = None
        elif not isinstance(lot_id, str) or lot_id not in lots:
            report.fail("events.shape", f"events[{index}] kind={kind!r}的lot_id未知")
            continue
        else:
            lot_state = state[lot_id]
            if kind != "lot.released" and lot_state["released"] is None:
                report.fail(
                    "events.shape",
                    f"events[{index}] lot {lot_id}的{kind}早于lot.released",
                )
            route = routes[lots[lot_id]["route"]]["operations"]
            if kind in (
                "production.transfer_start", "processing.start", "delay.start",
                "processing.end", "delay.end", "operation.skipped", "operation.excluded",
            ):
                cursor = lot_state["next_index"]
                expected = route[cursor]["source_step"] if cursor < len(route) else None
                if event.get("source_step") != expected:
                    report.fail("route.sequence", f"lot {lot_id}的{kind}步骤{event.get('source_step')}，前序推进要求{expected}")
                excluded = cursor < len(route) and route[cursor].get("excluded_from_execution", False)
                if excluded != (kind == "operation.excluded"):
                    report.fail("route.sequence", f"lot {lot_id}排除步骤与事件{kind}不符")
                if kind == "operation.excluded":
                    if event.get("reason") != "delay32_scope":
                        report.fail("route.sequence", f"lot {lot_id}排除原因缺失或非法")
                    lot_state["excluded"].append(event.get("source_step"))
                if kind in (*PROCESS_END_KINDS, "operation.skipped", "operation.excluded"):
                    lot_state["next_index"] += 1
            elif kind == "lot.rework":
                target = event.get("target_step")
                if type(target) is not int or not 1 <= target <= len(route):
                    report.fail("route.sequence", f"lot {lot_id}返工目标非法：{target}")
                else:
                    lot_state["next_index"] = target - 1
        if kind == "lot.released":
            if lot_state["released"] is not None:
                report.fail("events.shape", f"events[{index}] lot {lot_id}重复释放")
            else:
                lot_state["released"] = time
            declared = lots[lot_id]["release_s"]
            if time < declared:
                report.fail(
                    "lot.release_not_early",
                    f"lot {lot_id}在{time}s释放，早于定义的{declared}s",
                )
        elif kind == "production.transfer_requested":
            # 虚拟delay结束或离开队列：上一次输入token在此归还。
            _release_token(lot_id, lot_state, token_usage, report, where=kind)
            lot_state["token_gate"] = None
        elif kind == "production.transfer_start":
            _transfer_start(
                index, event, time, lot_id, lot_state, token_usage, capacity, devices, report
            )
        elif kind == "production.transfer_end":
            _transfer_end(index, event, time, lot_id, lot_state, step, transfer_s, report)
        elif kind in ARRIVAL_KINDS:
            _arrival(index, kind, event, time, lot_id, lot_state, report)
            if kind == "processing.start":
                _processing_start(index, event, time, lot_id, open_processing, report)
                _enter_tool(kind, index, event, time, lot_id, lot_state, token_usage, report)
        elif kind in ("setup.start", "tool.load_start"):
            _enter_tool(kind, index, event, time, lot_id, lot_state, token_usage, report)
        elif kind == "processing.end":
            _processing_end(index, event, time, lot_id, open_processing, report)
            lot_state["last_end"] = time
            lot_state["processed"].append(event.get("source_step"))
            open_batches.pop(lot_id, None)
        elif kind == "delay.end":
            lot_state["last_end"] = time
            lot_state["processed"].append(event.get("source_step"))
            open_batches.pop(lot_id, None)
        elif kind == "module.enter":
            _module_enter(index, event, time, open_modules, report)
        elif kind == "module.exit":
            _module_exit(index, event, time, open_modules, report)
        elif kind == "batch.formed":
            _batch(index, event, lots, batch_ops, open_batches, allow_underfilled, report)
        elif kind == "lot.completed":
            # 末次出口转运的token在lot离开队列、位置变为completed时归还。
            _release_token(lot_id, lot_state, token_usage, report, where=kind, required=True)
            _completed(index, time, lot_id, lot_state, exit_targets, report)

    for lot_id, lot_state in state.items():
        if lot_state["pending"] is not None and lot_state["completed"] is not None:
            report.fail(
                "transfer.pairing",
                f"lot {lot_id}已完工，但production.transfer_start没有对应结束",
            )
        if lot_state["completed"] is None:
            # 窗口截断时未完工lot可以合法地仍在queue/加工中。
            continue
        if lot_state["token"] is not None:
            report.fail(
                "transfer.input_token",
                f"lot {lot_id}已完工，但仍持有{lot_state['token']!r}的输入token",
            )
        for key in open_processing:
            if key[0] == lot_id:
                report.fail(
                    "processing.pairing",
                    f"lot {lot_id}已完工，但工序{key[1]!r} occurrence={key[2]!r}"
                    "的processing.start没有processing.end",
                )
        for key, entry in open_modules.items():
            if entry["lot"] == lot_id:
                report.fail(
                    "module.pairing",
                    f"lot {lot_id}已完工，但{key[0]}模块{key[1]}的module.enter没有module.exit",
                )
    counts["input_tokens_open"] = sum(1 for entry in state.values() if entry["token"] is not None)
    counts["processing_open"] = len(open_processing)
    counts["module_open"] = len(open_modules)
    report.counts = counts
    report.lot_processed = {lot_id: entry["processed"] for lot_id, entry in state.items()}
    report.lot_excluded = {lot_id: entry["excluded"] for lot_id, entry in state.items()}
    report.lot_terminal = {
        lot_id: (entry["released"], entry["completed"]) for lot_id, entry in state.items()
    }


def _release_token(
    lot_id: str,
    lot_state: dict,
    token_usage: dict[str, int],
    report: _Report,
    *,
    where: str,
    required: bool = False,
) -> None:
    token = lot_state["token"]
    if token is None:
        if required:
            report.fail(
                "transfer.input_token",
                f"lot {lot_id}在{where}时没有持有输入token",
            )
        return
    lot_state["token"] = None
    remaining = token_usage.get(token, 0) - 1
    token_usage[token] = remaining
    if remaining < 0:
        report.fail(
            "transfer.input_token",
            f"lot {lot_id}在{where}归还{token!r}的token后计数为负，token账本不一致",
        )


def _transfer_start(
    index: int,
    event: dict,
    time: float,
    lot_id: str,
    lot_state: dict,
    token_usage: dict[str, int],
    capacity: int,
    devices: set,
    report: _Report,
) -> None:
    if lot_state["pending"] is not None:
        pending = lot_state["pending"]
        report.fail(
            "transfer.pairing",
            f"lot {lot_id}在{time}s开始新转运，但此前于第{pending['index']}条开始"
            "的转运没有结束",
        )
    last_end = lot_state["last_end"]
    if last_end is not None and time < last_end:
        report.fail(
            "transfer.start_after_previous_end",
            f"lot {lot_id}在{time}s开始转运，早于上一道工序结束的{last_end}s",
        )
    target = event.get("equipment")
    if not isinstance(target, str) or target not in devices:
        report.fail(
            "transfer.input_token",
            f"events[{index}] lot {lot_id}的转运目标{target!r}不是已知设备",
        )
    if lot_state["token"] is not None:
        report.fail(
            "transfer.input_token",
            f"lot {lot_id}在{time}s开始新转运时已持有{lot_state['token']!r}的输入token",
        )
        # 先归还旧token，避免单lot违规把后续容量账本永久算多。
        _release_token(lot_id, lot_state, token_usage, report, where="transfer_start")
    lot_state["token"] = target
    lot_state["token_gate"] = None
    used = token_usage.get(target, 0) + 1
    token_usage[target] = used
    if used > capacity:
        report.fail(
            "transfer.input_token",
            f"设备{target!r}在{time}s有{used}个输入token，超过input_buffer_lots={capacity}",
        )
    lot_state["pending"] = {
        "index": index,
        "time": time,
        "equipment": target,
        "source_step": event.get("source_step"),
    }


def _transfer_end(
    index: int,
    event: dict,
    time: float,
    lot_id: str,
    lot_state: dict,
    step: float,
    transfer_s: float,
    report: _Report,
) -> None:
    target = event.get("equipment")
    token = lot_state["token"]
    if token is None:
        report.fail(
            "transfer.input_token",
            f"events[{index}] lot {lot_id}到达{target!r}时没有持有输入token",
        )
    elif token != target:
        report.fail(
            "transfer.input_token",
            f"lot {lot_id}到达{target!r}时持有的输入token属于{token!r}",
        )
    pending = lot_state["pending"]
    if pending is None:
        report.fail(
            "transfer.pairing",
            f"events[{index}] lot {lot_id}的production.transfer_end没有对应开始",
        )
        return
    if target != pending["equipment"]:
        report.fail(
            "transfer.pairing",
            f"lot {lot_id}转运结束目标{target!r}与开始的"
            f"{pending['equipment']!r}不一致",
        )
    if event.get("source_step") != pending["source_step"]:
        report.fail(
            "transfer.pairing",
            f"lot {lot_id}转运结束source_step={event.get('source_step')!r}与开始的"
            f"{pending['source_step']!r}不一致",
        )
    expected = grid_time_at_or_after(pending["time"] + transfer_s, step)
    if time != expected:
        report.fail(
            "transfer.pairing",
            f"lot {lot_id}转运{pending['time']}s→{time}s，网格对齐后应精确为{expected}s"
            f"（fixed_transfer_s={transfer_s}，step={step}）",
        )
    lot_state["pending"] = None
    lot_state["arrived"] = {
        "time": time,
        "equipment": target,
        "source_step": event.get("source_step"),
    }
    lot_state["last_transfer"] = (time, target, event.get("source_step"))


def _enter_tool(
    kind: str,
    index: int,
    event: dict,
    time: float,
    lot_id: str,
    lot_state: dict,
    token_usage: dict[str, int],
    report: _Report,
) -> None:
    """lot离开目标.queue进入机台：第一个setup/load/processing事件归还输入token。"""
    key = (event.get("equipment"), event.get("source_step"))
    token = lot_state["token"]
    if token is not None:
        if token != event.get("equipment"):
            report.fail(
                "transfer.input_token",
                f"lot {lot_id}的{kind}在{event.get('equipment')!r}，"
                f"但输入token属于{token!r}",
            )
        else:
            lot_state["token_gate"] = key
        _release_token(lot_id, lot_state, token_usage, report, where=kind)
    elif lot_state["token_gate"] != key:
        report.fail(
            "transfer.input_token",
            f"events[{index}] lot {lot_id}的{kind}在{time}s前没有持有输入token",
        )


def _processing_start(
    index: int,
    event: dict,
    time: float,
    lot_id: str,
    open_processing: dict[tuple, dict],
    report: _Report,
) -> None:
    key = (lot_id, event.get("source_step"), event.get("occurrence"))
    previous = open_processing.get(key)
    if previous is not None:
        report.fail(
            "processing.pairing",
            f"lot {lot_id}工序{key[1]!r} occurrence={key[2]!r}在{time}s重复start，"
            f"第{previous['index']}条start（{previous['time']}s）没有end",
        )
    else:
        open_processing[key] = {"index": index, "time": time}


def _processing_end(
    index: int,
    event: dict,
    time: float,
    lot_id: str,
    open_processing: dict[tuple, dict],
    report: _Report,
) -> None:
    key = (lot_id, event.get("source_step"), event.get("occurrence"))
    if open_processing.pop(key, None) is None:
        report.fail(
            "processing.pairing",
            f"events[{index}] lot {lot_id}工序{key[1]!r} occurrence={key[2]!r}"
            "的processing.end没有对应start",
        )


def _module_enter(
    index: int, event: dict, time: float, open_modules: dict[tuple, dict], report: _Report
) -> None:
    equipment, module = event.get("equipment"), event.get("module")
    if not isinstance(equipment, str) or type(module) is not int:
        report.fail(
            "module.pairing",
            f"events[{index}] module.enter缺少合法equipment/module：{equipment!r}/{module!r}",
        )
        return
    key = (equipment, module)
    previous = open_modules.get(key)
    if previous is not None:
        report.fail(
            "module.pairing",
            f"{equipment}模块{module}在{time}s被lot {event.get('lot_id')!r}进入，"
            f"但lot {previous['lot']!r}自{previous['time']}s起仍在占用",
        )
        return
    open_modules[key] = {"index": index, "time": time, "lot": event.get("lot_id")}


def _module_exit(
    index: int, event: dict, time: float, open_modules: dict[tuple, dict], report: _Report
) -> None:
    equipment, module = event.get("equipment"), event.get("module")
    key = (equipment, module)
    previous = open_modules.pop(key, None)
    if previous is None:
        report.fail(
            "module.pairing",
            f"events[{index}] {equipment!r}模块{module!r}在{time}s的module.exit没有对应enter",
        )
    elif previous["lot"] != event.get("lot_id"):
        report.fail(
            "module.pairing",
            f"{equipment}模块{module}在{time}s由lot {event.get('lot_id')!r}退出，"
            f"但进入的是lot {previous['lot']!r}",
        )


def _arrival(
    index: int, kind: str, event: dict, time: float, lot_id: str, lot_state: dict, report: _Report
) -> None:
    arrived = lot_state["arrived"]
    if arrived is None:
        report.fail(
            "processing.arrived_at_target",
            f"events[{index}] lot {lot_id}的{kind}发生在任何到达之前",
        )
        return
    if arrived["equipment"] != event.get("equipment"):
        report.fail(
            "processing.arrived_at_target",
            f"lot {lot_id}的{kind}在{event.get('equipment')!r}，但最近到达的是"
            f"{arrived['equipment']!r}",
        )
    if arrived["source_step"] != event.get("source_step"):
        report.fail(
            "processing.arrived_at_target",
            f"lot {lot_id}的{kind} source_step={event.get('source_step')!r}，但最近到达的"
            f"是{arrived['source_step']!r}",
        )
    if time < arrived["time"]:
        report.fail(
            "processing.arrived_at_target",
            f"lot {lot_id}的{kind}在{time}s，早于到达时刻{arrived['time']}s",
        )


def _batch(
    index: int,
    event: dict,
    lots: dict,
    batch_ops: dict,
    open_batches: dict,
    allow_underfilled: bool | None,
    report: _Report,
) -> None:
    lot_ids = event.get("lot_ids")
    if not isinstance(lot_ids, list) or not lot_ids or not all(
        isinstance(lot_id, str) for lot_id in lot_ids
    ):
        report.fail("batch.identity", f"events[{index}] batch.formed.lot_ids必须是非空字符串列表")
        return
    if len(set(lot_ids)) != len(lot_ids):
        report.fail("batch.identity", f"events[{index}] batch.formed同一lot出现多次")
        return
    unknown = [lot_id for lot_id in lot_ids if lot_id not in lots]
    if unknown:
        report.fail("batch.identity", f"events[{index}] batch.formed引用未知lot：{unknown}")
        return
    routes = {lots[lot_id]["route"] for lot_id in lot_ids}
    if len(routes) != 1:
        report.fail("batch.identity", f"events[{index}] batch.formed跨路线：{sorted(routes)}")
        return
    route = routes.pop()
    source_step = event.get("source_step")
    op = batch_ops.get((route, source_step))
    if op is None:
        report.fail(
            "batch.identity",
            f"events[{index}] batch.formed指向{route}步骤{source_step!r}，该步不是Batch工序",
        )
        return
    for lot_id in lot_ids:
        previous = open_batches.get(lot_id)
        if previous is not None:
            report.fail(
                "batch.identity",
                f"lot {lot_id}在前一批（{previous[0]!r}步骤{previous[1]!r}）结束前再次入批",
            )
        open_batches[lot_id] = (event.get("equipment"), source_step)
    wafers = sum(lots[lot_id]["wafer_count"] for lot_id in lot_ids)
    if event.get("wafers") != wafers:
        report.fail(
            "batch.wafer_bounds",
            f"events[{index}] batch.formed.wafers={event.get('wafers')!r}，"
            f"lot声明合计{wafers}",
        )
    minimum, maximum = event.get("minimum"), event.get("maximum")
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or minimum < 0
        or maximum <= 0
        or minimum > maximum
    ):
        report.fail(
            "batch.wafer_bounds",
            f"events[{index}] batch.formed的minimum/maximum非法：{minimum!r}/{maximum!r}",
        )
        return
    if minimum != op["batch_min"] or maximum != op["batch_max"]:
        report.fail(
            "batch.wafer_bounds",
            f"events[{index}] batch.formed的{minimum}/{maximum}与路线声明的"
            f"{op['batch_min']}/{op['batch_max']}不一致",
        )
    if wafers > maximum:
        report.fail(
            "batch.wafer_bounds",
            f"events[{index}] batch.formed.wafers={wafers}超过上限{maximum}",
        )
    underfilled = event.get("underfilled")
    if not isinstance(underfilled, bool):
        report.fail("batch.wafer_bounds", f"events[{index}] batch.formed.underfilled不是bool")
    elif underfilled != (wafers < minimum):
        report.fail(
            "batch.wafer_bounds",
            f"events[{index}] batch.formed.underfilled={underfilled}与wafers={wafers}、"
            f"minimum={minimum}不一致",
        )
    elif underfilled and allow_underfilled is False:
        report.fail(
            "batch.wafer_bounds",
            f"events[{index}] batch.formed欠批，但运行声明不允许欠批",
        )


def _completed(
    index: int,
    time: float,
    lot_id: str,
    lot_state: dict,
    exit_targets: set,
    report: _Report,
) -> None:
    if lot_state["completed"] is not None:
        report.fail("events.shape", f"events[{index}] lot {lot_id}重复完工")
    lot_state["completed"] = time
    last = lot_state["last_transfer"]
    if last is None:
        report.fail("completion.final_transfer", f"lot {lot_id}没有任何转运就完工")
        return
    end_time, equipment, source_step = last
    if equipment not in exit_targets:
        report.fail(
            "completion.final_transfer",
            f"lot {lot_id}末次转运到{equipment!r}，不在COMPLETE候选{sorted(exit_targets)}内",
        )
    if source_step is not None:
        report.fail(
            "completion.final_transfer",
            f"lot {lot_id}末次转运source_step={source_step!r}，不是完工出口转运",
        )
    if end_time > time:
        report.fail(
            "completion.final_transfer",
            f"lot {lot_id}在{time}s完工，早于末次转运结束的{end_time}s",
        )


def _check_summary(summary: dict, config: dict, report: _Report) -> None:
    counts = report.counts
    report.mark("summary.counts")
    expected = {
        "released_lots": counts.get("lot.released", 0),
        "completed_lots": counts.get("lot.completed", 0),
        "wip_lots": counts.get("lot.released", 0) - counts.get("lot.completed", 0),
        "completed_operations": counts.get("processing.end", 0) + counts.get("delay.end", 0),
    }
    if "delay32_policy" in config.get("production", {}):
        expected["excluded_operations"] = counts.get("operation.excluded", 0)
        if summary.get("delay32_policy") != config["production"]["delay32_policy"]:
            report.fail("summary.counts", "summary.delay32_policy与输入不一致")
        excluded = {name: [op["source_step"] for op in route["operations"]
                           if op.get("excluded_from_execution", False)]
                    for name, route in config["routes"].items()}
        execution = {name: len(route["operations"]) - len(excluded[name])
                     for name, route in config["routes"].items()}
        if summary.get("excluded_route_steps") != excluded or summary.get("execution_route_step_counts") != execution:
            report.fail("summary.counts", "summary源排除步骤/执行步骤数与输入不一致")
    for field, value in expected.items():
        actual = summary.get(field)
        if type(actual) is not int:
            report.fail("summary.counts", f"summary.{field}缺失或不是整数")
        elif actual != value:
            report.fail("summary.counts", f"summary.{field}={actual}，事件重算={value}")
    report.mark("summary.processed_steps")
    rows = summary.get("lot_results")
    if not isinstance(rows, list):
        report.fail("summary.processed_steps", "summary.lot_results缺失或不是列表")
        return
    state_processed = report.lot_processed
    report.mark("summary.lot_terminal")
    definitions = {lot["id"]: lot for lot in config["lots"]}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            report.fail("summary.processed_steps", "summary.lot_results含非对象条目")
            continue
        lot_id = row.get("lot_id")
        if lot_id not in state_processed:
            report.fail("summary.processed_steps", f"summary.lot_results含未知lot {lot_id!r}")
            continue
        if lot_id in seen:
            report.fail("summary.processed_steps", f"summary.lot_results重复lot {lot_id}")
            continue
        seen.add(lot_id)
        released, completed = report.lot_terminal[lot_id]
        expected_release = grid_time_at_or_after(definitions[lot_id]["release_s"], config["step_s"])
        if expected_release <= summary["end_time_s"] and released != expected_release:
            report.fail("summary.lot_terminal", f"lot {lot_id}未按原日历投片：{released} != {expected_release}")
        if row.get("actual_release_s") != released or row.get("completion_s") != completed:
            report.fail("summary.lot_terminal", f"lot {lot_id}投片/完工时刻与事件不一致")
        if (row.get("phase") == "completed") != (completed is not None):
            report.fail("summary.lot_terminal", f"lot {lot_id}终态phase与完工事件不一致")
        if (row.get("phase") == "unreleased") != (released is None):
            report.fail("summary.lot_terminal", f"lot {lot_id}未释放phase与事件不一致")
        if completed is not None and row.get("location") != "completed":
            report.fail("summary.lot_terminal", f"lot {lot_id}完工位置不一致")
        for field in ("route", "wafer_count", "release_s"):
            if row.get(field) != definitions[lot_id][field]:
                report.fail("summary.lot_terminal", f"lot {lot_id}的{field}与输入定义不一致")
        steps = row.get("processed_steps")
        if "delay32_policy" in config.get("production", {}) and row.get("excluded_steps") != report.lot_excluded[lot_id]:
            report.fail("summary.processed_steps", f"lot {lot_id}的excluded_steps与事件不一致")
        if not isinstance(steps, list):
            report.fail("summary.processed_steps", f"lot {lot_id}缺processed_steps列表")
        elif steps != state_processed[lot_id]:
            report.fail(
                "summary.processed_steps",
                f"lot {lot_id} processed_steps与事件不一致："
                f"summary={steps} events={state_processed[lot_id]}",
            )
    declared = {lot["id"] for lot in config["lots"]}
    for lot_id in sorted(declared - seen):
        report.fail("summary.processed_steps", f"summary.lot_results缺少lot {lot_id}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("run_dir", type=Path, help="含config/summary/events的production_only运行目录")
    parser.add_argument("--report", type=Path, required=True, help="全新报告JSON路径；已存在则拒绝")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    report_path = args.report
    if report_path.exists():
        print(f"拒绝：报告已存在，不覆盖: {report_path}", file=sys.stderr)
        return 2
    try:
        result = verify_production_run(run_dir)
    except Exception as exc:  # 事件流损坏等：仍写出可追溯的失败报告
        result = {
            "run_dir": str(run_dir),
            "passed": False,
            "checks": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
            "counts": {},
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    except FileExistsError:
        print(f"拒绝：报告在核查期间被创建，未覆盖: {report_path}", file=sys.stderr)
        return 2
    print(f"passed={result['passed']} report={report_path}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
