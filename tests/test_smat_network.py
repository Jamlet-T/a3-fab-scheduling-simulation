"""真实空间接入的关键回归：源拓扑、端口映射、连续取送和同轨道上游寻路。"""

import json
import math
from pathlib import Path

import pytest
import sumolib

from fabcosim.cosim.demo import load_config, run_demo
from fabcosim.transport.sumo import route_between


@pytest.fixture(scope="module")
def smat_run(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "configs/smat_two_tools.json")
    if not (root / config["network"]["workbook"]).exists():
        pytest.skip("当前工作区没有SMAT2022原始Excel")
    output = tmp_path_factory.mktemp("smat") / "run"
    summary = run_demo(config, output)
    return output, summary


def test_source_topology_and_port_positions_preserved(smat_run):
    output, summary = smat_run
    net = sumolib.net.readNet(str(output / "network.net.xml"))
    mapping = json.loads((output / "network_mapping.json").read_text("utf-8"))
    assert len(net.getNodes()) == summary["network"]["node_count"] == 2858
    assert len(net.getEdges()) == summary["network"]["edge_count"] == 3424
    assert len(mapping["ports"]) == 22120
    nodes = {row["NAME"]: row for row in mapping["nodes"]}
    for rail in mapping["rails"]:
        edge = net.getEdge(rail["NAME"])
        assert edge.getFromNode().getID() == rail["FROM_NODE"]
        assert edge.getToNode().getID() == rail["TO_NODE"]
        expected = [
            (nodes[name]["POSITION_X"] * 0.001, nodes[name]["POSITION_Y"] * 0.001)
            for name in (rail["FROM_NODE"], rail["TO_NODE"])
        ]
        assert all(math.dist(a, b) < 1e-8 for a, b in zip(edge.getLane(0).getShape(), expected))
    ports = summary["network"]["selected_ports"]
    assert ports["A"]["source_id"] == "DIF0138_Port1"
    assert ports["A"]["position_m"] == pytest.approx(1.125)
    assert ports["B"]["source_id"] == "ECH0578_Port1"
    assert ports["B"]["position_m"] == pytest.approx(4.459)


def test_real_vehicle_handoff_and_continuous_trajectory(smat_run):
    output, summary = smat_run
    # 运行输出含源车辆ID等解析字段，仍须能够作为配置重新读取。
    saved_config = load_config(output / "config.json")
    assert saved_config["oht"]["source_id"] == "SMT_oht_0"
    events = json.loads((output / "events.json").read_text("utf-8"))
    rows = json.loads((output / "trajectory.json").read_text("utf-8"))
    times = {e["kind"]: e["time"] for e in events}
    assert times["transport.arrived_A"] <= times["transport.pickup_start"]
    assert times["transport.arrived_B"] < times["transport.delivered"]
    assert times["transport.delivered"] <= times["B.processing_start"]
    assert all(row["holders"] == 1 for row in rows)
    assert all(
        math.hypot(b["x"] - a["x"], b["y"] - a["y"]) <= 0.150001 for a, b in zip(rows, rows[1:])
    )
    assert rows[0]["edge"] == "rl_B1_2_3"
    assert rows[0]["position_m"] == pytest.approx(0.79719387755102)
    assert rows[-1]["edge"] == "rl_B18_20_21"
    assert rows[-1]["position_m"] == pytest.approx(4.459, abs=0.05)
    assert summary["route"]["loaded_distance_m"] > 100
    assert summary["loaded_travel_s"] >= summary["route"]["loaded_distance_m"] / 1.5
    assert summary["vehicle_id"] == "SMT_oht_0"
    assert summary["vehicle_count_at_end"] == 1
    assert summary["teleports"] == summary["collisions"] == []
    assert summary["completed_operations"] == 2


def test_same_edge_upstream_destination_requires_directed_loop(smat_run):
    output, _ = smat_run
    net = sumolib.net.readNet(str(output / "network.net.xml"))
    edge = "rl_A20_4_5"
    path, distance = route_between(
        net, {"edge": edge, "position_m": 2}, {"edge": edge, "position_m": 1}
    )
    assert len(path) > 2
    assert path[0] == path[-1] == edge
    assert all(net.getEdge(b) in net.getEdge(a).getOutgoing() for a, b in zip(path, path[1:]))
    expected = sum(net.getEdge(e).getLength() for e in path) - 2 - net.getEdge(edge).getLength() + 1
    assert distance == pytest.approx(expected)
    assert distance > 1
