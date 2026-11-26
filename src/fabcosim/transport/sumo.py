"""SUMO运动适配。停靠与续行通过原生stop控制，不移动或删除车辆。"""

import json
import subprocess
from collections import OrderedDict
from pathlib import Path

from sumolib import checkBinary
from sumolib.net import Net

from fabcosim.data.smat_network import build_smat_network


def route_between(net: Net, start: dict, goal: dict) -> tuple[list[str], float]:
    """按精确边内位置复用静态图查询；不量化位置或改变最短路平局规则。"""
    limit = getattr(net, "_fabcosim_route_cache_limit", 4096)
    if limit <= 0:
        return _route_between_uncached(net, start, goal)

    def exact_position(value):
        return type(value), value.hex() if isinstance(value, float) else value

    key = (
        start["edge"],
        exact_position(start["position_m"]),
        goal["edge"],
        exact_position(goal["position_m"]),
    )
    cache = getattr(net, "_fabcosim_routes", None)
    if cache is None:
        cache = net._fabcosim_routes = OrderedDict()
    if key in cache:
        edges, distance = cache[key]
        cache.move_to_end(key)
        return list(edges), distance
    edges, distance = _route_between_uncached(net, start, goal)
    cache[key] = tuple(edges), distance
    if len(cache) > limit:
        cache.popitem(last=False)
    return edges, distance


def _route_between_uncached(net: Net, start: dict, goal: dict) -> tuple[list[str], float]:
    """按单向轨道寻路，边内起终点也参与距离计算。"""
    first, last = net.getEdge(start["edge"]), net.getEdge(goal["edge"])
    if first == last and start["position_m"] > goal["position_m"]:
        # 到同一轨道的上游端口必须走有向回路，不能倒车或直接抵达。
        candidates = []
        for successor in first.getOutgoing():
            path, distance = net.getShortestPath(
                successor, last, fromPos=0, toPos=goal["position_m"]
            )
            if path:
                candidates.append(
                    (
                        [first.getID()] + [edge.getID() for edge in path],
                        first.getLength() - start["position_m"] + distance,
                    )
                )
        if not candidates:
            raise RuntimeError("无法沿有向回路到达同轨道上游端口")
        return min(candidates, key=lambda candidate: candidate[1])
    path, distance = net.getShortestPath(
        first, last, fromPos=start["position_m"], toPos=goal["position_m"]
    )
    if not path:
        raise RuntimeError(f"不存在有向路径：{start['edge']} → {goal['edge']}")
    return [edge.getID() for edge in path], distance


def route_shapes(net: Net, edges: list[str], start: dict, goal: dict) -> list[list]:
    """回放路线也按真实边内起终点裁剪，避免高亮越过端口的轨道段。"""
    from sumolib.geomhelper import positionAtShapeOffset

    result = []
    for index, edge_id in enumerate(edges):
        shape = net.getEdge(edge_id).getLane(0).getShape()
        begin = start["position_m"] if index == 0 else 0
        end = goal["position_m"] if index == len(edges) - 1 else net.getEdge(edge_id).getLength()
        points = [positionAtShapeOffset(shape, begin)]
        offset = 0
        for a, b in zip(shape, shape[1:]):
            offset += ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            if begin < offset < end:
                points.append(b)
        points.append(positionAtShapeOffset(shape, end))
        result.append(points)
    return result


class SumoTransport:
    vehicle_id = "oht0"

    def __init__(self, config: dict, output: Path) -> None:
        import libsumo

        self.api = libsumo
        self.config = config
        self.teleports: list[str] = []
        self.collisions: list[str] = []
        if config.get("network", {}).get("type") == "smat2022":
            network = build_smat_network(config, output)
        else:
            network = self.build_synthetic(config, output)
        self.vehicle_id = config["oht"].get("source_id", "oht0")
        import sumolib

        net = sumolib.net.readNet(str(network), withInternal=True)
        start = {
            "edge": config["oht"].get("initial_edge", "e0"),
            "position_m": config["oht"]["initial_position_m"],
        }
        for port in [start, *config["ports"].values()]:
            if not 0 < port["position_m"] < net.getEdge(port["edge"]).getLength():
                raise ValueError(f"起始位置或端口不在轨道内部：{port}")
        approach, approach_distance = route_between(net, start, config["ports"]["A"])
        loaded, loaded_distance = route_between(net, config["ports"]["A"], config["ports"]["B"])
        route = approach + loaded[1:]
        successors = net.getEdge(route[-1]).getOutgoing()
        if not successors:
            raise RuntimeError("交付轨道没有后继，不能保证车辆持续存在")
        route.append(next(iter(successors)).getID())
        self.route_info = {
            "empty_edges": approach,
            "loaded_edges": loaded,
            "empty_distance_m": approach_distance,
            "loaded_distance_m": loaded_distance,
        }
        (output / "route.json").write_text(
            json.dumps(self.route_info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.geometry = {
            "lanes": [
                list(lane.getShape())
                for edge in net.getEdges(withInternal=True)
                for lane in edge.getLanes()
            ],
            "ports": {
                name: list(
                    sumolib.geomhelper.positionAtShapeOffset(
                        net.getEdge(port["edge"]).getLane(0).getShape(), port["position_m"]
                    )
                )
                for name, port in config["ports"].items()
            },
            "empty_lanes": route_shapes(net, approach, start, config["ports"]["A"]),
            "loaded_lanes": route_shapes(net, loaded, config["ports"]["A"], config["ports"]["B"]),
            "equipment": config.get("network", {}).get("equipment", []),
        }
        self.api.start(
            [
                "sumo",
                "--net-file",
                str(network),
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
            vehicle = config["oht"]
            for method, value in [
                ("setLength", vehicle["length_m"]),
                ("setMinGap", vehicle["min_gap_m"]),
                ("setMaxSpeed", vehicle["max_speed_m_s"]),
                ("setAccel", vehicle["accel_m_s2"]),
                ("setDecel", vehicle["decel_m_s2"]),
                ("setImperfection", 0),
                ("setTau", max(config["step_s"], 0.2)),
                ("setSpeedFactor", 1),
                ("setSpeedDeviation", 0),
            ]:
                getattr(vt, method)("oht", value)
            self.api.route.add("mission", route)
            self.api.vehicle.add(
                self.vehicle_id,
                "mission",
                typeID="oht",
                departPos=str(vehicle["initial_position_m"]),
                departSpeed="0",
            )
            self.stop_at("A")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def build_synthetic(config: dict, output: Path) -> Path:
        binary = Path(checkBinary("netconvert"))
        if not binary.exists():
            import shutil

            found = shutil.which("netconvert")
            if not found:
                raise RuntimeError("找不到netconvert，请安装项目的sumo-tools依赖")
            binary = Path(found)
        w, h = config["track"]["width_m"], config["track"]["height_m"]
        nodes = (
            "<nodes>"
            + "".join(
                f'<node id="n{i}" x="{x}" y="{y}" type="priority"/>'
                for i, (x, y) in enumerate([(0, 0), (w, 0), (w, h), (0, h)])
            )
            + "</nodes>"
        )
        edges = (
            "<edges>"
            + "".join(
                f'<edge id="e{i}" from="n{i}" to="n{(i + 1) % 4}" '
                f'numLanes="1" speed="{config["oht"]["max_speed_m_s"]}"/>'
                for i in range(4)
            )
            + "</edges>"
        )
        (output / "nodes.nod.xml").write_text(nodes, encoding="utf-8")
        (output / "edges.edg.xml").write_text(edges, encoding="utf-8")
        network = output / "network.net.xml"
        subprocess.run(
            [
                str(binary),
                "--node-files",
                str(output / "nodes.nod.xml"),
                "--edge-files",
                str(output / "edges.edg.xml"),
                "--output-file",
                str(network),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return network

    def stop_at(self, port_name: str) -> None:
        port = self.config["ports"][port_name]
        self.api.vehicle.setStop(
            self.vehicle_id,
            port["edge"],
            pos=port["position_m"],
            duration=self.config["max_time_s"] + 1,
        )

    def depart_to_b(self) -> None:
        self.stop_at("B")
        self.api.vehicle.resume(self.vehicle_id)

    def step(self, target: float) -> None:
        self.api.simulationStep(target)
        if abs(self.api.simulation.getTime() - target) > 1e-7:
            raise RuntimeError("SUMO与主控时间不一致")
        self.teleports.extend(self.api.simulation.getStartingTeleportIDList())
        self.collisions.extend(self.api.simulation.getCollidingVehiclesIDList())
        if self.teleports or self.collisions:
            raise RuntimeError("发生瞬移或碰撞，停止仿真")
        if self.vehicle_id not in self.api.vehicle.getIDList():
            raise RuntimeError("OHT未插入或意外消失")

    def stopped_at(self, name: str) -> bool:
        port = self.config["ports"][name]
        return (
            self.api.vehicle.isStopped(self.vehicle_id)
            and self.api.vehicle.getRoadID(self.vehicle_id) == port["edge"]
            and abs(self.api.vehicle.getLanePosition(self.vehicle_id) - port["position_m"]) < 0.05
        )

    def snapshot(self) -> dict:
        vehicle = self.api.vehicle
        x, y = vehicle.getPosition(self.vehicle_id)
        return {
            "x": x,
            "y": y,
            "speed": vehicle.getSpeed(self.vehicle_id),
            "edge": vehicle.getRoadID(self.vehicle_id),
            "position_m": vehicle.getLanePosition(self.vehicle_id),
        }

    def close(self) -> None:
        self.api.close()
