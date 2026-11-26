"""运输引擎接口：主循环只能通过公开方法取用运输侧状态。

目的是让替代运输模型只需实现同一组方法。此前`full.py`直接调用
`engine.api.vehicle.getLeader`等SUMO专有接口、并直接读`engine.owners`等内部结构。
"""

from types import SimpleNamespace

from fabcosim.transport.fleet import FleetTransport, Gate


def stub_engine(ids=("a", "b"), leader=None, stops="stop-a", time=123.5):
    """只带接口所需最小状态的运输引擎替身。"""
    engine = FleetTransport.__new__(FleetTransport)
    engine.ids = list(ids)
    engine.waiting = {vid: None for vid in ids}
    engine.plans = {}
    gate_a = Gate("z1", 10, 15, "edge", 8, 8, "granted", True)
    gate_b = Gate("z2", 20, 25, "edge2", 3, 3, "queued", False)
    engine.owners = {"z1": ("a", gate_a)}
    engine.queues = {"z2": [("b", gate_b)], "empty": []}
    engine.api = SimpleNamespace(
        vehicle=SimpleNamespace(
            getIDList=lambda: list(ids),
            getLeader=lambda vid, distance: leader,
            getStops=lambda vid: stops,
        ),
        simulation=SimpleNamespace(getTime=lambda: time),
    )
    return engine


def test_present_reports_whether_every_vehicle_is_still_in_the_engine():
    assert stub_engine().present() is True
    missing = stub_engine()
    missing.api.vehicle.getIDList = lambda: ["a"]
    assert missing.present() is False


def test_scalar_queries_delegate_to_the_engine():
    engine = stub_engine(leader=("b", 1.25))
    assert engine.leader("a", 8) == ("b", 1.25)
    assert engine.stops("a") == "stop-a"
    assert engine.integrator_time() == 123.5


def test_zone_owners_and_queues_are_plain_views_for_replay_frames():
    engine = stub_engine()
    assert engine.zone_owners() == {"z1": "a"}
    # 空队列不出现在回放帧里。
    assert engine.zone_queues() == {"z2": ["b"]}


def test_zone_state_serializes_gates_for_checkpoints():
    state = stub_engine().zone_state()
    assert set(state) == {"owners", "queues"}
    assert state["owners"]["z1"] == {
        "vehicle_id": "a",
        "zone": "z1",
        "enter": 10,
        "exit": 15,
        "hold_edge": "edge",
        "hold_pos": 8,
        "hold_distance": 8,
        "state": "granted",
        "installed": True,
    }
    assert state["queues"]["z2"][0]["vehicle_id"] == "b"
    assert "empty" not in state["queues"]


def test_stall_state_carries_everything_needed_to_diagnose_a_stall():
    engine = stub_engine(leader=("b", 0.5))
    engine.plans = {"a": {"gates": [Gate("z1", 10, 15, "edge", 8, 8, "granted", True)]}}
    state = engine.stall_state()
    assert set(state) == {"native_stops", "leaders", "owners", "plans"}
    assert state["native_stops"] == {"a": "stop-a", "b": "stop-a"}
    assert state["leaders"] == {"a": ("b", 0.5), "b": ("b", 0.5)}
    assert state["owners"]["z1"]["vehicle_id"] == "a"
    # 计划里的gate转成普通字典，落盘时才可序列化。
    assert state["plans"]["a"]["gates"][0]["zone"] == "z1"
    assert isinstance(state["plans"]["a"]["gates"][0], dict)


def test_full_loop_does_not_touch_engine_internals():
    """主循环只应通过公开方法取用运输侧状态；出现engine.api等即回归。"""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/fabcosim/cosim/full.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("engine.api", "engine.owners", "engine.queues", "engine.plans"):
        assert forbidden not in source, f"full.py不应直接使用{forbidden}"
