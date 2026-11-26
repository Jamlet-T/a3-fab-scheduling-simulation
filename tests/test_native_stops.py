"""混流六车真实停滞现场的停靠契约及前车距离回归。"""

from types import SimpleNamespace

from fabcosim.transport.fleet import FleetTransport, Gate


def test_actual_native_stop_target_matches_without_moving_position():
    engine = FleetTransport.__new__(FleetTransport)
    engine.config = {"oht": {"port_stop_tolerance_m": 0.1}}
    stop = SimpleNamespace(lane="rail_0", endPos=4.177, arrival=365768)
    vehicle = SimpleNamespace(
        isStopped=lambda _: True,
        getRoadID=lambda _: "rail",
        getLanePosition=lambda _: 4.270474230002617,
        getStops=lambda _: (stop,),
    )
    engine.api = SimpleNamespace(vehicle=vehicle)
    goal = {"edge": "rail", "position_m": 4.177}
    assert engine.arrived("v", goal)
    assert engine.position("v")["position_m"] == 4.270474230002617
    stop.endPos = 4.270  # 附近另一停靠目标不能算已到指定端口。
    assert not engine.arrived("v", goal)
    stop.endPos = 4.177
    stop.arrival = -1  # 尚未开始的未来停靠不能算到达。
    assert not engine.arrived("v", goal)


def test_stopped_leader_beyond_zone_exit_does_not_block_grant():
    engine = FleetTransport.__new__(FleetTransport)
    engine.ids = ["v"]
    engine.config = {"traffic": {"lookahead_m": 8}, "max_time_s": 100}
    gate = Gate("zone", 1, 3.34, "rail", 0, 0, state="queued", installed=True)
    engine.plans = {"v": {"gates": [gate]}}
    engine.owners, engine.queues, engine.waiting = {}, {"zone": [("v", gate)]}, {"v": None}
    engine.queued_zones, engine.queue_order = {"zone"}, {"zone": 0}
    removed = []
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(
            getDistance=lambda _: 0,
            getSpeed=lambda _: 0,
            getLeader=lambda *args: ("stopped_front", 5.795816939),
            replaceStop=lambda *args: removed.append(args),
        )
    )
    engine.record = lambda *args, **kwargs: None
    engine.control(0)
    assert engine.owners["zone"] == ("v", gate)
    assert gate.state == "granted" and removed == [("v", 0, "")]
