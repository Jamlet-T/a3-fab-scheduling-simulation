"""SMAT2022空间数据接入；只读Excel，保留源节点、轨道ID和边内偏移。"""

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from fabcosim.data.workbook import SMAT_SHEETS, read_tables


def build_smat_network(config: dict, output: Path) -> Path:
    root = Path(__file__).resolve().parents[3]
    settings = config["network"]
    source = root / settings["workbook"]
    scale = settings["coordinate_scale"]
    tables = read_tables(
        source, SMAT_SHEETS, config.get("performance", {}).get("workbook_cache", True)
    )
    indexes = {name: {r["NAME"]: r for r in rows} for name, rows in tables.items()}
    coords = {
        name: (r["POSITION_X"] * scale, r["POSITION_Y"] * scale)
        for name, r in indexes["Address"].items()
    }
    incoming, outgoing = defaultdict(list), defaultdict(list)
    lengths = {}
    for rail in tables["Rail"]:
        incoming[rail["TO_NODE"]].append(rail["NAME"])
        outgoing[rail["FROM_NODE"]].append(rail["NAME"])
        a, b = coords[rail["FROM_NODE"]], coords[rail["TO_NODE"]]
        lengths[rail["NAME"]] = math.dist(a, b)

    # 直接生成SUMO原生网络，避免道路转换器横移车道、缩短端口所在轨道或合并源节点。
    # 无内部连接边；运动网络本身不承担ZCU逻辑，多车版由FleetTransport控制。
    # 曲轨按源端点弦线近似。
    net = ET.Element("net", version="1.20", junctionCornerDetail="0", limitTurnSpeed="0")
    xs, ys = zip(*coords.values())
    boundary = f"{min(xs)},{min(ys)},{max(xs)},{max(ys)}"
    ET.SubElement(
        net,
        "location",
        netOffset="0,0",
        convBoundary=boundary,
        origBoundary=boundary,
        projParameter="!",
    )
    for rail in tables["Rail"]:
        a, b = coords[rail["FROM_NODE"]], coords[rail["TO_NODE"]]
        shape = f"{a[0]:.9f},{a[1]:.9f} {b[0]:.9f},{b[1]:.9f}"
        edge = ET.SubElement(
            net,
            "edge",
            {"id": rail["NAME"], "from": rail["FROM_NODE"], "to": rail["TO_NODE"], "priority": "1"},
        )
        ET.SubElement(
            edge,
            "lane",
            id=f"{rail['NAME']}_0",
            index="0",
            width="0.6",
            speed=str(config["oht"]["max_speed_m_s"]),
            length=f"{lengths[rail['NAME']]:.9f}",
            shape=shape,
        )
    for name, (x, y) in coords.items():
        ET.SubElement(
            net,
            "junction",
            id=name,
            type="unregulated",
            x=str(x),
            y=str(y),
            incLanes=" ".join(f"{edge}_0" for edge in incoming[name]),
            intLanes="",
            shape=f"{x},{y}",
        )
    connection_count = 0
    for name in coords:
        for start in incoming[name]:
            for end in outgoing[name]:
                ET.SubElement(
                    net,
                    "connection",
                    {
                        "from": start,
                        "to": end,
                        "fromLane": "0",
                        "toLane": "0",
                        "dir": "s",
                        "state": "M",
                    },
                )
                connection_count += 1
    path = output / "network.net.xml"
    ET.ElementTree(net).write(path, encoding="utf-8", xml_declaration=True)

    mapped_ports = []
    for port in tables["Port"]:
        position = port["DISTANCE"] * scale
        length = lengths[port["RAILLINE_NAME"]]
        if not 0 <= position <= length + 0.001:
            raise ValueError(f"端口{port['NAME']}偏移超出当前轨道几何")
        mapped_ports.append(
            {
                "id": port["NAME"],
                "equipment": port["EQP_NAME"],
                "type": port["PORT_TYPE"],
                "edge": port["RAILLINE_NAME"],
                "position_m": position,
            }
        )
    port_index = {p["id"]: p for p in mapped_ports}
    for role, selected in config["ports"].items():
        port = port_index[selected["source_id"]]
        equipment = indexes["Equipment"][port["equipment"]]
        selected.update(
            {
                "edge": port["edge"],
                "position_m": port["position_m"],
                "equipment": equipment["NAME"],
                "bay": equipment["BAY"],
                "tool_group": equipment["TOOL_GROUP"],
            }
        )
        if port["type"] != "EQPPort" and selected.get("facility_kind") not in (
            "buffer",
            "commit",
            "complete",
        ):
            raise ValueError(f"当前加工小场景的{role}需要设备端口")
    selected_vehicles = config.get("vehicles") or [{"source_id": settings["vehicle_id"]}]
    for selected in selected_vehicles:
        vehicle = indexes["Vehicle"][selected["source_id"]]
        selected.update(
            {
                "id": vehicle["NAME"],
                "edge": vehicle["RAILLINE_NAME"],
                "position_m": vehicle["DISTANCE"] * scale,
            }
        )
    vehicle = indexes["Vehicle"][selected_vehicles[0]["source_id"]]
    config["oht"].update(
        {
            "initial_edge": vehicle["RAILLINE_NAME"],
            "initial_position_m": vehicle["DISTANCE"] * scale,
            "source_id": vehicle["NAME"],
        }
    )
    report = {
        "source": settings["workbook"],
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "coordinate_scale": scale,
        "node_count": len(coords),
        "edge_count": len(lengths),
        "connection_count": connection_count,
        "equipment_count": len(tables["Equipment"]),
        "port_count": len(mapped_ports),
        "curve_chord_count": sum(bool(r["CURVE"]) for r in tables["Rail"]),
        "geometry_model": "source_endpoints_with_curve_chords_no_internal_edges",
        "rail_speed_model": "vehicle_max_speed; source Rail.MAX_SPEED not interpreted",
        "selected_ports": config["ports"],
        "selected_vehicle": config["oht"],
        "selected_vehicles": selected_vehicles,
    }
    (output / "network_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "network_mapping.json").write_text(
        json.dumps(
            {
                "rails": tables["Rail"],
                "nodes": tables["Address"],
                "ports": mapped_ports,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config["network"]["summary"] = report
    config["network"]["equipment"] = [
        {
            "id": r["NAME"],
            "bay": r["BAY"],
            "tool_group": r["TOOL_GROUP"],
            "x": r["POSITION_X"] * scale,
            "y": r["POSITION_Y"] * scale,
            "width": r["WIDTH"] * scale,
            "height": r["HEIGHT"] * scale,
        }
        for r in tables["Equipment"]
    ]
    return path
