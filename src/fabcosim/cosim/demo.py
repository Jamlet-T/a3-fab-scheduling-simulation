"""单时间主控：推进SUMO整步，再排空该时刻的生产与交接事件。"""

import copy
import json
import math
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path

import simpy

from fabcosim.ports.model import Lot, Port
from fabcosim.production.model import grid_delay, process_lot
from fabcosim.transport.sumo import SumoTransport


class Transfer:
    """一个在途任务；加工完成仅请求运输，不提前把lot放上车辆。"""

    def __init__(
        self,
        env: simpy.Environment,
        ports: dict[str, Port],
        engine: SumoTransport,
        config: dict,
        record: Callable[[str], None],
    ) -> None:
        self.env, self.ports, self.engine = env, ports, engine
        self.config, self.record = config, record
        self.at_source = env.event()
        self.at_destination = env.event()
        self.delivered = env.event()
        self.state = "empty_to_A"
        self.requested = False
        self.cargo: Lot | None = None

    def request(self) -> simpy.Event:
        if self.requested:
            raise RuntimeError("一辆OHT不能同时接收多个任务")
        self.requested = True
        self.record("transport.requested")
        self.env.process(self.service())
        return self.delivered

    def service(self) -> Generator:
        yield self.at_source
        self.state = "picking_up"
        self.record("transport.pickup_start")
        yield self.env.timeout(
            grid_delay(self.env.now, self.config["pickup_s"], self.config["step_s"])
        )
        self.cargo = self.ports["A"].take(self.engine.vehicle_id)
        self.record("transport.pickup_end")
        self.state = "pickup_done"
        yield self.at_destination
        self.state = "dropping_off"
        self.record("transport.dropoff_start")
        yield self.env.timeout(
            grid_delay(self.env.now, self.config["dropoff_s"], self.config["step_s"])
        )
        self.ports["B"].put(self.cargo, self.engine.vehicle_id)
        self.cargo = None
        self.state = "parked_at_B"
        self.record("transport.delivered")
        self.delivered.succeed()

    def observe(self) -> None:
        if not self.at_source.triggered and self.engine.stopped_at("A"):
            self.state = "waiting_at_A"
            self.record("transport.arrived_A")
            self.at_source.succeed()
        if self.state == "loaded_to_B" and self.engine.stopped_at("B"):
            self.record("transport.arrived_B")
            self.at_destination.succeed()

    def dispatch(self) -> None:
        if self.state == "pickup_done":
            self.engine.depart_to_b()
            self.state = "loaded_to_B"
            self.record("transport.departed_A")


def drain(env: simpy.Environment, boundary: float) -> None:
    # run(until=t)不处理t上的事件，这里显式处理同刻事件（包括新触发的交接）。
    while env.peek() <= boundary + 1e-8:
        env.step()


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    positive = [
        config["step_s"],
        config["max_time_s"],
        *config.get("track", {}).values(),
        *(
            config["oht"][key]
            for key in ("length_m", "min_gap_m", "max_speed_m_s", "accel_m_s2", "decel_m_s2")
        ),
    ]
    durations = [*config["processing_s"].values(), config["pickup_s"], config["dropoff_s"]]
    if any(not math.isfinite(x) or x <= 0 for x in positive):
        raise ValueError("时间步、时限、轨道和车辆参数必须是有限正数")
    if any(not math.isfinite(x) or x < 0 for x in durations):
        raise ValueError("加工与取送耗时必须是有限非负数")
    milliseconds = config["step_s"] * 1000
    if milliseconds < 1 or abs(milliseconds - round(milliseconds)) > 1e-7:
        raise ValueError("SUMO时间步必须为整数毫秒")
    if config.get("network", {}).get("type") == "smat2022":
        if (
            not math.isfinite(config["network"]["coordinate_scale"])
            or config["network"]["coordinate_scale"] <= 0
        ):
            raise ValueError("坐标缩放必须是有限正数")
        return config
    if config["ports"]["A"]["edge"] != "e0" or config["ports"]["B"]["edge"] != "e1":
        raise ValueError("当前小场景支持A在e0、B在e1")
    if not config["oht"]["initial_position_m"] < config["ports"]["A"]["position_m"]:
        raise ValueError("初始车辆必须位于A端口上游")
    return config


def run_demo(config: dict, output: Path) -> dict:
    config = copy.deepcopy(config)
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    env = simpy.Environment()
    lot = Lot()
    ports = {name: Port(f"{name}.port") for name in ("A", "B")}
    tools = {name: simpy.Resource(env, capacity=1) for name in ("A", "B")}
    events, trajectory = [], []

    def record(kind: str) -> None:
        events.append(
            {
                "time": round(env.now, 9),
                "kind": kind,
                "lot": lot.id,
                "location": lot.location,
                "operations": lot.completed_operations,
            }
        )

    engine = None
    real_network = config.get("network", {}).get("type") == "smat2022"
    summary = {
        "status": "failed",
        "scope": (
            "smat_network_two_tools_one_oht_one_lot"
            if real_network
            else "synthetic_two_tools_one_oht_one_lot"
        ),
    }
    try:
        engine = SumoTransport(config, output)
        transfer = Transfer(env, ports, engine, config, record)
        production = env.process(
            process_lot(
                env,
                lot,
                ports,
                tools,
                config["processing_s"],
                config["step_s"],
                transfer.request,
                record,
            )
        )
        drain(env, 0)
        # 每步均先让两个引擎到达同一时间；交接只在该边界上执行。
        for tick in range(1, math.floor(config["max_time_s"] / config["step_s"]) + 1):
            target = round(tick * config["step_s"], 9)
            engine.step(target)
            env.timeout(max(0, target - env.now))
            drain(env, target)
            transfer.observe()
            drain(env, target)
            transfer.dispatch()
            holders = int(lot.location.endswith(".tool") or lot.location == "completed")
            holders += sum(port.lot is lot for port in ports.values())
            holders += int(transfer.cargo is lot)
            if holders != 1:
                raise RuntimeError(f"物料守恒失效，lot实际持有者数量为{holders}")
            trajectory.append(
                {
                    "time": target,
                    **engine.snapshot(),
                    "state": transfer.state,
                    "location": lot.location,
                    "holders": holders,
                }
            )
            if production.triggered:
                break
        if not production.triggered:
            raise TimeoutError(f"在{config['max_time_s']}秒内未完成小场景")
        times = {event["kind"]: event["time"] for event in events}
        summary.update(
            {
                "status": "completed",
                "completion_s": times["lot.completed"],
                "transport_request_to_delivery_s": round(
                    times["transport.delivered"] - times["transport.requested"], 9
                ),
                "loaded_travel_s": round(
                    times["transport.arrived_B"] - times["transport.departed_A"], 9
                ),
                "completed_operations": lot.completed_operations,
                "final_location": lot.location,
                "vehicle_count_at_end": len(engine.api.vehicle.getIDList()),
                "teleports": engine.teleports,
                "collisions": engine.collisions,
                "max_observed_speed_m_s": max(row["speed"] for row in trajectory),
                "step_s": config["step_s"],
                "sumo_version": engine.api.getVersion(),
                "route": engine.route_info,
                "vehicle_id": engine.vehicle_id,
            }
        )
        if real_network:
            summary["network"] = config["network"]["summary"]
        write_replay(output, config, events, trajectory, engine.geometry)
        return summary
    except BaseException as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        raise
    finally:
        for name, value in (
            ("config", config),
            ("events", events),
            ("trajectory", trajectory),
            ("summary", summary),
        ):
            (output / f"{name}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if engine is not None:
            engine.close()


def write_replay(
    output: Path, config: dict, events: list, trajectory: list, geometry: dict
) -> None:
    template = Path(__file__).with_name("replay.html").read_text(encoding="utf-8")
    data = json.dumps(
        {"config": config, "events": events, "trajectory": trajectory, "geometry": geometry},
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    (output / "replay.html").write_text(template.replace("__RUN_DATA__", data), encoding="utf-8")


def default_output(root: Path, name: str = "two-tools") -> Path:
    return root / "runs" / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{name}"
