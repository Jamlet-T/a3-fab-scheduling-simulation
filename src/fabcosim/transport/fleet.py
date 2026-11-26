"""多车运动与保守区域互斥。原生停车点约束通行，不改写车辆位置。"""

import json
import math
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import sumolib
from traci import constants as tc

from fabcosim.data.smat_network import build_smat_network
from fabcosim.transport.sumo import route_between, route_shapes


def build_merge_network(config: dict, output: Path) -> Path:
    coords = {
        "n0": (0, 0),
        "merge": (20, 0),
        "n1": (40, 0),
        "n2": (40, 20),
        "n3": (0, 20),
        "branch": (0, -8),
    }
    pairs = [
        ("n0", "merge"),
        ("merge", "n1"),
        ("n1", "n2"),
        ("n2", "n3"),
        ("n3", "n0"),
        ("n0", "branch"),
        ("branch", "merge"),
    ]
    net = ET.Element("net", version="1.20")
    ET.SubElement(
        net,
        "location",
        netOffset="0,0",
        convBoundary="0,-8,40,20",
        origBoundary="0,-8,40,20",
        projParameter="!",
    )
    for i, (a, b) in enumerate(pairs):
        edge = ET.SubElement(net, "edge", {"id": f"e{i}", "from": a, "to": b, "priority": "1"})
        ET.SubElement(
            edge,
            "lane",
            id=f"e{i}_0",
            index="0",
            width="0.6",
            speed=str(config["oht"]["max_speed_m_s"]),
            length=str(math.dist(coords[a], coords[b])),
            shape=" ".join(f"{x},{y}" for x, y in (coords[a], coords[b])),
        )
    for name, (x, y) in coords.items():
        ET.SubElement(
            net,
            "junction",
            id=name,
            type="unregulated",
            x=str(x),
            y=str(y),
            incLanes=" ".join(f"e{i}_0" for i, (_, b) in enumerate(pairs) if b == name),
            intLanes="",
            shape=f"{x},{y}",
        )
    for i, (_, b) in enumerate(pairs):
        for j, (a, _) in enumerate(pairs):
            if a == b:
                ET.SubElement(
                    net,
                    "connection",
                    {
                        "from": f"e{i}",
                        "to": f"e{j}",
                        "fromLane": "0",
                        "toLane": "0",
                        "dir": "s",
                        "state": "M",
                    },
                )
    path = output / "network.net.xml"
    ET.ElementTree(net).write(path, encoding="utf-8", xml_declaration=True)
    return path


@dataclass
class Gate:
    zone: str
    enter: float
    exit: float
    hold_edge: str
    hold_pos: float
    hold_distance: float
    state: str = "new"
    installed: bool = False


class FleetTransport:
    def __init__(self, config: dict, output: Path, record: Callable[..., None]) -> None:
        from fabcosim._native import check_sumo_sqlite

        check_sumo_sqlite()
        import libsumo

        self.api, self.config, self.record = libsumo, config, record
        self._route_geometry = config.get("performance", {}).get("route_event_geometry", True)
        if not self._route_geometry and config.get("replay", {}).get("enabled", True):
            raise ValueError("关闭路线几何记录时须显式关闭replay，避免生成不完整回放")
        real = config.get("network", {}).get("type") == "smat2022"
        path = build_smat_network(config, output) if real else build_merge_network(config, output)
        self.net = sumolib.net.readNet(str(path))
        self.net._fabcosim_route_cache_limit = config.get("performance", {}).get(
            "route_cache_size", 4096
        )
        self.nodes = {}
        if real:
            mapping = json.loads((output / "network_mapping.json").read_text("utf-8"))
            self.nodes = {n["NAME"]: n for n in mapping["nodes"]}
        self.ids = sorted(v["id"] for v in config["vehicles"])
        self.plans: dict[str, dict] = {}
        self.native_route_ids: dict[tuple[str, ...], str] = {}
        self.native_stop_index_bound: dict[str, int] = {}
        self._reads: dict[str, dict] = {}
        self._arrivals: dict[str, dict[tuple[str, float], bool]] = {}
        self._arrival_cache = config.get("performance", {}).get("arrival_cache", True)
        self._gate_schedule = config.get("performance", {}).get("gate_schedule", True)
        self._queued_gates: dict[str, list[Gate]] = {}
        self._subscriptions = config.get("performance", {}).get("vehicle_subscriptions", True)
        self.parking: dict[str, dict] = {}
        self.time_offset = 0.0
        self.master_time = 0.0
        self.owners: dict[str, tuple[str, Gate]] = {}
        self.queues: dict[str, list[tuple[str, Gate]]] = {}
        self.queued_zones: set[str] = set()
        self.queue_order: dict[str, int] = {}
        self.waiting: dict[str, str | None] = {vid: None for vid in self.ids}
        self.teleports, self.collisions = [], []
        self.geometry = {
            "lanes": [edge.getLane(0).getShape() for edge in self.net.getEdges()],
            "ports": {
                name: sumolib.geomhelper.positionAtShapeOffset(
                    self.net.getEdge(p["edge"]).getLane(0).getShape(), p["position_m"]
                )
                for name, p in config["ports"].items()
            },
            "equipment": config.get("network", {}).get("equipment", []),
        }
        zone_points = {}
        for node in self.nodes.values():
            if node["ZCU_TYPE"] == "STOP":
                zone_points.setdefault(node["ZCU_NAME"], []).append(
                    self.net.getNode(node["NAME"]).getCoord()
                )
        self.geometry["zones"] = {
            zone: [sum(p[i] for p in points) / len(points) for i in (0, 1)]
            for zone, points in zone_points.items()
        }
        self.geometry["zones"].update(
            {
                f"junction:{n.getID()}": n.getCoord()
                for n in self.net.getNodes()
                if len(n.getIncoming()) > 1
            }
        )
        self.api.start(
            [
                "sumo",
                "--net-file",
                str(path),
                "--step-length",
                str(config["step_s"]),
                "--time-to-teleport",
                "-1",
                "--collision.action",
                "warn",
                "--no-step-log",
                "true",
                "--duration-log.disable",
                "true",
            ]
        )
        try:
            vt = self.api.vehicletype
            vt.copy("DEFAULT_VEHTYPE", "oht")
            values = config["oht"]
            for method, value in [
                ("setLength", values["length_m"]),
                ("setMinGap", values["min_gap_m"]),
                ("setMaxSpeed", values["max_speed_m_s"]),
                ("setAccel", values["accel_m_s2"]),
                ("setDecel", values["decel_m_s2"]),
                ("setImperfection", 0),
                ("setTau", max(0.2, config["step_s"])),
                ("setSpeedFactor", 1),
                ("setSpeedDeviation", 0),
            ]:
                getattr(vt, method)("oht", value)
            for vehicle in config["vehicles"]:
                start = {"edge": vehicle["edge"], "position_m": vehicle["position_m"]}
                cycle, _ = self.cycle(start)
                route = cycle + cycle[1:] + cycle[1:]
                self.api.route.add(vehicle["id"], route)
                self.api.vehicle.add(
                    vehicle["id"],
                    vehicle["id"],
                    typeID="oht",
                    departPos=str(vehicle["position_m"]),
                    departSpeed="0",
                )
                if self._subscriptions:
                    self.api.vehicle.subscribe(
                        vehicle["id"],
                        (tc.VAR_DISTANCE, tc.VAR_SPEED, tc.VAR_POSITION,
                         tc.VAR_ROAD_ID, tc.VAR_LANEPOSITION),
                    )
        except BaseException:
            self.close()
            raise

    def _invalidate(self, vid: str) -> None:
        """本步内原生停车/路线改变后，位置订阅和到达判断都不能沿用。"""
        getattr(self, "_reads", {}).pop(vid, None)
        getattr(self, "_arrivals", {}).pop(vid, None)

    def position(self, vid: str) -> dict:
        row = getattr(self, "_reads", {}).get(vid)
        if row is not None:
            return {"edge": row[tc.VAR_ROAD_ID], "position_m": row[tc.VAR_LANEPOSITION]}
        return {
            "edge": self.api.vehicle.getRoadID(vid),
            "position_m": self.api.vehicle.getLanePosition(vid),
        }

    def speed(self, vid: str) -> float:
        row = getattr(self, "_reads", {}).get(vid)
        return row[tc.VAR_SPEED] if row is not None else self.api.vehicle.getSpeed(vid)

    def _distance(self, vid: str) -> float:
        row = getattr(self, "_reads", {}).get(vid)
        return row[tc.VAR_DISTANCE] if row is not None else self.api.vehicle.getDistance(vid)

    def park(self, vid: str) -> dict | None:
        """选当前巡行路径上区域之外的停车点，通过正常路径/停车动作抵达。"""
        start = self.position(vid)
        origin = self.api.vehicle.getDistance(vid)
        cycle, distance = self.cycle(start)
        route = cycle + cycle[1:] + cycle[1:] + cycle[1:]
        limit = origin + distance * 2
        gates = self.make_gates(route, start, origin, limit, vid)
        forbidden = [(g.enter, g.exit) for g in gates]
        forbidden.extend(
            (max(origin, g.enter), g.exit) for owner, g in self.owners.values() if owner == vid
        )
        offset = origin - start["position_m"]
        spacing = self.config["oht"]["length_m"] + self.config["oht"]["min_gap_m"] + 0.3
        # 单向轨道不能越过已预留的停车点；后车目标必须留在停车前车之后。
        barrier = math.inf
        blocker = None
        ahead = {}
        scan = offset
        for edge_id in route:
            for other in self.parking:
                position = self.position(other)
                point = scan + position["position_m"]
                if position["edge"] == edge_id and point >= origin and other not in ahead:
                    ahead[other] = point
            scan += self.net.getEdge(edge_id).getLength()
        scan = offset
        for edge_id in route:
            for other, reserved in self.parking.items():
                point = scan + reserved["position_m"]
                if (
                    other != vid
                    and reserved["edge"] == edge_id
                    and point >= origin
                    and ahead.get(other, math.inf)
                    <= point + self.config["oht"].get("port_stop_tolerance_m", 0.1)
                ):
                    if point - spacing < barrier:
                        barrier, blocker = point - spacing, other
            scan += self.net.getEdge(edge_id).getLength()
        speed = self.api.vehicle.getSpeed(vid)
        minimum = origin + max(
            0 if speed < 1e-8 else 0.01,
            speed * speed / (2 * self.config["oht"].get("decel_m_s2", 3))
            + speed * self.config.get("step_s", 0.1)
            + (0.2 if speed > 1e-8 else 0),
        )
        for edge_id in route:
            edge = self.net.getEdge(edge_id)
            positions = [
                edge.getLength() - 0.5 - index * spacing for index in range(len(self.ids) + 4)
            ]
            if edge_id == start["edge"] and offset <= origin and speed < 1e-8:
                positions.append(start["position_m"])
            if 0 < barrier - offset < edge.getLength():
                positions.insert(0, barrier - offset)
            for position in positions:
                point = offset + position
                stationary_here = speed < 1e-8 and abs(point - origin) < 1e-8
                clearance = spacing - 0.3 if stationary_here else spacing
                allowed_barrier = barrier + (0.3 if stationary_here else 0)
                if position <= 0 or not minimum <= point <= min(limit, allowed_barrier):
                    continue
                if any(
                    p["edge"] == edge_id and abs(p["position_m"] - position) < clearance - 1e-6
                    for v, p in self.parking.items()
                    if v != vid
                ):
                    continue
                if not any(a - 0.3 <= point <= b + 0.3 for a, b in forbidden):
                    goal = {"edge": edge_id, "position_m": position}
                    self.plan(vid, goal)
                    self.parking[vid] = goal
                    self.record("vehicle.parking", vehicle_id=vid, goal=goal)
                    return goal
            offset += edge.getLength()
        if blocker is not None:
            # 前车停车使后车无法在区域外落位时，前车正常前驶让出出口。
            # 不撤销已进入区域的路权、不让后车停在区域内。
            self.plan(blocker, None)
            self.record("vehicle.parking_yield", vehicle_id=blocker, blocked_vehicle=vid)
            return None
        raise RuntimeError(f"{vid}当前巡行路径找不到区域外停车点")

    def advance_stationary(self, now: float) -> None:
        """仅原生停靠且速度为零时批量推进；不跳过运动或区域控制。"""
        if any(
            not self.api.vehicle.isStopped(v) or self.api.vehicle.getSpeed(v) > 1e-8
            for v in self.ids
        ):
            raise RuntimeError("运动中的车队不可批量推进")
        before = {v: self.api.vehicle.getDistance(v) for v in self.ids}
        if self.config.get("static_time_skip", False):
            # 所有车辆原生停靠，且无外部SUMO流量/定时故障：积分状态恒定。
            # 主时钟正常推进；恢复运动时把全局时刻映射到暂停过的积分器时钟。
            # 以原生积分器的实际时刻为锚点，不能累计数十万次浮点差值。
            self.time_offset = round(now - self.api.simulation.getTime(), 9)
        else:
            self.api.simulationStep(round(now - self.time_offset, 9))
        self._arrivals.clear()
        self.master_time = now
        self._check_clock(now)
        if any(abs(self.api.vehicle.getDistance(v) - d) > 1e-8 for v, d in before.items()):
            raise RuntimeError("静止推进中车辆发生位移")

    def _check_clock(self, now: float) -> None:
        mapped = self.api.simulation.getTime() + self.time_offset
        if abs(mapped - now) > 1e-6:
            raise RuntimeError(f"原生积分器与主时钟映射不一致：{mapped} / {now}")

    def cycle(self, start: dict) -> tuple[list[str], float]:
        return route_between(self.net, start, {"edge": start["edge"], "position_m": 0})

    def distance(self, vid: str, goal: dict) -> float:
        return route_between(self.net, self.position(vid), goal)[1]

    def plan(self, vid: str, goal: dict | None) -> None:
        # 路线/停车接口可能改变原生读取结果；直到下一运动步都用实时查询。
        self._invalidate(vid)
        self._queued_gates.pop(vid, None)
        self.parking.pop(vid, None)
        api = self.api.vehicle
        if self.config["traffic"].get("use_route_ids", False):
            self.native_stop_index_bound[vid] = max(
                self.native_stop_index_bound.get(vid, 0), api.getRouteIndex(vid)
            )
        # 上一步已清出区域时，先释放再重规划，不能把旧出口延长到下一圈RESET。
        travelled = api.getDistance(vid)
        for zone, (owner, held) in list(self.owners.items()):
            if owner == vid and travelled >= held.exit - 1e-8:
                held.state = "done"
                del self.owners[zone]
                self.record(
                    "zone.released",
                    vehicle_id=vid,
                    zone=zone,
                    exit_distance_m=held.exit,
                    distance_m=travelled,
                )
        for zone in sorted(self.queued_zones, key=self.queue_order.__getitem__):
            queue = self.queues[zone]
            if any(v == vid for v, _ in queue):
                self.record("zone.cancelled", vehicle_id=vid, zone=zone)
            queue[:] = [(v, g) for v, g in queue if v != vid]
            if not queue:
                self.queued_zones.discard(zone)
        while api.getStops(vid):
            api.replaceStop(vid, 0, "")
        start = self.position(vid)
        prefix, distance = (
            self.cycle(start) if goal is None else route_between(self.net, start, goal)
        )
        origin = api.getDistance(vid)
        actual_goal = goal or {"edge": start["edge"], "position_m": 0}
        tail_start = {"edge": prefix[-1], "position_m": 0.001}
        tail, _ = self.cycle(tail_start)
        route = prefix + tail[1:] + tail[1:]
        for zone, (owner, held) in self.owners.items():
            if owner != vid or zone.startswith("junction:") or not held.enter <= origin < held.exit:
                continue
            if (
                origin
                >= held.exit - self.config["oht"]["length_m"] - self.config["oht"]["min_gap_m"]
            ):
                continue
            if any(
                self.nodes.get(self.net.getEdge(e).getToNode().getID(), {}).get("ZCU_NAME") == zone
                and self.nodes[self.net.getEdge(e).getToNode().getID()]["ZCU_TYPE"] == "RESET"
                for e in route
            ):
                continue
            # 已进入区域不可直接绕开RESET：先经过合法出口，再去新目的地。
            choices = []
            for node in self.nodes.values():
                if node.get("ZCU_NAME") != zone or node.get("ZCU_TYPE") != "RESET":
                    continue
                for edge in self.net.getNode(node["NAME"]).getOutgoing():
                    anchor = {"edge": edge.getID(), "position_m": min(0.1, edge.getLength() / 2)}
                    a, da = route_between(self.net, start, anchor)
                    b, db = route_between(self.net, anchor, actual_goal)
                    choices.append((da + db, a + b[1:]))
            if not choices:
                raise RuntimeError(f"持有区域{zone}无可达RESET出口")
            distance, prefix = min(choices, key=lambda choice: choice[0])
            tail_start = {"edge": prefix[-1], "position_m": 0.001}
            tail, _ = self.cycle(tail_start)
            route = prefix + tail[1:] + tail[1:]
            self.record("zone.exit_waypoint", vehicle_id=vid, zone=zone)
        if self.config["traffic"].get("use_route_ids", False):
            # SUMO 1.27.1保留旧停靠的routeIndex，却不随setRouteID重映射；
            # 后续setStop的setSkips会读取新route[旧index]。仅延长原有
            # 备用回路，容纳曾实际到达的停靠索引；任务路径/终点不变，
            # 控制器仍在既定终点重规划，不驶入这些额外备用圈。
            native_route = list(route)
            while len(native_route) <= self.native_stop_index_bound[vid]:
                native_route.extend(tail[1:])
            key = tuple(native_route)
            if key not in self.native_route_ids:
                route_id = f"fab_route_{len(self.native_route_ids)}"
                self.api.route.add(route_id, native_route)
                self.native_route_ids[key] = route_id
            # 相同未来路径共享原生route，避免setRoute追加并复制整段历史。
            api.setRouteID(vid, self.native_route_ids[key])
        else:
            api.setRoute(vid, route)
        self.plans[vid] = {
            "goal": goal,
            "route": route,
            "end": origin + distance,
            "gates": self.make_gates(route, start, origin, origin + distance, vid),
        }
        # 重规划不能沿用旧路径的RESET距离：分岔后新出口可能更远。
        for zone, (owner, held) in list(self.owners.items()):
            if owner != vid:
                continue
            gates = self.plans[vid]["gates"]
            if (
                origin
                >= held.exit - self.config["oht"]["length_m"] - self.config["oht"]["min_gap_m"]
            ):
                # 前端已越过RESET，只等待既定车尾距离，不把出口移到下一圈。
                gates.append(held)
                continue
            if origin < held.enter:
                replacement = next((g for g in gates if g.zone == zone), None)
                if replacement is not None:
                    replacement.state = "granted"
                    self.owners[zone] = (vid, replacement)
                    self.record(
                        "zone.exit_replanned",
                        vehicle_id=vid,
                        zone=zone,
                        old_exit_m=held.exit,
                        new_exit_m=replacement.exit,
                    )
                    continue
                # 已预留但尚未进入的区域被新路线绕开，可撤销预留。
                held.state = "done"
                del self.owners[zone]
                self.record("zone.reservation_cancelled", vehicle_id=vid, zone=zone)
                continue
            if not zone.startswith("junction:"):
                old_exit = held.exit
                offset = origin - start["position_m"]
                for edge_id in route:
                    edge = self.net.getEdge(edge_id)
                    offset += edge.getLength()
                    node = self.nodes.get(edge.getToNode().getID(), {})
                    if node.get("ZCU_NAME") == zone and node.get("ZCU_TYPE") == "RESET":
                        held.exit = (
                            offset
                            + self.config["oht"]["length_m"]
                            + self.config["oht"]["min_gap_m"]
                        )
                        self.record(
                            "zone.exit_replanned",
                            vehicle_id=vid,
                            zone=zone,
                            old_exit_m=old_exit,
                            new_exit_m=held.exit,
                        )
                        break
                else:
                    raise RuntimeError(f"持有区域{zone}的重规划路径没有RESET")
            gates.append(held)
        self.plans[vid]["gates"].sort(key=lambda gate: gate.enter)
        if goal is not None:
            api.setStop(
                vid, goal["edge"], pos=goal["position_m"], duration=self.config["max_time_s"] + 1
            )
        self.waiting[vid] = None
        self.record(
            "vehicle.route",
            vehicle_id=vid,
            edges=prefix,
            distance_m=distance,
            goal=goal,
            **({"shapes": route_shapes(
                self.net, prefix, start, goal or {"edge": start["edge"], "position_m": 0}
            )} if self._route_geometry else {}),
        )

    def make_gates(
        self, route: list[str], start: dict, origin: float, limit: float, vid: str | None = None
    ) -> list[Gate]:
        boundaries, offset = [], origin - start["position_m"]
        spans = []
        for edge_id in route:
            edge = self.net.getEdge(edge_id)
            spans.append((edge_id, offset, offset + edge.getLength()))
            offset += edge.getLength()
            boundaries.append((edge.getToNode().getID(), offset))
        intervals = []
        for i, (node_id, enter) in enumerate(boundaries):
            if enter < origin or enter > limit + 1e-8:
                continue
            node = self.nodes.get(node_id, {})
            if node.get("ZCU_TYPE") == "STOP":
                zone = node["ZCU_NAME"]
                reset = next(
                    (
                        pos
                        for nid, pos in boundaries[i + 1 :]
                        if self.nodes.get(nid, {}).get("ZCU_NAME") == zone
                        and self.nodes[nid]["ZCU_TYPE"] == "RESET"
                    ),
                    None,
                )
                if reset is None:
                    raise RuntimeError(f"路径中的{zone}没有对应RESET出口")
                intervals.append((zone, enter, reset))
        coverage = list(intervals)
        for zone, (owner, held) in self.owners.items():
            if owner != vid or zone.startswith("junction:") or not held.enter <= origin < held.exit:
                continue
            if (
                origin
                >= held.exit - self.config["oht"]["length_m"] - self.config["oht"]["min_gap_m"]
            ):
                continue
            reset = next(
                (
                    pos
                    for nid, pos in boundaries
                    if self.nodes.get(nid, {}).get("ZCU_NAME") == zone
                    and self.nodes[nid]["ZCU_TYPE"] == "RESET"
                    and pos >= origin
                ),
                None,
            )
            if reset is not None:
                coverage.append((zone, origin, reset))
        for node_id, enter in boundaries:
            if origin <= enter <= limit and len(self.net.getNode(node_id).getIncoming()) > 1:
                if not any(a <= enter <= b for _, a, b in coverage):
                    intervals.append(
                        (
                            f"junction:{node_id}",
                            enter,
                            enter + self.config["traffic"]["junction_clearance_m"],
                        )
                    )
        gates = []
        length = self.config["oht"]["length_m"] + self.config["oht"]["min_gap_m"]
        for zone, enter, exit_pos in sorted(intervals, key=lambda item: item[1]):
            hold = enter - self.config["traffic"]["stop_margin_m"]
            edge_id, begin, _ = next((e, a, b) for e, a, b in spans if a < hold <= b)
            gates.append(Gate(zone, enter, exit_pos + length, edge_id, hold - begin, hold))
        return gates

    def control(self, now: float) -> None:
        vehicle = self.api.vehicle
        # 本次控制只增删停车/路权，不推进运动；同一位置的里程只读一次。
        distances = {vid: self._distance(vid) for vid in self.ids}
        for zone, (vid, gate) in list(self.owners.items()):
            if distances[vid] >= gate.exit - 1e-8:
                gate.state = "done"
                del self.owners[zone]
                self.record(
                    "zone.released",
                    vehicle_id=vid,
                    zone=zone,
                    exit_distance_m=gate.exit,
                    distance_m=distances[vid],
                )
        for vid in self.ids:
            plan = self.plans[vid]
            distance = distances[vid]
            self.waiting[vid] = None
            # 距离只会前进。尚未到下一申请/清理边界时，区域列表不变；
            # 仍逐步执行所有owner释放、队列授权和未经授权进入检查。
            if getattr(self, "_gate_schedule", False) and distance < plan.get(
                "next_gate_check_m", -math.inf
            ):
                continue
            plan["gates"][:] = [
                g for g in plan["gates"] if g.state in ("new", "queued") or g.exit > distance - 1e-8
            ]
            for gate in plan["gates"]:
                if gate.state == "queued":
                    break
                if (
                    gate.state == "new"
                    and gate.hold_distance - distance <= self.config["traffic"]["lookahead_m"]
                ):
                    if gate.zone in self.owners and self.owners[gate.zone][0] == vid:
                        held = self.owners[gate.zone][1]
                        if held.exit >= gate.hold_distance:
                            held.exit = max(held.exit, gate.exit)
                            gate.state = "granted"
                        continue
                    if gate.hold_distance < distance - 0.01:
                        if distance < gate.enter - 1e-6 and vehicle.getSpeed(vid) < 1e-8:
                            # 从端口静止起步时可在当前安全位置等待，不能要求倒退到默认停车线。
                            gate.hold_edge = vehicle.getRoadID(vid)
                            gate.hold_pos = vehicle.getLanePosition(vid)
                            gate.hold_distance = distance
                            self.record("zone.stop_reanchored", vehicle_id=vid, zone=gate.zone)
                        else:
                            raise RuntimeError(f"{vid}已越过未授权的停车边界{gate.zone}")
                    # 按沿途顺序插入；端口停车始终排在区域停车之后。
                    active = [g for g in plan["gates"] if g.installed]
                    index = sum(g.hold_distance < gate.hold_distance for g in active)
                    if self.config["traffic"].get("fit_native_stops", False):
                        # setStop按既定路径排序；避免insertStop另行搜索/重建路线。
                        # 不能合并同位置的其他停靠，也不能改变停车次序或行驶路径。
                        stops = vehicle.getStops(vid)
                        route = vehicle.getRoute(vid)
                        vehicle.setStop(
                            vid,
                            gate.hold_edge,
                            pos=gate.hold_pos,
                            duration=self.config["max_time_s"] + 1,
                        )
                        self._invalidate(vid)
                        fitted = vehicle.getStops(vid)
                        if (
                            len(fitted) != len(stops) + 1
                            or fitted[index].lane != gate.hold_edge + "_0"
                            or abs(fitted[index].endPos - gate.hold_pos) > 1e-6
                            or vehicle.getRoute(vid) != route
                        ):
                            raise RuntimeError(f"{vid}区域停车未按既定路径与顺序插入")
                    else:
                        vehicle.insertStop(
                            vid,
                            index,
                            gate.hold_edge,
                            pos=gate.hold_pos,
                            duration=self.config["max_time_s"] + 1,
                        )
                        self._invalidate(vid)
                    gate.installed, gate.state = True, "queued"
                    if gate.zone not in self.queues:
                        self.queue_order[gate.zone] = len(self.queues)
                        self.queues[gate.zone] = []
                    self.queues[gate.zone].append((vid, gate))
                    self.queued_zones.add(gate.zone)
                    self.record("zone.requested", vehicle_id=vid, zone=gate.zone)
                    break
            if getattr(self, "_gate_schedule", False):
                plan["next_gate_check_m"] = min(
                    (
                        g.hold_distance - self.config["traffic"]["lookahead_m"]
                        if g.state == "new" else g.exit + 1e-8
                        for g in plan["gates"] if g.state != "queued"
                    ),
                    default=math.inf,
                )
                self._queued_gates[vid] = [g for g in plan["gates"] if g.state == "queued"]
        # 重规划后，前车可能才申请后车已预留的区域。后车尚未进入且已停稳时，
        # 撤回预留让实际前车先行；已进入区域绝不撤回。
        for zone, (owner, held) in list(self.owners.items()):
            if distances[owner] >= held.enter or vehicle.getSpeed(owner) > 1e-8:
                continue
            leader = vehicle.getLeader(owner, self.config["traffic"]["lookahead_m"])
            if (
                leader
                and leader[0] != owner
                and leader[1] <= self.config["traffic"]["lookahead_m"] + 1e-8
                and any(vid == leader[0] for vid, _ in self.queues.get(zone, []))
            ):
                del self.owners[zone]
                held.state = "new"
                self.plans[owner].pop("next_gate_check_m", None)
                self.record("zone.priority_revoked", vehicle_id=owner, zone=zone, leader=leader[0])
        # 跳过历史空队列，但沿用原字典的首次出现次序；重新入队不改变
        # 多区域授权优先级，不能直接按set或新的入队次序遍历。
        for zone in sorted(self.queued_zones, key=self.queue_order.__getitem__):
            queue = self.queues[zone]
            if zone in self.owners or not queue:
                continue
            for vid, gate in list(queue):
                clearance = max(0, gate.exit - distances[vid])
                leader = vehicle.getLeader(vid, clearance)
                # getLeader会返回整条前向车道上的首车，距离可能超出查询范围。
                # gap为扣除minGap后本车可前进的距离；区域出口之外不应阻挡清区。
                if (
                    leader
                    and leader[0] != vid
                    and leader[1] < clearance - 1e-8
                    and (
                        any(v == leader[0] for v, _ in queue) or vehicle.getSpeed(leader[0]) < 0.01
                    )
                ):
                    continue
                self.owners[zone] = (vid, gate)
                queue.remove((vid, gate))
                if not queue:
                    self.queued_zones.discard(zone)
                active = sorted(
                    (g for g in self.plans[vid]["gates"] if g.installed),
                    key=lambda g: g.hold_distance,
                )
                if self.config["traffic"].get("use_route_ids", False):
                    self.native_stop_index_bound[vid] = max(
                        self.native_stop_index_bound.get(vid, 0), vehicle.getRouteIndex(vid)
                    )
                vehicle.replaceStop(vid, active.index(gate), "")
                self._invalidate(vid)
                gate.installed, gate.state = False, "granted"
                self.plans[vid].pop("next_gate_check_m", None)
                self.record("zone.granted", vehicle_id=vid, zone=zone)
                break
        for vid in self.ids:
            distance = distances[vid]
            plan = self.plans[vid]
            for gate in getattr(self, "_queued_gates", {}).get(vid, plan["gates"]):
                if gate.state == "queued":
                    if self.waiting[vid] is None:
                        self.waiting[vid] = f"zcu:{gate.zone}"
                    if distance > gate.enter + 1e-6:
                        raise RuntimeError(f"{vid}未经授权进入{gate.zone}")

    def step(self, now: float) -> None:
        if self.api.vehicle.getIDList():
            for vid in self.ids:
                if vid not in self.plans:
                    self.plan(vid, None)
                plan = self.plans[vid]
                if plan["goal"] is None and self._distance(vid) >= plan["end"] - 0.5:
                    self.plan(vid, None)
            self.control(now)
        self.api.simulationStep(round(now - self.time_offset, 9))
        self._arrivals.clear()
        if self._subscriptions:
            self._reads = self.api.vehicle.getAllSubscriptionResults()
        self.master_time = now
        self._check_clock(now)
        self.teleports.extend(self.api.simulation.getStartingTeleportIDList())
        self.collisions.extend(self.api.simulation.getCollidingVehiclesIDList())
        if self.teleports or self.collisions:
            raise RuntimeError("多车运行发生碰撞或瞬移")
        if set(self.api.vehicle.getIDList()) != set(self.ids):
            raise RuntimeError("车辆未成功插入或意外消失")
        for vid, plan in self.plans.items():
            distance = self._distance(vid)
            for gate in plan["gates"]:
                if gate.enter > distance:
                    break
                if gate.enter <= distance < gate.exit:
                    if self.owners.get(gate.zone, (None,))[0] != vid:
                        raise RuntimeError(f"实际运动未经授权进入区域：{vid} / {gate.zone}")

    def arrived(self, vid: str, goal: dict) -> bool:
        if not getattr(self, "_arrival_cache", False):
            return self._arrived_native(vid, goal)
        cache = self._arrivals.setdefault(vid, {})
        key = goal["edge"], goal["position_m"]
        if key not in cache:
            cache[key] = self._arrived_native(vid, goal)
        return cache[key]

    def _arrived_native(self, vid: str, goal: dict) -> bool:
        p = self.position(vid)
        if not self.api.vehicle.isStopped(vid) or p["edge"] != goal["edge"]:
            return False
        stops = self.api.vehicle.getStops(vid)
        tolerance = self.config["oht"].get("port_stop_tolerance_m", 0.1)
        return bool(
            stops
            and stops[0].lane == goal["edge"] + "_0"
            and abs(stops[0].endPos - goal["position_m"]) < 1e-6
            and stops[0].arrival >= 0
            and abs(p["position_m"] - goal["position_m"]) <= tolerance + 1e-8
        )

    def snapshot(self, vid: str) -> dict:
        row = getattr(self, "_reads", {}).get(vid)
        x, y = row[tc.VAR_POSITION] if row is not None else self.api.vehicle.getPosition(vid)
        return {
            "id": vid,
            "x": x,
            "y": y,
            "speed": self.speed(vid),
            **self.position(vid),
            "distance_m": self._distance(vid),
            "zone_wait": self.waiting[vid],
        }

    def present(self) -> bool:
        """全部车辆是否都还在运输引擎里；不重建、不移动、不删车的自检。"""
        return set(self.api.vehicle.getIDList()) == set(self.ids)

    def leader(self, vid: str, distance_m: float) -> tuple[str, float] | None:
        """同向最近的领车及其净间距，查询范围内没有则None。"""
        return self.api.vehicle.getLeader(vid, distance_m)

    def stops(self, vid: str) -> str:
        """当前原生停车目标的可读描述，仅用于停滞现场诊断。"""
        return str(self.api.vehicle.getStops(vid))

    def integrator_time(self) -> float:
        """运输积分器自身推进到的时间；静止时段暂停时小于主时钟。"""
        return self.api.simulation.getTime()

    def zone_owners(self) -> dict[str, str]:
        """{区域: 持有车辆}，用于回放帧。"""
        return {zone: vid for zone, (vid, _) in self.owners.items()}

    def zone_queues(self) -> dict[str, list[str]]:
        """{区域: [排队等待的车辆]}，仅含非空队列，用于回放帧。"""
        return {
            zone: [vid for vid, _ in queue] for zone, queue in self.queues.items() if queue
        }

    def zone_state(self) -> dict[str, dict]:
        """{区域: {vehicle_id, ...授权信息}} 与 {区域: [排队车辆]}，用于检查点。"""
        return {
            "owners": {
                zone: {"vehicle_id": vid, **asdict(gate)}
                for zone, (vid, gate) in self.owners.items()
            },
            "queues": {
                zone: [{"vehicle_id": vid, **asdict(gate)} for vid, gate in queue]
                for zone, queue in self.queues.items()
                if queue
            },
        }

    def stall_state(self) -> dict:
        """停滞现场的完整运输侧状态；不通过删车或强制交付继续运行。"""
        return {
            "native_stops": {vid: self.stops(vid) for vid in self.ids},
            "leaders": {vid: self.leader(vid, 1000) for vid in self.ids},
            "owners": {
                zone: {"vehicle_id": vid, **asdict(gate)}
                for zone, (vid, gate) in self.owners.items()
            },
            "plans": {
                vid: {**plan, "gates": [asdict(gate) for gate in plan["gates"]]}
                for vid, plan in self.plans.items()
            },
        }

    def close(self) -> None:
        self.api.close()
