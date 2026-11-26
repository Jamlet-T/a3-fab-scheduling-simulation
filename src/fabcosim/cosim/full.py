"""完整生产路线与真实OHT：动态工序、有限输入缓冲、实际运输及静止时段推进。"""

import copy
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import simpy

from fabcosim.cosim.checkpoint_runtime import CheckpointPaused
from fabcosim.cosim.demo import drain
from fabcosim.cosim.event_log import (
    COMPRESSIONS,
    DEFAULT_GZIP_LEVEL,
    EventLog,
    event_log_path,
    validate_gzip_level,
)
from fabcosim.cosim.metric_history import MetricHistory
from fabcosim.cosim.metrics import TrafficMetrics, tardiness_metrics, window_metrics
from fabcosim.cosim.queue_occupancy import QueueOccupancy
from fabcosim.cosim.replay import write_full_replay
from fabcosim.cosim.task_history import TASK_HISTORY_NAME, DiskTaskHistory
from fabcosim.data.availability import load_availability
from fabcosim.data.full_route import compile_full_routes
from fabcosim.policies.registry import resolve_policies
from fabcosim.ports.audit import IncrementalMaterialAudit
from fabcosim.ports.model import Lot, Port
from fabcosim.production.availability import MachineAvailability
from fabcosim.production.batching import finite_cohorts, select_queue_batch
from fabcosim.production.machines import Machine
from fabcosim.production.model import grid_delay, grid_time_at_or_after, next_grid_time
from fabcosim.production.pure_routines import (
    BatchRoutine,
    DeadlineRoutine,
    LotRoutine,
    TransferRoutine,
)
from fabcosim.production.releases import assign_due_dates, load_calendar, prepare_releases
from fabcosim.production.scope import apply_production_scope
from fabcosim.transport.delay import DelayTransport
from fabcosim.transport.fleet import FleetTransport
from fabcosim.transport.recording import MotionRecorder
from fabcosim.transport.sumo import route_between, route_shapes


def _check_material(
    lots: dict,
    ports: dict,
    vehicles: dict,
    location_cache: dict[str, tuple[bool, Port | None]] | None = None,
) -> None:
    """检查全部lot归属；只缓存本run固定位置分类和Port对象，不缓存动态持有状态。"""
    if location_cache is None:
        location_cache = {}
    cargo_counts = {}
    for vehicle in vehicles.values():
        cargo = vehicle["cargo"]
        if cargo is not None:
            cargo_id = id(cargo)
            cargo_counts[cargo_id] = cargo_counts.get(cargo_id, 0) + 1
    for lot in lots.values():
        location = lot.location
        cached = location_cache.get(location)
        if cached is None:
            cached = (
                location in ("release_queue", "completed")
                or location.endswith((".tool", ".queue")),
                ports.get(location.removesuffix(".port")),
            )
            location_cache[location] = cached
        intrinsic_holder, port = cached
        lot_id = id(lot)
        holders = (
            intrinsic_holder
            + (port is not None and port.lot is lot)
            + cargo_counts.get(lot_id, 0)
        )
        if holders != 1:
            raise RuntimeError(f"物料守恒错误：{lot.id} / {holders}")


def take(queue: list[dict], job: dict) -> dict:
    """按对象身份从机台队列取出job并返回。

    队列元素是dict，等值比较可能命中另一个值相同的job，所以不能用`remove`定位；
    同一lot同时只有一个未完成job，按身份取出与原有语义一致。
    """
    return queue.pop(next(index for index, item in enumerate(queue) if item is job))


def run_full(settings: dict, output: Path) -> dict:
    wall_started = time.perf_counter()
    wafer_detail = settings.get('production', {}).get('wafer_detail', 'micro')
    if wafer_detail not in ('micro', 'lot_service', 'terminal_tail'):
        raise ValueError('production.wafer_detail必须为micro、lot_service或terminal_tail')
    if wafer_detail != 'micro' and settings.get('checkpoint'):
        raise ValueError('Wafer实验档不支持完整快照，不能生成不可恢复状态')
    checkpoint_enabled = bool(settings.get("checkpoint"))
    pure_production = settings.get("transport", {}).get("model") == "production_only"
    if checkpoint_enabled:
        options = settings["checkpoint"]
        if not isinstance(options, dict):
            raise ValueError("checkpoint必须是对象")
        interval = options.get("interval_s")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval <= 0):
            raise ValueError("checkpoint.interval_s必须为有限正数")
        if type(options.get("stop_after_save", False)) is not bool:
            raise ValueError("checkpoint.stop_after_save必须为布尔值")
        if not pure_production:
            raise ValueError("完整状态续算目前仅支持production_only")
        if settings.get("performance", {}).get("spool_task_history", False):
            raise ValueError("纯生产快照不需要spool_task_history，请关闭它")
        settings = copy.deepcopy(settings)
        settings.setdefault("performance", {})["stream_events"] = True
        settings["replay"] = {"enabled": False}
    transfer_s = settings.get("production", {}).get("fixed_transfer_s", 60.0)
    if pure_production and (
        isinstance(transfer_s, bool) or not isinstance(transfer_s, (int, float))
        or not math.isfinite(transfer_s) or transfer_s <= 0
    ):
        raise ValueError("纯生产fixed_transfer_s必须为有限正秒数")
    if pure_production and settings.get("replay", {}).get("enabled", False):
        raise ValueError("纯生产模式不支持运输回放")
    if pure_production and "transport" in settings.get("policies", {}):
        raise ValueError("纯生产模式不执行运输策略，请删除policies.transport")
    root = Path(__file__).resolve().parents[3]
    prepared = prepare_releases(settings)
    if "release_calendar" in prepared:
        prepared = load_calendar(prepared, root)
    config = compile_full_routes(prepared, root)
    apply_production_scope(config)
    assign_due_dates(config)
    policies = resolve_policies(config)
    rules = policies["rules"]
    observation = config.get("observation")
    replay_enabled = config.get("replay", {}).get("enabled", not pure_production)
    performance = config.get("performance", {})
    stream_events = performance.get("stream_events", False)
    incremental_material_audit = performance.get("incremental_material_audit", False)
    if type(incremental_material_audit) is not bool:
        raise ValueError("performance.incremental_material_audit必须为布尔值")
    material_audit_port_index = performance.get("material_audit_port_index", False)
    if type(material_audit_port_index) is not bool:
        raise ValueError("performance.material_audit_port_index必须为布尔值")
    if material_audit_port_index and not incremental_material_audit:
        raise ValueError("端口索引需要incremental_material_audit=true")
    delay_event_wakeup = performance.get("delay_event_wakeup", False)
    event_compression = performance.get("event_compression", "none")
    compression_level = validate_gzip_level(
        performance.get("event_compression_level", DEFAULT_GZIP_LEVEL)
    )
    # 事件日志档位在创建输出目录之前校验：非法配置不留半个运行目录。
    # 压缩档沿用流式写出的前提（完整事件不驻留内存、无代表回放），否则明确报错而不静默降级。
    if event_compression not in COMPRESSIONS:
        raise ValueError("performance.event_compression必须为none或gzip")
    if event_compression != "none" and not stream_events:
        raise ValueError(
            "performance.event_compression=gzip需要performance.stream_events，"
            "否则完整事件仍驻留内存"
        )
    if event_compression != "none" and replay_enabled:
        raise ValueError("performance.event_compression=gzip需要关闭replay.enabled")
    if stream_events and replay_enabled:
        raise ValueError("stream_events目前需要关闭replay，代表回放仍使用完整内存事件")
    # 统计缓存档位同样在创建输出目录前校验：sqlite档只替代统计缓存，
    # 完整事件仍流式落盘，因此必须已有流式事件且没有内存回放。
    metric_history_mode = performance.get("metric_history", "memory")
    if metric_history_mode not in ("memory", "sqlite"):
        raise ValueError("performance.metric_history必须为memory或sqlite")
    if metric_history_mode == "sqlite" and (not stream_events or replay_enabled):
        raise ValueError(
            "performance.metric_history=sqlite需要performance.stream_events=true且replay.enabled=false"
        )
    spool_task_history = performance.get("spool_task_history", False)
    if type(spool_task_history) is not bool:
        raise ValueError("performance.spool_task_history必须为布尔值")
    if spool_task_history and (
        not stream_events or replay_enabled or performance.get("compact_json") is not True
    ):
        raise ValueError("spool_task_history需要流式事件、compact_json=true并关闭回放")
    record_inventory = performance.get("runtime_inventory", False)
    if type(record_inventory) is not bool:
        raise ValueError("performance.runtime_inventory必须为布尔值")
    queue_occupancy_index = performance.get("queue_occupancy_index", False)
    if type(queue_occupancy_index) is not bool:
        raise ValueError("performance.queue_occupancy_index必须为布尔值")
    batch_host_policy = config["production"].get("batch_host_policy", "legacy")
    if batch_host_policy not in ("legacy", "compatible_buffer"):
        raise ValueError("production.batch_host_policy必须为legacy或compatible_buffer")
    minimum_host_lots = {}
    if batch_host_policy == "compatible_buffer":
        if config["production"].get("batch_policy") not in (None, "queue_batch"):
            raise ValueError("compatible_buffer只支持queue_batch，不改变finite_cohort语义")
        quantities = {lot["wafer_count"] for lot in config["lots"]}
        if len(quantities) != 1 or type(next(iter(quantities), None)) is not int or min(quantities) <= 0:
            raise ValueError("compatible_buffer要求统一的正整数wafer_count")
        quantity = next(iter(quantities))
        for route_name, route in config["routes"].items():
            for op in route["operations"]:
                if op["dedication_step"] and route["operations"][op["dedication_step"] - 1]["unit"] == "Batch":
                    raise ValueError("compatible_buffer不覆盖Batch专机绑定")
                if op["unit"] == "Batch":
                    needed = math.ceil(op["batch_min"] / quantity)
                    if needed > config["production"]["input_buffer_lots"]:
                        raise ValueError("单个合法批的最低lot数超过输入容量，不允许欠批或扩容")
                    minimum_host_lots[route_name, op["source_step"]] = needed
    replay_start = config.get("replay", {}).get("start_s", 0)
    replay_end = config.get("replay", {}).get("end_s", config["max_time_s"])
    if replay_enabled and (
        not 0 <= replay_start < replay_end <= config["max_time_s"]
        or any(
            abs(t / config["step_s"] - round(t / config["step_s"])) > 1e-6
            for t in (replay_start, replay_end)
        )
    ):
        raise ValueError("代表回放区间必须有效并对齐时间网格")
    output.mkdir(parents=True, exist_ok=False)
    env = simpy.Environment()
    step = config["step_s"]
    material_audit = config.get("performance", {}).get("material_audit", "events")
    if material_audit not in ("events", "every_step"):
        raise ValueError("material_audit必须为events或every_step")
    dispatch_mode = config["production"].get("dispatch_mode", "immediate")
    if dispatch_mode not in ("immediate", "deferred"):
        raise ValueError("dispatch_mode必须为immediate或deferred")
    deferred_dispatch = dispatch_mode == "deferred"
    if policies["names"]["production.job"] in ("implant_setup_fifo", "implant_setup_fifo_3groups") and not deferred_dispatch:
        raise ValueError(f"{policies['names']['production.job']}要求dispatch_mode=deferred，以实际空闲机台状态选择")
    legal_candidates_only = config["production"].get("legal_candidates_only", pure_production)
    if type(legal_candidates_only) is not bool:
        raise ValueError("production.legal_candidates_only必须为布尔值")
    if pure_production and not legal_candidates_only:
        raise ValueError("纯生产要求legal_candidates_only=true，策略只选择合法候选")
    availability_config = dict(config.get("availability") or {})
    availability_on = bool(availability_config.get("enabled"))
    availability_tables = None
    if availability_on:
        source = root / config["source_catalog"]["production"]["path"]
        availability_tables = load_availability(
            source, config.get("performance", {}).get("workbook_cache", True)
        )
    transport_model = config.get("transport", {}).get("model", "exact")
    if transport_model not in ("exact", "delay", "production_only"):
        raise ValueError("transport.model必须为exact、delay或production_only")
    aggregate_transport = transport_model == "delay"
    events, frames, tasks = [], [], {}
    # 已交付完成的任务不再参与派单（派单只读 ready_ids），其计数改由本计数器维护，
    # 所以条目可以在交接完成后立即回收，避免长仿真按交付量线性堆积。
    completed_task_count = 0
    ready_ids = set()
    vehicles, states, lots = {}, {}, {}
    route_cache = {}
    batch_hosts, batch_host_budget = {}, {}
    active_lots = set()
    queue_occupancy = QueueOccupancy() if queue_occupancy_index else None
    completed_counter = SimpleNamespace(value=0)
    engine = None
    motion = None
    production_buffers = None
    summary = {
        "status": "failed",
        "scope": "full_route_declared_policies",
        # 记录本次实际生效的规则名与派工模式，使运行结果可追溯到具体配置。
        "policies": policies["names"],
        "dispatch_mode": dispatch_mode,
    }
    if batch_host_policy != "legacy":
        summary["batch_host_policy"] = batch_host_policy
    zone_grants = 0
    event_log = None
    # 统计缓存：None为内存事件列表（默认），MetricHistory为SQLite派生缓存。
    history = None
    task_history = None
    material_index = None
    # 下列事件供summary和所有观测窗口统计使用；其余事件仍完整落盘。
    metric_events = {
        "processing.start", "processing.end", "delay.end", "batch.formed", "lot.rework",
        "cqt.checked", "lot.released", "lot.completed", "transport.requested",
        "transport.pickup_start", "availability.down",
    }

    def write_inventory(stage: str = "running") -> None:
        """复用现有进度边界，不新增仿真事件；只扫活跃lot的等待/加工阶段。"""
        if not record_inventory:
            return
        row = {
            "time_s": round(env.now, 9),
            "wall_elapsed_s": time.perf_counter() - wall_started,
            "declared_lots": len(config["lots"]), "registered_lots": len(lots),
            "active_lots": len(active_lots), "completed_lots": completed_counter.value,
            "unreleased_lots": len(config["lots"]) - len(active_lots) - completed_counter.value,
            "active_tasks": len(tasks), "ready_tasks": len(ready_ids),
            "future_simpy_events": len(env._queue),
            "business_events": event_log.count if event_log is not None else len(events),
            "task_history_rows": len(config.get("tasks", [])),
            "production_route_cache_entries": len(route_cache),
            "transport_distance_pairs": engine.cached_distance_pairs()
            if aggregate_transport and engine is not None else None,
            "metric_database_bytes": history.path.stat().st_size if history is not None else None,
            "task_history_disk_bytes": task_history.path.stat().st_size
            if task_history is not None else None,
            "retained_event_dicts": len(events),
            "metric_pending_buffer": history.pending_count if history is not None else None,
            "transport_requests": history.request_count if history is not None else None,
            "lot_event_projections": len(history.lot_events) if history is not None else None,
            "active_phase_counts": dict(Counter(states[lid]["phase"] for lid in active_lots)),
            "stage": stage,
            "status": summary["status"] if stage == "terminal" else "running",
        }
        if pure_production and production_buffers is not None:
            row.update(
                production_input_reserved=len(production_buffers.tokens),
                production_input_waiting=sum(len(resource.queue) for resource in production_buffers.resources.values()),
                production_max_input_occupancy=max(
                    (len(resource.users) for resource in production_buffers.resources.values()), default=0
                ),
            )
        with (output / "runtime-inventory.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record(kind: str, **fields) -> None:
        nonlocal zone_grants
        if kind == "zone.granted":
            zone_grants += 1
        vid = fields.get("vehicle_id")
        task = vehicles.get(vid, {}).get("task")
        if task and kind == "vehicle.route":
            fields["lot_id"], fields["task_id"] = task["lot_id"], task["id"]
        # 完整工艺保存全部生产/运输事件；区域常规申请/释放仅计数，避免长期巡行日志膨胀。
        if kind.startswith("zone.") and kind not in (
            "zone.exit_replanned",
            "zone.reservation_cancelled",
            "zone.exit_waypoint",
            "zone.stop_reanchored",
            "zone.priority_revoked",
        ):
            return
        event = {"time": round(env.now, 9), "kind": kind, **fields}
        if event_log is not None:
            event_log.append(event)
        # SQLite模式：完整事件已原样写盘，这里只把统计类事件交给派生缓存；
        # 原统计事件字典不再进入内存events列表。
        if history is not None and kind in metric_events:
            history.observe(event)
        if not stream_events:
            events.append(event)
        elif history is None and kind in metric_events:
            events.append(event)

    def cleanup():
        primary_error = sys.exc_info()[1]
        cache_close_error = None
        if material_index is not None:
            material_index.close()
        if event_log is not None:
            event_log.close()
        if history is not None:
            # 完整EventLog先收尾再关缓存；缓存close失败同样让本次运行失败，
            # 但在已有异常传播时不覆盖原异常。
            try:
                history.close()
            except BaseException as exc:
                summary["metric_history_close_error"] = str(exc)
                if primary_error is None:
                    summary.update(status="failed", error=str(exc))
                    cache_close_error = exc
        if task_history is not None:
            try:
                task_history.close()
            except BaseException as exc:
                summary["task_history_close_error"] = str(exc)
                if primary_error is None and cache_close_error is None:
                    summary.update(status="failed", error=str(exc))
                    cache_close_error = exc
        if material_index is not None:
            (output / "material-audit.json").write_text(
                json.dumps({
                    "mode": "production_only_incremental_with_periodic_full_scan" if pure_production else (
                        "incremental_observed_ports_full_vehicle_scan"
                        if material_audit_port_index else "incremental_with_full_port_vehicle_scan"
                    ),
                    "checks": material_index.checks,
                    "full_checks": material_index.full_checks,
                    "checked_lots": material_index.checked_lots,
                    "ports_examined": material_index.ports_examined,
                    "declared_lots": len(lots),
                    "time_s": env.now,
                    "recovery": "not_a_resumable_checkpoint",
                }, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        if motion is not None:
            motion.close()
        for name, value in (
            ("config", config),
            ("summary", summary),
            ("events", events),
            ("frames", frames),
        ):
            if name == "events" and event_log is not None:
                continue
            if name == "config" and task_history is not None:
                try:
                    task_history.write_config(config, output / "config.json")
                except BaseException as exc:
                    summary["task_history_write_error"] = str(exc)
                    if primary_error is None and cache_close_error is None:
                        summary.update(status="failed", error=str(exc))
                        cache_close_error = exc
                continue
            # 长实验的事件列表可达数GB；逐块写出，避免同时保留编码块和巨型字符串。
            with (output / f"{name}.json").open("w", encoding="utf-8") as stream:
                compact = config.get("performance", {}).get("compact_json", False)
                json.dump(value, stream, ensure_ascii=False, indent=None if compact else 2,
                          separators=(",", ":") if compact else None)
        if engine:
            engine.close()
        write_inventory("terminal")
        if cache_close_error is not None:
            raise cache_close_error

    try:
        if stream_events:
            if checkpoint_enabled:
                from fabcosim.cosim.checkpoint_runtime import SavableEventLog
                event_log_type = SavableEventLog
            else:
                event_log_type = EventLog
            event_log = event_log_type(
                event_log_path(output, event_compression), event_compression, compression_level
            )
        if metric_history_mode == "sqlite":
            # 派生统计缓存单独落盘；完整events仍由EventLog原样写出。
            if checkpoint_enabled:
                from fabcosim.cosim.checkpoint_runtime import SavableMetricHistory
                history = SavableMetricHistory(output / "metric-history.sqlite3")
            else:
                history = MetricHistory(output / "metric-history.sqlite3")
        engine = None if pure_production else (
            DelayTransport(config, output, record)
            if aggregate_transport
            else FleetTransport(config, output, record)
        )
        traffic_metrics = (
            TrafficMetrics(
                observation["warmup_s"],
                observation["end_s"],
                engine.ids,
                config["oht"]["length_m"],
                congestion_free=aggregate_transport,
            )
            if observation and not pure_production
            else None
        )
        if replay_enabled:
            motion = MotionRecorder(
                output,
                engine,
                config.get("motion_interval_s", step),
                config.get("motion_chunk_s", 600),
            )
        ports = {} if pure_production else {name: Port(name + ".port") for name in config["ports"]}
        # 端口映射在本run内固定；缓存不得跨run复用Port对象。
        material_location_cache: dict[str, tuple[bool, Port | None]] = {}
        changed = {name: env.event() for name in ports}
        assets = config["assets"]
        requests = {name: [] for name in assets}
        def availability_for(name: str, group: str) -> MachineAvailability | None:
            """按声明规则给机台装配PM与非计划停机；未启用时返回None。"""
            if not availability_on:
                return None
            params: dict = {}
            if availability_config.get("pm", True):
                params["pm"] = availability_tables["pm_by_group"].get(group)
            if availability_config.get("breakdown", True):
                area = config["toolgroups"][group].get("AREA")
                params["breakdown"] = availability_tables["breakdown_by_area"].get(area)
            return MachineAvailability(name, params, config["seed"])

        machines = {
            name: Machine(
                env,
                name,
                config["toolgroups"][asset["tool_group"]],
                config["setups"],
                step,
                record,
                availability_for(name, asset["tool_group"]),
                wafer_detail=wafer_detail,
                tool_group=asset['tool_group'],
                processing_distribution=config['production']['processing_distribution'],
                wafer_fast_context=(
                    wafer_detail != 'micro' and pure_production and deferred_dispatch
                    and all(op['unit'] == 'Wafer' for route in config['routes'].values()
                            for op in route['operations'] if op['tool_group'] == asset['tool_group'])
                ),
            )
            for name, asset in assets.items()
            if asset["kind"] == "tool"
        }
        for machine in machines.values():
            if machine.availability is not None and machine.availability.has_calendar_events():
                env.process(machine.downtime())
        delay_resources = simpy.Resource(env, capacity=400)
        vehicles = {} if pure_production else {
            vid: {"phase": "idle", "task": None, "cargo": None} for vid in engine.ids
        }
        production_buffers = None
        if pure_production:
            from fabcosim.production.transfer import ProductionTransferBuffers

            production_buffers = ProductionTransferBuffers(
                env, assets, config["production"]["input_buffer_lots"]
            )
        # immediate：job一到就倒进机台资源，排队顺序在到达时定下（既有行为）。
        # deferred：机台空闲时才派下一批，job留在requests里等待，生产派工策略因此
        # 能看到真实的机台队列。两种模式的成批时机不同，结果不可互比。
        machine_busy: set[str] = set()
        route_cache = {}
        cohorts = (
            finite_cohorts(config)
            if config["production"].get("batch_policy") == "finite_cohort"
            else {}
        )
        random_streams = {}
        # 不变式：active_lots == {已投片、尚未完工}的lot ID。未投片（release_queue）
        # 与完工（completed）都不在其中，因此它只可用于"是否占用某目标.queue"的计数。
        # 按ID索引投片定义：summary逐个lot取release_s时不再线性扫描config["lots"]。
        definitions_by_id = {}
        if spool_task_history:
            task_history = DiskTaskHistory(output / TASK_HISTORY_NAME, compression_level)
        config["tasks"] = task_history if task_history is not None else []
        for definition in config["lots"]:
            if definition["id"] in lots:
                raise ValueError("lot ID重复，不能重复投片")
            lots[definition["id"]] = Lot(definition["id"], "release_queue")
            definitions_by_id[definition["id"]] = definition
            states[definition["id"]] = {
                "route": definition["route"],
                "phase": "unreleased",
                "operation_index": 0,
                "wafer_count": definition["wafer_count"],
                "due_s": definition.get("due_s"),
                "priority": definition.get("priority"),
                "superhot": definition.get("superhot"),
                "passed_steps": [],
                "processed_steps": [],
                "skipped_steps": [],
                "excluded_steps": [],
                "dedication": {},
                "cqt": [],
                "equipment": None,
                "visits": 0,
                "move_count": 0,
                "seen": set(),
                "done": env.event(),
            }
        if incremental_material_audit:
            # 初始注册表固定；生产与交接只能改变已注册对象，不能替换/增删lot。
            material_index = IncrementalMaterialAudit(
                lots, ports, _check_material, port_index=material_audit_port_index
            )
            lots = material_index.lots
            ports = material_index.ports

        def notify(port: str) -> None:
            changed[port].succeed()
            changed[port] = env.event()

        def locate(lot: Lot, location: str) -> None:
            previous = lot.location
            if production_buffers is not None and previous.endswith(".queue") and previous != location:
                production_buffers.release(lot.id)
            lot.location = location
            if queue_occupancy is not None:
                queue_occupancy.transition(previous, lot.location)

        def put_on_port(port: Port, lot: Lot, origin: str) -> None:
            previous = lot.location
            port.put(lot, origin)
            if queue_occupancy is not None:
                queue_occupancy.transition(previous, lot.location)

        def take_from_port(port: Port, destination: str) -> Lot:
            lot = port.take(destination)
            if queue_occupancy is not None:
                queue_occupancy.transition(port.id, lot.location)
            return lot

        def path(source: str, target: str) -> tuple[list[str], float]:
            key = source, target
            if key not in route_cache:
                route_cache[key] = route_between(
                    engine.net, config["ports"][source], config["ports"][target]
                )
            return route_cache[key]

        def log_lot(kind: str, lot_id: str, op: dict | None = None, **fields) -> None:
            record(
                kind,
                lot_id=lot_id,
                location=lots[lot_id].location,
                source_step=op["source_step"] if op else None,
                operation_index=states[lot_id]["operation_index"],
                **fields,
            )

        def destination(lot_id: str, op: dict) -> str:
            state = states[lot_id]
            dedicated = state["dedication"].get(op["source_step"])
            if dedicated:
                return dedicated
            key = state["route"], op["source_step"]
            if cohorts and op["unit"] == "Batch":
                key += (cohorts[state["route"], op["source_step"], lot_id][0],)
            if op["unit"] == "Batch" and key in batch_hosts:
                return batch_hosts[key]
            current = state["equipment"]
            if op["virtual"] and assets[current]["kind"] == "buffer":
                return current
            start = assets[current]["output_port"]
            candidates = config["candidates"][op["tool_group"]]
            if batch_host_policy == "compatible_buffer" and op["unit"] == "Batch":
                needed = minimum_host_lots[key]
                candidates = [name for name in candidates if
                              batch_host_budget.get(name, 0) + needed <= config["production"]["input_buffer_lots"]]
                if not candidates:
                    raise RuntimeError(f"批host兼容预算无可用机台：{key}；不扩大容量或降低批量")
                lowest = min(batch_host_budget.get(name, 0) for name in candidates)
                candidates = [name for name in candidates if batch_host_budget.get(name, 0) == lowest]
            used_hosts = (
                {host for host_key, host in batch_hosts.items() if host_key[:2] == key[:2]}
                if cohorts and op["unit"] == "Batch"
                else frozenset()
            )
            selected = rules["destination"](
                candidates,
                distance=lambda name: 0.0 if pure_production else path(start, assets[name]["input_port"])[1],
                queue_lengths={
                    name: production_buffers.committed_lots(name) if pure_production
                    else len(requests[name]) for name in candidates
                },
                penalty_m=config["production"]["queue_penalty_m"],
                used_hosts=used_hosts,
            )
            if op["unit"] == "Batch":
                batch_hosts[key] = selected
                if batch_host_policy == "compatible_buffer":
                    batch_host_budget[selected] = batch_host_budget.get(selected, 0) + minimum_host_lots[key]
            return selected

        def transport(lot_id: str, target: str, op: dict | None):
            if pure_production:
                return TransferRoutine(production_context, lot_id, target, op)
            return vehicle_transport(lot_id, target, op)

        def vehicle_transport(lot_id: str, target: str, op: dict | None):
            state, lot = states[lot_id], lots[lot_id]
            state["next_port"] = assets[target]["input_port"]
            if state["equipment"] == target:
                if op and op["virtual"]:
                    log_lot("delay.continued", lot_id, op, equipment=target)
                    yield env.timeout(0)
                    return
                log_lot("transport.internal", lot_id, op, equipment=target)
                yield env.timeout(
                    grid_delay(env.now, config["production"]["internal_transfer_s"], step)
                )
                locate(lot, target + ".queue")
                return
            source_port = assets[state["equipment"]]["output_port"]
            target_port = assets[target]["input_port"]
            requested_s = env.now
            tid = f"{lot_id}.move{state['move_count'] + 1}"
            log_lot("transport.requested", lot_id, op, task_id=tid)
            state["phase"] = "waiting_output"
            while ports[source_port].lot is not None or ports[source_port].reserved_by is not None:
                yield changed[source_port]
            put_on_port(ports[source_port], lot, lot.location)
            state["move_count"] += 1
            task = {
                "id": tid,
                "lot_id": lot_id,
                "source": source_port,
                "target": target_port,
                "destination": target,
                "state": "ready",
                "ready_s": env.now,
                "requested_s": requested_s,
                "vehicle_id": None,
                "delivered": env.event(),
            }
            tasks[tid] = task
            ready_ids.add(tid)
            config["tasks"].append({k: task[k] for k in ("id", "lot_id", "source", "target")})
            state["phase"] = "awaiting_transport"
            edges, distance = path(source_port, target_port)
            log_lot(
                "material.route",
                lot_id,
                op,
                task_id=tid,
                distance_m=distance,
                shapes=route_shapes(
                    engine.net, edges, config["ports"][source_port], config["ports"][target_port]
                ),
            )
            log_lot("task.ready", lot_id, op, task_id=tid, port=source_port)
            yield task["delivered"]
            state["equipment"] = target

        def operation_log(kind: str, batch: list[dict], **fields) -> None:
            for job in batch:
                lot_id, op = job["lot_id"], job["op"]
                state, lot = states[lot_id], lots[lot_id]
                if kind in ("setup.start", "processing.start", "tool.load_start"):
                    locate(lot, state["equipment"] + ".tool")
                    state["phase"] = (
                        "setup"
                        if kind.startswith("setup")
                        else "loading_tool"
                        if kind.startswith("tool.load")
                        else "processing"
                    )
                if kind == "processing.start":
                    if state["equipment"] in state["seen"]:
                        log_lot("lot.reentered", lot_id, op, equipment=state["equipment"])
                    state["seen"].add(state["equipment"])
                    if op["dedication_step"]:
                        state["dedication"][op["dedication_step"]] = state["equipment"]
                        log_lot(
                            "dedication.bound",
                            lot_id,
                            op,
                            target_step=op["dedication_step"],
                            equipment=state["equipment"],
                        )
                    for window in list(state["cqt"]):
                        if window["target_step"] == op["source_step"]:
                            log_lot(
                                "cqt.checked",
                                lot_id,
                                op,
                                **window,
                                violated=env.now > window["deadline_s"] + 1e-8,
                            )
                            state["cqt"].remove(window)
                if kind == "processing.end":
                    lot.completed_operations += 1
                    state["processed_steps"].append(op["source_step"])
                    if op["cqt_step"]:
                        state["cqt"].append(
                            {
                                "origin_step": op["source_step"],
                                "target_step": op["cqt_step"],
                                "deadline_s": round(env.now + op["cqt_s"], 9),
                            }
                        )
                    state["phase"] = "unloading_tool"
                log_lot(kind, lot_id, op, occurrence=job["occurrence"], **fields)

        def sample(job: dict) -> float:
            op = job["op"]
            if config["production"]["processing_distribution"] == "nominal_mean":
                return op["processing_s"]
            return random_streams[job["lot_id"], "processing"].uniform(
                op["processing_s"] - op["offset_s"], op["processing_s"] + op["offset_s"]
            )

        def batch_formed(name: str, batch: list[dict]):
            op = batch[0]["op"]
            if op["unit"] == "Batch":
                wafers = sum(states[j["lot_id"]]["wafer_count"] for j in batch)
                record(
                    "batch.formed",
                    equipment=name,
                    lot_ids=[j["lot_id"] for j in batch],
                    source_step=op["source_step"],
                    wafers=wafers,
                    minimum=op["batch_min"],
                    maximum=op["batch_max"],
                    underfilled=wafers < op["batch_min"],
                )
        def finish_batch(name: str, batch: list[dict]) -> None:
            for job in batch:
                job["done"].succeed()
            if deferred_dispatch:
                machine_busy.discard(name)
                dispatch_machine(name)

        def process_batch(name: str, batch: list[dict]):
            if pure_production:
                return BatchRoutine(production_context, name, batch)
            return vehicle_batch(name, batch)

        def vehicle_batch(name: str, batch: list[dict]):
            batch_formed(name, batch)
            yield env.process(
                machines[name].process(
                    batch[0]["op"],
                    [states[j["lot_id"]]["wafer_count"] for j in batch],
                    lambda: sample(batch[0]),
                    lambda kind, **fields: operation_log(kind, batch, **fields),
                )
            )
            finish_batch(name, batch)

        def remaining_nominal(job: dict) -> float:
            """剩余名义加工时间：按冻结路线源均值从当前步求到末步，不含抽检/返工。"""
            return config["routes"][job["route"]]["remaining_s"][job["op"]["source_step"]]

        def cqt_deadline(job: dict) -> float | None:
            """作业当前步骤上未闭CQT窗口的最近截止时刻；没有窗口时返回None。"""
            windows = states[job["lot_id"]]["cqt"]
            target = job["op"]["source_step"]
            deadlines = [
                window["deadline_s"]
                for window in windows
                if window["target_step"] == target
            ]
            return min(deadlines) if deadlines else None

        def dispatch_machine(name: str) -> None:
            queue = requests[name]
            while queue:
                if deferred_dispatch and name in machine_busy:
                    return
                eligible = queue
                if legal_candidates_only and not cohorts:
                    # 欠批组不能挡住同台上已合法的Lot工序；策略只看实际能开的候选。
                    eligible = [candidate for candidate in queue if candidate["op"]["unit"] != "Batch"]
                    groups = {}
                    for candidate in queue:
                        if candidate["op"]["unit"] == "Batch":
                            groups.setdefault((candidate["route"], candidate["op"]["source_step"]), []).append(candidate)
                    for group in groups.values():
                        eligible.extend(select_queue_batch(
                            group, {j["lot_id"]: states[j["lot_id"]]["wafer_count"] for j in group},
                            env.now, config["production"]["batch_wait_s"],
                            config["production"]["allow_underfilled_after_timeout"],
                        ))
                if policies["names"]["production.job"] in ("implant_setup_fifo", "implant_setup_fifo_3groups"):
                    job = rules["production.job"](
                        eligible, now=env.now, remaining=remaining_nominal,
                        tool_group=assets[name]["tool_group"],
                        setup_duration=machines[name].setup_duration,
                    )
                elif policies["names"]["production.job"] == "source_baseline":
                    job = rules["production.job"](
                        eligible, now=env.now, remaining=remaining_nominal,
                        tool_group=assets[name]["tool_group"],
                        machine=machines[name],
                        cqt_deadline=cqt_deadline,
                    )
                else:
                    job = rules["production.job"](eligible, now=env.now, remaining=remaining_nominal)
                if job is None:
                    return
                op = job["op"]
                if op["unit"] != "Batch":
                    batch = [take(queue, job)]
                elif cohorts:
                    ready_batches = []
                    for candidate in queue:
                        members = cohorts[
                            candidate["route"], candidate["op"]["source_step"], candidate["lot_id"]
                        ]
                        group = [
                            member
                            for member in queue
                            if member["route"] == candidate["route"]
                            and member["op"]["source_step"] == candidate["op"]["source_step"]
                            and member["lot_id"] in members
                        ]
                        if {member["lot_id"] for member in group} == set(members):
                            ready_batches.append(group)
                    batch = rules["production.group"](
                        ready_batches, now=env.now, remaining=remaining_nominal
                    )
                    if batch is None:
                        return
                    for member in batch:
                        take(queue, member)
                else:
                    # queue_batch的成批规则在production.batching中固定，不由生产策略选择。
                    batch = select_queue_batch(
                        [candidate for candidate in queue
                         if (candidate["route"], candidate["op"]["source_step"])
                         == (job["route"], op["source_step"])] if legal_candidates_only else queue,
                        # 选批函数只读取当前机台队列中的lot，不必为每次派工
                        # 重建全部未来/完工lot的数量表；保留调用时的实际wafer数。
                        {
                            candidate["lot_id"]: states[candidate["lot_id"]]["wafer_count"]
                            for candidate in queue
                        },
                        env.now,
                        config["production"]["batch_wait_s"],
                        config["production"]["allow_underfilled_after_timeout"],
                    )
                    if not batch:
                        return
                    for member in batch:
                        take(queue, member)
                if deferred_dispatch:
                    machine_busy.add(name)
                if pure_production:
                    record(
                        "production.dispatched", equipment=name,
                        lot_ids=[member["lot_id"] for member in batch],
                        nonbatch_candidates=sum(candidate["op"]["unit"] != "Batch" for candidate in queue)
                        + sum(member["op"]["unit"] != "Batch" for member in batch),
                    )
                env.process(process_batch(name, batch))
                if deferred_dispatch:
                    # 一次只启动一批；本批加工结束后process_batch会再派一次。
                    return

        def batch_deadline(name: str):
            if pure_production:
                return DeadlineRoutine(production_context, name)
            return vehicle_deadline(name)

        def vehicle_deadline(name: str):
            yield env.timeout(grid_delay(env.now, config["production"]["batch_wait_s"], step))
            dispatch_machine(name)

        def release_lot(definition: dict) -> None:
            lot_id = definition["id"]
            state, lot = states[lot_id], lots[lot_id]
            state["actual_release_s"] = env.now
            # seed/purpose与创建顺序和旧实现相同，但推迟到投片之后：尚未投片的lot不占流。
            for purpose in ("sampling", "rework", "processing"):
                seed = hashlib.sha256(
                    f"{config['seed']}:{definition['id']}:{purpose}".encode()
                ).digest()
                random_streams[definition["id"], purpose] = random.Random(seed)
            state["equipment"] = sorted(config["candidates"]["COMMIT"])[0]
            locate(lot, state["equipment"] + ".queue")
            # 必须在位置赋值之后加入，否则队列计数会漏掉这一拍。
            active_lots.add(lot_id)
            log_lot("lot.released", lot_id)

        def next_operation(lot_id: str):
            state = states[lot_id]
            route = config["routes"][state["route"]]["operations"]
            while state["operation_index"] < len(route):
                op = route[state["operation_index"]]
                if op.get("excluded_from_execution", False):
                    state["excluded_steps"].append(op["source_step"])
                    log_lot("operation.excluded", lot_id, op, reason="delay32_scope")
                    state["operation_index"] += 1
                    continue
                state["visits"] += 1
                if state["visits"] > config["production"]["max_visits_per_lot"]:
                    raise RuntimeError("返工访问超过上限，不强行完成lot")
                if random_streams[lot_id, "sampling"].random() >= op["sample_probability"]:
                    state["skipped_steps"].append(op["source_step"])
                    state["passed_steps"].append(op["source_step"])
                    log_lot("operation.skipped", lot_id, op, probability=op["sample_probability"])
                    for window in list(state["cqt"]):
                        if window["target_step"] == op["source_step"]:
                            log_lot("cqt.skipped", lot_id, op, **window)
                            state["cqt"].remove(window)
                    state["operation_index"] += 1
                    continue
                return op
            return None

        def enqueue_job(lot_id: str, target: str, op: dict) -> dict:
            state = states[lot_id]
            job = {
                "lot_id": lot_id,
                "route": state["route"],
                "op": op,
                "ready_s": env.now,
                "due_s": state["due_s"],
                "priority": state["priority"],
                "done": env.event(),
                "occurrence": state["visits"],
            }
            requests[target].append(job)
            if (
                op["unit"] == "Batch"
                and not cohorts
                and config["production"]["allow_underfilled_after_timeout"]
            ):
                env.process(batch_deadline(target))
            dispatch_machine(target)
            return job

        def after_operation(lot_id: str, op: dict) -> None:
            state = states[lot_id]
            state["passed_steps"].append(op["source_step"])
            if random_streams[lot_id, "rework"].random() < op["rework_probability"]:
                log_lot("lot.rework", lot_id, op, target_step=op["rework_step"])
                state["operation_index"] = op["rework_step"] - 1
            else:
                state["operation_index"] += 1

        def finish_lot(lot_id: str) -> None:
            state, lot = states[lot_id], lots[lot_id]
            locate(lot, "completed")
            state["phase"] = "completed"
            state["completion_s"] = round(env.now, 9)
            log_lot("lot.completed", lot_id)
            state["done"].succeed()
            completed_counter.value += 1
            active_lots.discard(lot_id)
            # 完工后不再抽样；只回收本lot的三个流，未完工lot不受影响。
            for purpose in ("sampling", "rework", "processing"):
                del random_streams[lot_id, purpose]


        def produce(definition: dict):
            if pure_production:
                return LotRoutine(production_context, definition)
            return vehicle_produce(definition)

        def vehicle_produce(definition: dict):
            lot_id = definition["id"]
            state, lot = states[lot_id], lots[lot_id]
            yield env.timeout(grid_delay(0, definition["release_s"], step))
            release_lot(definition)
            while (op := next_operation(lot_id)) is not None:
                target = destination(lot_id, op)
                state["next_port"] = assets[target]["input_port"]
                log_lot("destination.selected", lot_id, op, equipment=target)
                yield env.process(transport(lot_id, target, op))
                state["phase"] = "waiting_tool"
                if op["virtual"]:
                    with delay_resources.request() as request:
                        yield request
                        state["phase"] = "process_delay"
                        log_lot("delay.start", lot_id, op, equipment=target)
                        yield env.timeout(grid_delay(env.now, sample({"op": op, "lot_id": lot_id}), step))
                        lot.completed_operations += 1
                        state["processed_steps"].append(op["source_step"])
                        log_lot("delay.end", lot_id, op, equipment=target)
                else:
                    yield enqueue_job(lot_id, target, op)["done"]
                after_operation(lot_id, op)
            exit_name = sorted(config["candidates"]["COMPLETE"])[0]
            yield env.process(transport(lot_id, exit_name, None))
            finish_lot(lot_id)

        def pickup(vid: str, task: dict):
            lot_id = task["lot_id"]
            log_lot("transport.pickup_start", lot_id, task_id=task["id"], vehicle_id=vid)
            yield env.timeout(grid_delay(env.now, config["pickup_s"], step))
            vehicles[vid]["cargo"] = take_from_port(ports[task["source"]], vid)
            if vehicles[vid]["cargo"] is not lots[lot_id]:
                raise RuntimeError("车辆取错lot")
            notify(task["source"])
            vehicles[vid]["phase"], task["state"], states[lot_id]["phase"] = (
                "loaded",
                "in_transit",
                "transport",
            )
            log_lot("transport.pickup_end", lot_id, task_id=task["id"], vehicle_id=vid)
            engine.plan(vid, config["ports"][task["target"]])

        def dropoff(vid: str, task: dict):
            nonlocal completed_task_count
            lot_id, vehicle = task["lot_id"], vehicles[vid]
            log_lot("transport.dropoff_start", lot_id, task_id=task["id"], vehicle_id=vid)
            yield env.timeout(grid_delay(env.now, config["dropoff_s"], step))
            port = ports[task["target"]]
            put_on_port(port, vehicle["cargo"], vid)
            if take_from_port(port, task["destination"] + ".queue") is not lots[lot_id]:
                raise RuntimeError("输入缓冲物料错误")
            port.release(task["id"])
            notify(task["target"])
            task["state"], task["delivery_s"] = "completed", round(env.now, 9)
            log_lot("port.released", lot_id, task_id=task["id"], port=task["target"])
            log_lot("transport.delivered", lot_id, task_id=task["id"], vehicle_id=vid)
            vehicle.update(phase="idle", task=None, cargo=None)
            task["delivered"].succeed()
            engine.plan(vid, None)
            # 交接全部走完、engine.plan 未抛异常后才回收，异常路径与回收前逐字相同。
            # 此刻 SimPy 还没让被唤醒的 transport() 续跑，dropoff/transport 的栈帧仍持有
            # task 对象，删表只释放字典条目与终态对象，不触碰任何在途引用。
            completed_task_count += 1
            del tasks[task["id"]]

        production_context = SimpleNamespace(
            env=env, config=config, states=states, lots=lots, assets=assets, step=step,
            buffers=production_buffers, transfer_s=transfer_s, delay_resources=delay_resources,
            machines=machines, locate=locate, log_lot=log_lot, transport=transport,
            batch_formed=batch_formed, finish_batch=finish_batch, sample=sample,
            operation_log=operation_log, dispatch_machine=dispatch_machine,
            release_lot=release_lot, next_operation=next_operation, destination=destination,
            enqueue_job=enqueue_job, after_operation=after_operation, finish_lot=finish_lot,
        )
        for definition in config["lots"]:
            env.process(produce(definition))
        drain(env, 0)
        write_inventory()
        if material_index is None:
            _check_material(lots, ports, vehicles, material_location_cache)
        else:
            material_index.check(
                vehicles, material_location_cache, full=material_audit == "every_step"
            )
        last_full_material_check = 0.0
        if queue_occupancy is not None:
            queue_occupancy.verify(lots[lid] for lid in active_lots)
        last_full_queue_check = 0.0
        now, last_frame, last_report, stationary_jumps = 0.0, -math.inf, 0.0, 0
        traffic_stopped_since = None
        local_stopped_since = {}
        first_dispatch = True
        # 显式启用事件唤醒时，上一轮派过车或首轮才需要保留网格候选。
        # 理由见下面候选时刻处的注释。
        assigned_last_iteration = False
        # 完工lot只增不减，用计数代替每轮全lot扫描。绝不能用not active_lots判完成：
        # horizon前未投片的lot不在active_lots里，会误判提前结束并绕过TimeoutError。
        finished = completed_counter.value == len(states)
        checkpoint_control = None
        if checkpoint_enabled:
            from fabcosim.cosim.checkpoint_runtime import CheckpointControl
            checkpoint_control = CheckpointControl(settings, output, event_log, history, wall_started)

        def after_restore():
            # 对象id在新进程改变；纯生产无Port/cargo，只重建身份索引，不能用旧整数id。
            if material_index is not None:
                material_index._registered = {id(lot): lot for lot in lots.values()}
                for field in ("_dirty", "_held", "_cargo"):
                    values = getattr(material_index, field).values()
                    setattr(material_index, field, {id(lot): lot for lot in values})
                material_index._owner = {}
                material_index._holders = {}
                material_index._dirty_ports = {}

        def execute():
            nonlocal now, last_frame, last_report, stationary_jumps, motion
            nonlocal last_full_material_check, last_full_queue_check, finished
            nonlocal traffic_stopped_since, first_dispatch, assigned_last_iteration
            if pure_production:
                # 只有SimPy生产事件驱动时钟；不创建运输引擎、路网、端口或运输任务。
                while now < config["max_time_s"] and (observation or not finished):
                    upcoming = env.peek()
                    if not math.isfinite(upcoming) and not observation:
                        raise RuntimeError("无后续生产事件且未排空；检查严格欠批和生产缓冲，不强制完成")
                    now = min(upcoming, last_report + 86400, config["max_time_s"],
                          checkpoint_control.next_s if checkpoint_control else math.inf)
                    env.timeout(max(0, now - env.now))
                    drain(env, now)
                    if material_index is None:
                        _check_material(lots, ports, vehicles, material_location_cache)
                    else:
                        full_due = material_audit == "every_step" or now - last_full_material_check >= 86400
                        material_index.check(vehicles, material_location_cache, full=full_due)
                        if full_due:
                            last_full_material_check = now
                    if now - last_report >= 86400 or now == config["max_time_s"]:
                        if not production_buffers.verify():
                            raise RuntimeError("生产输入预留与实际容量不一致")
                        if queue_occupancy is not None:
                            queue_occupancy.verify(lots[lid] for lid in active_lots)
                        write_inventory()
                        print(f"纯生产第{now / 86400:.1f}天：出厂{completed_counter.value}，WIP{len(active_lots)}", flush=True)
                        last_report = now
                    finished = completed_counter.value == len(states)
                    if checkpoint_control is not None and now >= checkpoint_control.next_s:
                        checkpoint_control.save(payload, now)
                if not production_buffers.verify():
                    raise RuntimeError("生产输入预留与实际容量不一致")
            while not pure_production and now < config["max_time_s"]:
                event_start = len(events)
                idle = all(v["phase"] == "idle" for v in vehicles.values())
                ready = bool(ready_ids)
                if idle and not ready and engine.present():
                    # 同轨道前车先选择较前停车点，后车选择不重叠的停车点。
                    # 聚合档也要走这一段：它决定车辆被记为"停车"还是"空闲"，
                    # 关掉会使统计层把空闲车辆算成阻塞（曾实测误报96%—99%阻塞）。
                    for vid in sorted(
                        engine.ids,
                        key=lambda v: (engine.position(v)["edge"], -engine.position(v)["position_m"]),
                    ):
                        if vid in engine.parking and not engine.arrived(vid, engine.parking[vid]):
                            leader = engine.leader(vid, config["traffic"]["lookahead_m"])
                            if (
                                engine.speed(vid) < 1e-8
                                and leader
                                and leader[0] != vid
                                and leader[0] in engine.parking
                                and engine.arrived(leader[0], engine.parking[leader[0]])
                            ):
                                # 汇入后的实际顺序可能不同于最初分配；在合法位置重新待命。
                                engine.parking.pop(vid)
                                record("vehicle.parking_reconsidered", vehicle_id=vid, leader=leader[0])
                        if vid not in engine.parking:
                            engine.park(vid)
                elif ready or not idle:
                    for vid in list(engine.parking):
                        if vehicles[vid]["phase"] == "idle":
                            engine.plan(vid, None)
                if aggregate_transport:
                    # 聚合档没有停车点与区域控制，"停稳"等价于没有在途车辆。
                    stationary = idle and not ready and engine.next_arrival() is None
                else:
                    stationary = (
                        idle
                        and not ready
                        and len(engine.parking) == len(vehicles)
                        and all(
                            engine.arrived(v, engine.parking[v]) and engine.speed(v) < 1e-8
                            for v in engine.ids
                        )
                    )
                next_time = min(next_grid_time(now, step), config["max_time_s"])
                if aggregate_transport:
                    # 无固定步长：跳到下一个生产事件或运输到达，避免每步空转。
                    # 这是该档的主要收益来源，不是"算得更快"。
                    candidates = [config["max_time_s"]]
                    production_event = env.peek()
                    if math.isfinite(production_event):
                        production_time = grid_time_at_or_after(production_event, step)
                        # 八年量级时1 ULP可能大于drain的绝对容差。若ULP网格补偿
                        # 向下取整后无法消费真实事件，直接唤醒到该事件时刻；不增容差、
                        # 不额外等待一整步，也不让下一轮永久重选当前时刻。
                        if production_event > production_time + 1e-8:
                            production_time = production_event
                        candidates.append(production_time)
                    arrival = engine.next_arrival()
                    if arrival is not None:
                        candidates.append(arrival)
                    if (
                        ready
                        and any(v["phase"] == "idle" for v in vehicles.values())
                        and (not delay_event_wakeup or first_dispatch or assigned_last_iteration)
                    ):
                        # 默认保留旧式每格唤醒，确保既有统计输出逐字节可复现。
                        # 显式启用事件唤醒后：派单发生在本轮靠后，计划时刻要等派单完成才知道，
                        # 所以**刚派过车的那一轮之后必须再走一格**：零距离空驶的
                        # arrive_s 恰好等于 master_time，next_arrival() 只报严格未来
                        # 的时刻，报不出它，只有再走一步才能让到站判定看见车已到。
                        # 首轮同理，要留一格消费 time=0 的就绪任务。
                        # 反过来，只要上一轮没派成，就说明目标缓冲/端口全被占住：
                        # 能解开它的只有 SimPy 事件或运输到达，两者都已在候选里，
                        # 而 ready_ids 只在 SimPy 物料事件里增长、只在派单里缩短，
                        # 所以每格空转不改变任何状态，只是把迭代数堆成冗余倍数。
                        candidates.append(next_grid_time(now, step))
                    next_time = min(candidates)
                if (
                    stationary
                    and not math.isfinite(env.peek())
                    and not all(s["done"].triggered for s in states.values())
                    and not observation
                ):
                    raise RuntimeError(
                        "车队停稳且无后续生产事件，场景无法继续；检查批量下限/缓冲约束，不强制排空"
                    )
                stationary_jump = stationary and env.peek() > next_time + 1e-8
                if stationary_jump:
                    next_time = min(
                        config["max_time_s"],
                        grid_time_at_or_after(env.peek(), step)
                        if math.isfinite(env.peek())
                        else config["max_time_s"],
                    )
                if replay_enabled:
                    for boundary in (replay_start, replay_end):
                        if now < boundary < next_time:
                            next_time = boundary
                if stationary_jump:
                    engine.control(now)
                    engine.advance_stationary(next_time)
                    stationary_jumps += 1
                else:
                    engine.step(next_time)
                now = next_time
                # 物料只由SimPy生产/交接事件修改。先看事件队列，再插入主控的
                # timeout；纯运输积分不会改变lot归属，无需重复全厂检查。
                material_changed = env.peek() <= now + 1e-8
                env.timeout(max(0, now - env.now))
                drain(env, now)
                for vid, vehicle in vehicles.items():
                    task = vehicle["task"]
                    if task is None:
                        continue
                    goal = config["ports"][
                        task["source"] if vehicle["phase"] == "empty" else task["target"]
                    ]
                    if vehicle["phase"] in ("empty", "loaded") and engine.arrived(vid, goal):
                        material_changed = True
                        action = pickup if vehicle["phase"] == "empty" else dropoff
                        vehicle["phase"] = "pickup" if action == pickup else "dropoff"
                        env.process(action(vid, task))
                drain(env, now)
                # 派单期间不运行SimPy事件；队列存量固定，入站数量只随本轮
                # 分配增加。无空闲车时无需反复排序和扫描全厂lot。
                # 派单条件仅由生产/交接事件改变；首轮还需消费time=0的就绪任务。
                dispatch_changed = first_dispatch or material_changed
                first_dispatch = False
                assigned_this_iteration = False
                available = (
                    [v for v, value in vehicles.items() if value["phase"] == "idle"]
                    if dispatch_changed else []
                )
                queued_counts = {}
                inbound_counts = {}
                for value in vehicles.values() if available else ():
                    if value["task"] is not None:
                        inbound_destination = value["task"]["destination"]
                        inbound_counts[inbound_destination] = (
                            inbound_counts.get(inbound_destination, 0) + 1
                        )
                for task in rules["transport.task"](
                    (tasks[tid] for tid in ready_ids) if available else (),
                    haul=lambda t: path(t["source"], t["target"])[1],
                ):
                    if not available:
                        break
                    port = ports[task["target"]]
                    target = task["destination"]
                    if target not in queued_counts:
                        # 未投片是release_queue、完工是completed，都不可能命中".queue"，
                        # 故只扫已投片未完工的lot与全量扫描结果逐项相同。
                        queued_counts[target] = (
                            queue_occupancy.count(target) if queue_occupancy is not None else sum(
                                lots[lid].location == target + ".queue" for lid in active_lots
                            )
                        )
                    queued = queued_counts[target]
                    inbound = inbound_counts.get(target, 0)
                    if queued + inbound >= config["production"]["input_buffer_lots"]:
                        continue
                    if not port.reserve(task["id"]):
                        continue
                    vid = rules["transport.vehicle"](
                        available, lambda v: engine.distance(v, config["ports"][task["source"]])
                    )
                    task["state"], task["vehicle_id"] = "assigned", vid
                    ready_ids.remove(task["id"])
                    vehicles[vid].update(phase="empty", task=task)
                    available.remove(vid)
                    inbound_counts[target] = inbound + 1
                    log_lot(
                        "task.assigned",
                        task["lot_id"],
                        task_id=task["id"],
                        vehicle_id=vid,
                        port=task["target"],
                    )
                    engine.plan(vid, config["ports"][task["source"]])
                    assigned_this_iteration = True
                assigned_last_iteration = assigned_this_iteration
                if material_changed or material_audit == "every_step":
                    if material_index is None:
                        _check_material(lots, ports, vehicles, material_location_cache)
                    else:
                        # 不增加仿真事件或积分分段：跨24小时后的首个原审计边界全量复查。
                        # every_step仍按其诊断含义，每个边界都做全量检查。
                        full_due = material_audit == "every_step" or now - last_full_material_check >= 86400
                        material_index.check(vehicles, material_location_cache, full=full_due)
                        if full_due:
                            last_full_material_check = now
                    if queue_occupancy is not None and (
                        material_audit == "every_step" or now - last_full_queue_check >= 86400
                    ):
                        queue_occupancy.verify(lots[lid] for lid in active_lots)
                        last_full_queue_check = now
                if material_changed:
                    finished = completed_counter.value == len(states)
                snapshots = []
                for vid, v in vehicles.items():
                    snap = engine.snapshot(vid)
                    reason = snap["zone_wait"] if snap["speed"] < 0.01 else None
                    parked = vid in engine.parking and engine.arrived(vid, engine.parking[vid])
                    if (
                        not parked
                        and reason is None
                        and snap["speed"] < 0.01
                        and v["phase"] in ("idle", "empty", "loaded")
                    ):
                        leader = engine.leader(vid, 2)
                        if leader and leader[0] != vid and leader[1] <= 2 + 1e-8:
                            reason = f"following:{leader[0]}"
                    snap.update(
                        phase="parked" if parked else v["phase"],
                        waiting_reason=reason,
                        task_id=v["task"]["id"] if v["task"] else None,
                        lot_id=v["cargo"].id if v["cargo"] else None,
                    )
                    snapshots.append(snap)
                if traffic_metrics is not None:
                    traffic_metrics.observe(now, snapshots)
                needs_motion = any(
                    vehicles[v]["phase"] in ("empty", "loaded")
                    or (v in engine.parking and not engine.arrived(v, engine.parking[v]))
                    for v in engine.ids
                )
                for snap in snapshots:
                    vid = snap["id"]
                    required = vehicles[vid]["phase"] in ("empty", "loaded") or (
                        vid in engine.parking and not engine.arrived(vid, engine.parking[vid])
                    )
                    if required and snap["speed"] < 1e-8:
                        local_stopped_since.setdefault(vid, now)
                    else:
                        local_stopped_since.pop(vid, None)
                local_limit = config["traffic"].get("local_stall_timeout_s")
                local_stalled = [
                    vid
                    for vid, since in local_stopped_since.items()
                    if local_limit is not None and now - since >= local_limit
                ]
                if needs_motion and all(s["speed"] < 1e-8 for s in snapshots):
                    if traffic_stopped_since is None:
                        traffic_stopped_since = now
                else:
                    traffic_stopped_since = None
                if local_stalled or (
                    traffic_stopped_since is not None
                    and now - traffic_stopped_since >= config["traffic"].get("stall_timeout_s", 300)
                ):
                    # 保存实际等待关系，不能通过删车或强制交付继续运行。
                    (output / "traffic-stall.json").write_text(
                        json.dumps(
                            {
                                "time": now,
                                "local_stalled": local_stalled,
                                "vehicles": snapshots,
                                **engine.stall_state(),
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    raise RuntimeError("有未完成运动但车队持续静止，现场见traffic-stall.json")
                recording = replay_enabled and replay_start <= now <= replay_end
                if motion is not None and recording:
                    motion.record(now, snapshots)
                business = recording and any(
                    not e["kind"].startswith(("zone.", "vehicle.", "module."))
                    for e in events[event_start:]
                )
                if recording and (
                    finished
                    or now in (replay_start, replay_end)
                    or now >= config["max_time_s"]
                    or business
                    or now - last_frame >= config["frame_interval_s"]
                ):
                    frames.append(
                        {
                            "time": now,
                            "vehicles": snapshots,
                            "lots": [
                                {
                                    "id": lid,
                                    "location": lot.location,
                                    "route": states[lid]["route"],
                                    "phase": states[lid]["phase"],
                                    "operation_index": min(
                                        states[lid]["operation_index"],
                                        len(config["routes"][states[lid]["route"]]["operations"]) - 1,
                                    ),
                                    "source_step": min(
                                        states[lid]["operation_index"] + 1,
                                        len(config["routes"][states[lid]["route"]]["operations"]),
                                    ),
                                    "operation_count": len(
                                        config["routes"][states[lid]["route"]]["operations"]
                                    ),
                                    "completed_operations": lot.completed_operations,
                                    "passed_count": len(set(states[lid]["passed_steps"])),
                                    "equipment": states[lid]["equipment"],
                                    "next_port": states[lid].get("next_port"),
                                }
                                for lid, lot in lots.items()
                            ],
                            "ports": {
                                name: {
                                    "lot_id": p.lot.id if p.lot else None,
                                    "reserved_by": p.reserved_by,
                                }
                                for name, p in ports.items()
                                if p.lot or p.reserved_by
                            },
                            "zones": engine.zone_owners(),
                            "zone_queues": engine.zone_queues(),
                            "completed_tasks": completed_task_count,
                            "completed_lots": sum(s["done"].triggered for s in states.values()),
                            "pending_tasks": len(ready_ids),
                        }
                    )
                    last_frame = now
                if now - last_report > 86400:
                    write_inventory()
                    (output / "checkpoint.json").write_text(
                        json.dumps(
                            {
                                "time": now,
                                "vehicles": [engine.snapshot(vid) for vid in engine.ids],
                                **engine.zone_state(),
                                "lots": {
                                    lid: {
                                        "source_step": min(
                                            state["operation_index"] + 1,
                                            len(config["routes"][state["route"]]["operations"]),
                                        ),
                                        "phase": state["phase"],
                                        "location": lots[lid].location,
                                    }
                                    for lid, state in states.items()
                                },
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    if observation:
                        released_count = sum(s["phase"] != "unreleased" for s in states.values())
                        complete_count = sum(s["done"].triggered for s in states.values())
                        print(
                            f"仿真第{now / 86400:.1f}天：释放{released_count}，"
                            f"出厂{complete_count}，WIP{released_count - complete_count}",
                            flush=True,
                        )
                    else:
                        print(
                            f"仿真第{now / 86400:.1f}天："
                            + ", ".join(
                                f"{lid}已出厂"
                                if s["phase"] == "completed"
                                else f"{lid}步骤{min(s['operation_index'] + 1, len(config['routes'][s['route']]['operations']))}"
                                for lid, s in states.items()
                            ),
                            flush=True,
                        )
                    last_report = now
                if finished and not observation:
                    break
            if material_index is not None:
                material_index.check(vehicles, material_location_cache, full=True)
            if queue_occupancy is not None:
                queue_occupancy.verify(lots[lid] for lid in active_lots)
            if completed_counter.value != len(states) and not observation:
                raise TimeoutError("完整工艺未排空，保存现场；不删工序、不强制完成lot")
            if history is not None or task_history is not None:
                # 时间循环已经结束；先封口业务流，后续SQL排序/统计失败也保留完整日志。
                if event_log is not None:
                    event_log.close()
            if task_history is not None:
                task_history.close()
            if history is not None:
                # 正常结束先finalize：未取货请求按右删失落盘后，窗口统计才能查询。
                history.finalize()
            # SQLite模式用缓存计数；memory模式继续扫描内存事件，原表达式不变。
            hc = history.summary_counts(assets) if history is not None else None
            summary.update(
                status="window_completed" if observation else "completed",
                scope="continuous_release_declared_policies" if observation else summary["scope"],
                end_time_s=now,
                observation=observation,
                replay_recorded=replay_enabled,
                replay_window={"start_s": replay_start, "end_s": replay_end}
                if replay_enabled
                else None,
                completion_s=now if not observation else None,
                completed_lots=sum(s["done"].triggered for s in states.values()),
                released_lots=sum(s["phase"] != "unreleased" for s in states.values()),
                completed_tasks=completed_task_count,
                wip_lots=sum(s["phase"] not in ("unreleased", "completed") for s in states.values()),
                completed_operations=sum(lot.completed_operations for lot in lots.values()),
                source_routes=config["source_catalog"]["routes"],
                route_step_counts={n: len(r["operations"]) for n, r in config["routes"].items()},
                facility_count=len(assets),
                available_tool_count=len(machines),
                visited_tool_count=hc["visited_tool_count"] if hc is not None else len(
                    {e["equipment"] for e in events if e["kind"] == "processing.start"}
                ),
                visited_tool_groups=hc["visited_tool_groups"] if hc is not None else sorted(
                    {
                        assets[e["equipment"]]["tool_group"]
                        for e in events
                        if e["kind"] == "processing.start"
                    }
                ),
                physical_processing_operations=hc["physical_processing_operations"]
                if hc is not None
                else sum(e["kind"] == "processing.end" for e in events),
                delay_operations=hc["delay_operations"]
                if hc is not None
                else sum(e["kind"] == "delay.end" for e in events),
                vehicle_count=len(vehicles),
                zone_grants=zone_grants,
                stationary_jumps=stationary_jumps,
                transport_integrator_time_s=engine.integrator_time() if engine is not None else None,
                static_time_offset_s=engine.time_offset if engine is not None else None,
                collisions=engine.collisions if engine is not None else None,
                teleports=engine.teleports if engine is not None else None,
                batch_count=hc["batch_count"]
                if hc is not None
                else sum(e["kind"] == "batch.formed" for e in events),
                underfilled_batches=hc["underfilled_batches"]
                if hc is not None
                else sum(e["kind"] == "batch.formed" and e["underfilled"] for e in events),
                availability=availability_config if availability_on else None,
                availability_outages=hc["availability_outages"]
                if hc is not None
                else sum(e["kind"] == "availability.down" for e in events),
                availability_down_s=hc["availability_down_s"]
                if hc is not None
                else sum(
                    e["duration_s"] for e in events if e["kind"] == "availability.down"
                ),
                reworks=hc["reworks"]
                if hc is not None
                else sum(e["kind"] == "lot.rework" for e in events),
                cqt_violations=hc["cqt_violations"]
                if hc is not None
                else sum(e["kind"] == "cqt.checked" and e["violated"] for e in events),
                lot_results=[
                    {
                        "lot_id": lid,
                        "route": states[lid]["route"],
                        "completion_s": states[lid].get("completion_s"),
                        "release_s": definitions_by_id[lid]["release_s"],
                        # 未声明交期的场景不写该字段，既有运行的summary保持逐字节可比。
                        **({"due_s": states[lid]["due_s"]} if states[lid]["due_s"] else {}),
                        "actual_release_s": states[lid].get("actual_release_s"),
                        "phase": states[lid]["phase"],
                        "location": lots[lid].location,
                        "wafer_count": states[lid]["wafer_count"],
                        "passed_steps": sorted(set(states[lid]["passed_steps"])),
                        "processed_steps": states[lid]["processed_steps"],
                        "skipped_steps": states[lid]["skipped_steps"],
                        **({"excluded_steps": states[lid]["excluded_steps"]} if pure_production else {}),
                    }
                    for lid in lots
                ],
            )
            if observation is None:
                # 有限场景没有统计窗口，按全程统计交期表现；未完工lot单列而不是算作准时。
                late = tardiness_metrics(
                    {d["id"]: d for d in config["lots"]},
                    {
                        lid: state["completion_s"]
                        for lid, state in states.items()
                        if state.get("completion_s") is not None
                    },
                    {lid: state.get("actual_release_s", math.inf) for lid, state in states.items()},
                    0,
                    now,
                )
                if late:
                    summary["tardiness"] = late
            if batch_host_policy == "compatible_buffer":
                (output / "batch-hosts.json").write_text(json.dumps({
                    "policy": batch_host_policy,
                    "meaning": "按最低兼容lot预算均衡的固定host绑定；不是物理占位或机台负载估计",
                    "input_buffer_lots": config["production"]["input_buffer_lots"],
                    "per_host_minimum_lots": batch_host_budget,
                    "bindings": [{"route": key[0], "source_step": key[1], "host": host,
                                  "minimum_lots": minimum_host_lots[key]}
                                 for key, host in batch_hosts.items()],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            if pure_production:
                summary["policies"] = {
                    key: value for key, value in summary["policies"].items()
                    if not key.startswith("transport.")
                }
                summary.update(
                    scope="production_only", transport_model="production_only",
                    fixed_transfer_s=transfer_s,
                    destination_assumption="zero_distance_using_declared_destination_policy",
                    logistics_metrics="not_available",
                    delay32_policy=config["production"]["delay32_policy"],
                    excluded_operations=sum(len(state["excluded_steps"]) for state in states.values()),
                    excluded_route_steps={name: route["excluded_source_steps"] for name, route in config["routes"].items()},
                    execution_route_step_counts={name: route["execution_step_count"] for name, route in config["routes"].items()},
                )
            if traffic_metrics is not None or (pure_production and observation):
                summary["metrics"] = window_metrics(
                    config, events, traffic_metrics.result() if traffic_metrics is not None else {}, history
                )
                if pure_production:
                    summary["metrics"]["transport_wait"] = None
                    summary["metrics"]["traffic"] = {"status": "not_available_for_production_only"}
                    if config.get("source_calendar") and "tardiness" in summary["metrics"]:
                        summary["metrics"]["tardiness"]["definition"] = "交期来自冻结投片CSV的DUE DATE"
                summary["analysis_windows"] = {}
                for definition in config.get("analysis_windows", []):
                    analysis_config = copy.copy(config)
                    analysis_config["observation"] = definition
                    summary["analysis_windows"][definition["name"]] = window_metrics(
                        analysis_config, events, {}, history
                    )
            if motion is not None:
                motion.close()
                motion = None
            if wafer_detail != 'micro':
                counts = Counter()
                for machine in machines.values():
                    counts.update(machine.wafer_counts)
                summary['wafer_detail'] = {
                    'mode': wafer_detail, 'counts': dict(counts),
                    'by_equipment': {name: dict(machine.wafer_counts) for name, machine in machines.items()
                                     if machine.wafer_counts},
                    'scope': 'lot-level service; internal wafer module traces not reproduced; no checkpoints',
                }
            if replay_enabled:
                write_full_replay(config, frames, events, engine.geometry, output)
            return summary
        payload = {"execute": execute, "cleanup": cleanup, "after_restore": after_restore,
                   "summary": summary, "control": checkpoint_control}
        return execute()
    except CheckpointPaused as exc:
        summary.update(status="checkpoint_saved", checkpoint=str(exc.path), end_time_s=exc.at)
        return summary
    except BaseException as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        raise
    finally:
        cleanup()
