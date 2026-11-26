"""多车、多任务生产—运输闭环；所有状态与动作使用同一个时间主控。"""

import copy
import json
import math
from pathlib import Path

import simpy

from fabcosim.cosim.demo import drain
from fabcosim.policies.dispatch import choose_vehicle
from fabcosim.ports.model import Lot, Port
from fabcosim.production.model import grid_delay
from fabcosim.production.routes import load_route_segments
from fabcosim.transport.fleet import FleetTransport
from fabcosim.transport.sumo import route_between, route_shapes


def run_multi(config: dict, output: Path) -> dict:
    config = copy.deepcopy(config)
    step = config["step_s"]
    if not math.isfinite(step) or step < 0.001 or abs(step * 1000 - round(step * 1000)) > 1e-7:
        raise ValueError("时间步必须为正整数毫秒")
    routed = "routes" in config
    if not config.get("lots" if routed else "tasks") or not config["vehicles"]:
        raise ValueError("多车场景至少需要一个任务和一辆车")
    if routed:
        load_route_segments(config, Path(__file__).resolve().parents[3])
    frame_ticks = max(1, round(config.get("frame_interval_s", step) / step))
    braking = config["oht"]["max_speed_m_s"] ** 2 / (2 * config["oht"]["decel_m_s2"])
    if (
        config["traffic"]["lookahead_m"]
        < braking + step * config["oht"]["max_speed_m_s"] + config["traffic"]["stop_margin_m"]
    ):
        raise ValueError("区域申请距离不足以安全停车")
    output.mkdir(parents=True, exist_ok=False)
    env = simpy.Environment()
    events, frames = [], []
    engine = None
    summary = {"status": "failed", "scope": "multi_vehicle_rules"}

    def record(kind: str, **fields) -> None:
        events.append({"time": round(env.now, 9), "kind": kind, **fields})

    try:
        engine = FleetTransport(config, output, record)
        if routed:
            engine.geometry["production_routes"] = {}
            for name, route in config["routes"].items():
                legs = []
                for a, b in zip(route["operations"], route["operations"][1:]):
                    source, target = (
                        config["ports"][a["output_port"]],
                        config["ports"][b["input_port"]],
                    )
                    edges, distance = route_between(engine.net, source, target)
                    legs.append(
                        {
                            "shapes": route_shapes(engine.net, edges, source, target),
                            "distance_m": distance,
                            "edges": edges,
                        }
                    )
                engine.geometry["production_routes"][name] = legs
        ports = {name: Port(f"{name}.port") for name in config["ports"]}
        tool_ids = {name: port.get("equipment", name) for name, port in config["ports"].items()}
        tools = {name: simpy.Resource(env, capacity=1) for name in set(tool_ids.values())}
        tasks, lots, work = {}, {}, []
        if routed:
            for definition in config["lots"]:
                operations = config["routes"][definition["route"]]["operations"]
                for op in operations:
                    a, b = config["ports"][op["input_port"]], config["ports"][op["output_port"]]
                    if a["equipment"] != b["equipment"] or a["tool_group"] != op["tool_group"]:
                        raise ValueError("工序设备组与真实输入/输出端口映射不一致")
                work.append({**definition, "operations": operations})
            definitions = [
                {
                    "id": f"{item['id']}.move{index + 1}",
                    "lot_id": item["id"],
                    "source": op["output_port"],
                    "target": item["operations"][index + 1]["input_port"],
                    "operation_index": index,
                }
                for item in work
                for index, op in enumerate(item["operations"][:-1])
            ]
            config["tasks"] = definitions
        else:
            definitions = config["tasks"]
            work = [
                {
                    "id": d["lot_id"],
                    "release_s": d["release_s"],
                    "task_ids": [d["id"]],
                    "operations": [
                        {
                            "input_port": p,
                            "output_port": p,
                            "processing_s": config["processing_s"][p],
                        }
                        for p in (d["source"], d["target"])
                    ],
                }
                for d in definitions
            ]
        progress, lot_done = {}, {}
        for item in work:
            if item["id"] in lots:
                raise ValueError("lot ID必须唯一")
            lots[item["id"]] = Lot(item["id"], "release_queue")
            lot_done[item["id"]] = env.event()
            progress[item["id"]] = {"operation_index": 0, "phase": "unreleased"}
            if routed:
                item["task_ids"] = [
                    f"{item['id']}.move{i + 1}" for i in range(len(item["operations"]) - 1)
                ]
        for definition in definitions:
            task = {
                **definition,
                "state": "unreleased",
                "vehicle_id": None,
                "delivered": env.event(),
                "done": env.event(),
                "wait_logged": False,
            }
            if task["id"] in tasks:
                raise ValueError("任务ID必须唯一")
            tasks[task["id"]] = task
        vehicles = {vid: {"phase": "idle", "task": None, "cargo": None} for vid in engine.ids}

        def log_task(kind: str, task: dict, **fields) -> None:
            record(
                kind,
                task_id=task["id"],
                lot_id=task["lot_id"],
                vehicle_id=task["vehicle_id"],
                location=lots[task["lot_id"]].location,
                **fields,
            )

        def produce(item: dict):
            lot = lots[item["id"]]
            state = progress[lot.id]
            yield env.timeout(grid_delay(0, item["release_s"], step))
            lot.location = f"{item['operations'][0]['input_port']}.queue"
            record("lot.released", lot_id=lot.id, location=lot.location)
            visited = set()
            for index, op in enumerate(item["operations"]):
                source, output_port = op["input_port"], op["output_port"]
                incoming = tasks[item["task_ids"][index - 1]] if index else None
                outgoing = tasks[item["task_ids"][index]] if index < len(item["task_ids"]) else None
                task = incoming or outgoing
                fields = {
                    "operation_index": index,
                    "source_step": op.get("source_step"),
                    "equipment": tool_ids[source],
                    "port": source,
                }
                state.update(operation_index=index, phase="waiting_tool")
                if outgoing:
                    outgoing["state"] = "processing_source"
                with tools[tool_ids[source]].request() as request:
                    yield request
                    if incoming:
                        if ports[source].take(f"{source}.tool") is not lot:
                            raise RuntimeError("目标端口物料错误")
                        ports[source].release(incoming["id"])
                        incoming["state"] = "processing_target"
                        log_task("port.released", incoming, port=source)
                    else:
                        lot.location = f"{source}.tool"
                    state["phase"] = "processing"
                    if tool_ids[source] in visited:
                        log_task("lot.reentered", task, **fields)
                    visited.add(tool_ids[source])
                    log_task("processing.start", task, **fields)
                    yield env.timeout(grid_delay(env.now, op["processing_s"], step))
                    lot.completed_operations += 1
                    log_task("processing.end", task, **fields)
                    if incoming:
                        incoming["state"] = "completed"
                        incoming["done"].succeed()
                    if outgoing:
                        state["phase"] = "waiting_output"
                        if (
                            ports[output_port].lot is not None
                            or ports[output_port].reserved_by is not None
                        ):
                            log_task("source_port.blocked", outgoing, port=output_port)
                        while (
                            ports[output_port].lot is not None
                            or ports[output_port].reserved_by is not None
                        ):
                            yield env.timeout(step)
                        ports[output_port].put(lot, f"{source}.tool")
                        outgoing["state"], outgoing["ready_s"] = "ready", env.now
                        state["phase"] = "awaiting_transport"
                        log_task("task.ready", outgoing, port=output_port)
                if outgoing:
                    yield outgoing["delivered"]
                else:
                    lot.location, state["phase"] = "completed", "completed"
                    state["completion_s"] = round(env.now, 9)
                    log_task("lot.completed", task)
                    lot_done[lot.id].succeed()

        def pickup(vid: str, task: dict):
            vehicle = vehicles[vid]
            log_task("transport.pickup_start", task)
            yield env.timeout(grid_delay(env.now, config["pickup_s"], config["step_s"]))
            vehicle["cargo"] = ports[task["source"]].take(vid)
            if vehicle["cargo"] is not lots[task["lot_id"]]:
                raise RuntimeError("车辆取错物料")
            log_task("transport.pickup_end", task)
            vehicle["phase"], task["state"] = "loaded", "in_transit"
            progress[task["lot_id"]]["phase"] = "transport"
            engine.plan(vid, config["ports"][task["target"]])

        def dropoff(vid: str, task: dict):
            vehicle = vehicles[vid]
            log_task("transport.dropoff_start", task)
            yield env.timeout(grid_delay(env.now, config["dropoff_s"], config["step_s"]))
            if ports[task["target"]].reserved_by != task["id"]:
                raise RuntimeError("交付前目标端口预留丢失")
            ports[task["target"]].put(vehicle["cargo"], vid)
            vehicle["cargo"] = None
            vehicle["phase"], vehicle["task"], task["state"] = "idle", None, "delivered"
            task["delivery_s"] = round(env.now, 9)
            log_task("transport.delivered", task)
            task["delivered"].succeed()
            engine.plan(vid, None)

        for item in work:
            env.process(produce(item))
        drain(env, 0)
        zone_wait_ticks = 0
        for tick in range(1, round(config["max_time_s"] / config["step_s"]) + 1):
            event_start = len(events)
            now = round(tick * config["step_s"], 9)
            engine.step(now)
            env.timeout(max(0, now - env.now))
            drain(env, now)
            for vid, vehicle in vehicles.items():
                task = vehicle["task"]
                if task is None:
                    continue
                if vehicle["phase"] == "empty" and engine.arrived(
                    vid, config["ports"][task["source"]]
                ):
                    vehicle["phase"] = "pickup"
                    env.process(pickup(vid, task))
                elif vehicle["phase"] == "loaded" and engine.arrived(
                    vid, config["ports"][task["target"]]
                ):
                    vehicle["phase"] = "dropoff"
                    env.process(dropoff(vid, task))
            drain(env, now)
            ready = sorted(
                (t for t in tasks.values() if t["state"] == "ready"),
                key=lambda t: (t["ready_s"], t["id"]),
            )
            for task in ready:
                idle = [vid for vid, vehicle in vehicles.items() if vehicle["phase"] == "idle"]
                if not idle:
                    break
                target_port = ports[task["target"]]
                if target_port.lot is not None or target_port.reserved_by is not None:
                    if not task["wait_logged"]:
                        log_task("task.waiting_destination", task, port=task["target"])
                        task["wait_logged"] = True
                    continue
                vid = choose_vehicle(
                    idle, lambda v: engine.distance(v, config["ports"][task["source"]])
                )
                if not target_port.reserve(task["id"]):
                    raise RuntimeError("端口重复预留")
                task["state"], task["vehicle_id"] = "assigned", vid
                vehicles[vid].update({"phase": "empty", "task": task})
                log_task("task.assigned", task, port=task["target"])
                engine.plan(vid, config["ports"][task["source"]])
            for lot in lots.values():
                holders = int(
                    lot.location in ("release_queue", "completed")
                    or lot.location.endswith((".tool", ".queue"))
                )
                holders += sum(port.lot is lot for port in ports.values())
                holders += sum(vehicle["cargo"] is lot for vehicle in vehicles.values())
                if holders != 1:
                    raise RuntimeError(f"{lot.id}持有者数量为{holders}")
            snapshots = []
            for vid, vehicle in vehicles.items():
                snapshot = engine.snapshot(vid)
                reason = None
                if vehicle["phase"] in ("empty", "loaded", "idle") and snapshot["speed"] < 0.01:
                    reason = snapshot["zone_wait"]
                    leader = engine.api.vehicle.getLeader(vid, 10)
                    if reason is None and leader:
                        reason = f"following:{leader[0]}"
                snapshot.update(
                    {
                        "phase": vehicle["phase"],
                        "waiting_reason": reason,
                        "task_id": vehicle["task"]["id"] if vehicle["task"] else None,
                        "lot_id": vehicle["cargo"].id if vehicle["cargo"] else None,
                    }
                )
                snapshots.append(snapshot)
            zone_wait_ticks += sum(
                v["waiting_reason"] is not None and v["waiting_reason"].startswith("zcu:")
                for v in snapshots
            )
            finished = all(done.triggered for done in lot_done.values())
            changed = any(
                e["kind"].startswith(
                    (
                        "processing.",
                        "lot.",
                        "transport.",
                        "task.assigned",
                        "task.ready",
                        "port.released",
                    )
                )
                for e in events[event_start:]
            )
            if tick == 1 or tick % frame_ticks == 0 or changed or finished:
                frames.append(
                    {
                        "time": now,
                        "vehicles": snapshots,
                        "lots": [
                            {
                                "id": item["id"],
                                "location": lots[item["id"]].location,
                                **progress[item["id"]],
                                "route": item.get("route"),
                                "completed_operations": lots[item["id"]].completed_operations,
                                "operation_count": len(item["operations"]),
                                "source_step": item["operations"][
                                    progress[item["id"]]["operation_index"]
                                ].get("source_step"),
                            }
                            for item in work
                        ],
                        "ports": {
                            name: {
                                "lot_id": p.lot.id if p.lot else None,
                                "reserved_by": p.reserved_by,
                            }
                            for name, p in ports.items()
                        },
                        "zones": {zone: vid for zone, (vid, _) in engine.owners.items()},
                        "zone_queues": {
                            zone: [vid for vid, _ in queue]
                            for zone, queue in engine.queues.items()
                            if queue
                        },
                        "completed_tasks": sum(t["state"] == "completed" for t in tasks.values()),
                        "completed_lots": sum(done.triggered for done in lot_done.values()),
                        "pending_tasks": sum(t["state"] == "ready" for t in tasks.values()),
                    }
                )
            if finished:
                break
        if not all(done.triggered for done in lot_done.values()):
            raise TimeoutError("多车场景超时；未通过瞬移、删车或强制释放区域解堵")
        summary.update(
            {
                "status": "completed",
                "completion_s": now,
                "vehicle_count": len(vehicles),
                "completed_tasks": len(tasks),
                "completed_lots": len(lots),
                "completed_operations": sum(lot.completed_operations for lot in lots.values()),
                "zone_grants": sum(e["kind"] == "zone.granted" for e in events),
                "zone_wait_vehicle_s": round(zone_wait_ticks * step, 3),
                "reentries": sum(e["kind"] == "lot.reentered" for e in events),
                "lot_results": [
                    {
                        "lot_id": item["id"],
                        "release_s": item["release_s"],
                        "completion_s": progress[item["id"]]["completion_s"],
                        "cycle_time_s": round(
                            progress[item["id"]]["completion_s"] - item["release_s"], 3
                        ),
                        "operations": lots[item["id"]].completed_operations,
                    }
                    for item in work
                ],
                "task_results": [
                    {
                        "task_id": task["id"],
                        "lot_id": task["lot_id"],
                        "ready_s": task["ready_s"],
                        "delivery_s": task["delivery_s"],
                        "ready_to_delivery_s": round(task["delivery_s"] - task["ready_s"], 3),
                    }
                    for task in tasks.values()
                ],
                "teleports": engine.teleports,
                "collisions": engine.collisions,
            }
        )
        if routed:
            summary["scope"] = "real_route_segment_reentrant"
            (output / "route_provenance.json").write_text(
                json.dumps(
                    {"routes": config["routes"], "selected_ports": config["ports"]},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        template = Path(__file__).with_name("fleet_replay.html").read_text("utf-8")
        data = json.dumps(
            {"config": config, "frames": frames, "events": events, "geometry": engine.geometry},
            ensure_ascii=False,
        ).replace("<", "\\u003c")
        (output / "replay.html").write_text(
            template.replace("__RUN_DATA__", data), encoding="utf-8"
        )
        return summary
    except BaseException as exc:
        summary["status"], summary["error"] = "failed", str(exc)
        raise
    finally:
        for name, value in (
            ("config", config),
            ("summary", summary),
            ("events", events),
            ("frames", frames),
        ):
            (output / f"{name}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if engine is not None:
            engine.close()
