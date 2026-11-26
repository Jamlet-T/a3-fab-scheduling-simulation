"""增量物料审计器：脏lot复检、端口索引差量、全量cargo扫描与注册表身份约束。

既有用例同时跑``port_index=False``（全端口扫描）与``True``（脏端口差量）两种
模式；新增用例覆盖等值替换、清空不改location、双端口重复持有、失败重试、
只读端口注册与随机合法流转逐步对照。
"""

import random

import pytest

from fabcosim.cosim import full
from fabcosim.ports.audit import IncrementalMaterialAudit
from fabcosim.ports.model import Lot, Port

MODES = [False, True]


def _setup(port_index=False, checker=None):
    lots = {"L1": Lot("L1", "release_queue")}
    ports = {"in": Port("in.port"), "out": Port("out.port")}
    audit = IncrementalMaterialAudit(
        lots, ports, checker or full._check_material, port_index=port_index
    )
    return lots, ports, audit


def _outcome(call):
    try:
        call()
    except RuntimeError as error:
        return str(error)
    return None


def _compare(lots, ports, vehicles, audit):
    expected = _outcome(lambda: full._check_material(dict(lots), dict(ports), vehicles, {}))
    actual = _outcome(lambda: audit.check(vehicles, {}))
    assert actual == expected
    return actual


@pytest.mark.parametrize("port_index", MODES)
def test_legal_flow_release_port_cargo_complete(port_index):
    lots, ports, audit = _setup(port_index)
    lot, vehicles, cache = lots["L1"], {"v1": {"cargo": None}}, {}
    lot.location = "in.queue"
    assert audit.check(vehicles, cache) == 1
    lot.location = "v1"
    vehicles["v1"]["cargo"] = lot
    assert audit.check(vehicles, cache) == 1
    ports["in"].put(lot, "v1")
    vehicles["v1"]["cargo"] = None
    assert audit.check(vehicles, cache) == 1
    vehicles["v1"]["cargo"] = ports["in"].take("v1")
    assert audit.check(vehicles, cache) == 1
    ports["out"].put(vehicles["v1"]["cargo"], "v1")
    vehicles["v1"]["cargo"] = None
    assert audit.check(vehicles, cache) == 1
    vehicles["v1"]["cargo"] = ports["out"].take("v1")
    assert audit.check(vehicles, cache) == 1
    lot.location = "completed"
    vehicles["v1"]["cargo"] = None
    assert audit.check(vehicles, cache) == 1
    assert audit.full_checks == 1


@pytest.mark.parametrize("port_index", MODES)
@pytest.mark.parametrize("location", ["release_queue", "completed"])
def test_port_injection_of_idle_lot_is_detected(port_index, location):
    lots, ports, audit = _setup(port_index)
    lot = lots["L1"]
    lot.location = location
    audit.check({}, {})
    ports["in"].lot = lot  # 绕过put，location保持不变
    # 原检查函数按location反查端口，这类注入只能由全量端口持有数交叉核对发现。
    with pytest.raises(RuntimeError, match="端口持有数错误：L1 / 1 != 0"):
        audit.check({}, {})


@pytest.mark.parametrize("port_index", MODES)
def test_cleared_port_with_unchanged_location_is_detected(port_index):
    lots, ports, audit = _setup(port_index)
    lot = lots["L1"]
    lot.location = "in.port"
    ports["in"].lot = lot
    assert audit.check({}, {}) == 1
    ports["in"].lot = None
    with pytest.raises(RuntimeError, match="物料守恒错误：L1 / 0"):
        audit.check({}, {})


@pytest.mark.parametrize("port_index", MODES)
def test_cleared_cargo_with_unchanged_location_is_detected(port_index):
    lots, ports, audit = _setup(port_index)
    lot, vehicles = lots["L1"], {"v1": {"cargo": None}}
    lot.location = "v1"
    vehicles["v1"]["cargo"] = lot
    assert audit.check(vehicles, {}) == 1
    vehicles["v1"]["cargo"] = None
    with pytest.raises(RuntimeError, match="物料守恒错误：L1 / 0"):
        audit.check(vehicles, {})


@pytest.mark.parametrize("port_index", MODES)
def test_two_ports_holding_same_lot_is_detected(port_index):
    lots, ports, audit = _setup(port_index)
    lot = lots["L1"]
    lot.location = "in.port"
    ports["in"].lot = lot
    ports["out"].lot = lot
    with pytest.raises(RuntimeError, match="端口持有数错误：L1 / 2 != 1"):
        audit.check({}, {})


@pytest.mark.parametrize("port_index", MODES)
@pytest.mark.parametrize("initial", ["release_queue", "completed"])
def test_location_edit_of_idle_lot_is_detected(port_index, initial):
    lots, ports, audit = _setup(port_index)
    lot = lots["L1"]
    lot.location = initial
    audit.check({}, {})
    lot.location = "nowhere"
    with pytest.raises(RuntimeError, match="物料守恒错误：L1 / 0"):
        audit.check({}, {})


@pytest.mark.parametrize("port_index", MODES)
def test_unregistered_objects_are_rejected(port_index):
    lots, ports, audit = _setup(port_index)
    ports["in"].lot = Lot("L1", "in.port")
    with pytest.raises(RuntimeError, match="注册表不一致"):
        audit.check({}, {})
    ports["in"].lot = None
    with pytest.raises(RuntimeError, match="注册表不一致"):
        audit.check({"v1": {"cargo": Lot("L1", "v1")}}, {})


@pytest.mark.parametrize("port_index", MODES)
def test_registry_is_read_only_and_survives_source_replacement(port_index):
    lots, ports, audit = _setup(port_index)
    original = lots["L1"]
    with pytest.raises(TypeError):
        audit.lots["L1"] = Lot("L1", "release_queue")
    lots["L1"] = Lot("L1", "release_queue")
    assert audit.lots["L1"] is original
    audit.check({}, {})


def test_port_registry_is_read_only():
    lots = {"L1": Lot("L1", "release_queue")}
    ports = {"in": Port("in.port")}
    audit = IncrementalMaterialAudit(lots, ports, full._check_material, port_index=True)
    with pytest.raises(TypeError):
        audit.ports["other"] = Port("other.port")
    assert audit.ports["in"] is ports["in"]


@pytest.mark.parametrize("port_index", MODES)
def test_close_detaches_callbacks(port_index):
    lots, ports, audit = _setup(port_index)
    lot = lots["L1"]
    audit.close()
    assert lot._location_callback is None
    assert ports["in"]._lot_callback is None
    lot.location = "in.queue"
    ports["in"].lot = lot
    with pytest.raises(RuntimeError, match="已关闭"):
        audit.check({})
    assert Lot()._location_callback is None


@pytest.mark.parametrize("port_index", MODES)
def test_failed_check_cannot_forget_dirty_lot(port_index):
    lots, _, audit = _setup(port_index)
    lots["L1"].location = "missing"
    for _ in range(2):
        with pytest.raises(RuntimeError, match="物料守恒错误"):
            audit.check({})


@pytest.mark.parametrize("port_index", MODES)
def test_failed_check_cannot_forget_removed_port_owner(port_index):
    lots, ports, audit = _setup(port_index)
    lot = lots["L1"]
    ports["in"].put(lot, "release_queue")
    audit.check({})
    ports["in"].lot = None
    for _ in range(2):
        with pytest.raises(RuntimeError, match="物料守恒错误"):
            audit.check({})


@pytest.mark.parametrize("port_index", MODES)
def test_second_audit_cannot_replace_active_observer(port_index):
    lots, ports, audit = _setup(port_index)
    with pytest.raises(ValueError, match="已有位置观察器|已有物料观察器"):
        IncrementalMaterialAudit(lots, ports, full._check_material, port_index=port_index)
    lots["L1"].location = "missing"
    with pytest.raises(RuntimeError, match="物料守恒错误"):
        audit.check({})


def test_second_index_audit_cannot_replace_port_observer():
    lots = {"L1": Lot("L1", "release_queue")}
    ports = {"in": Port("in.port")}
    first = IncrementalMaterialAudit(lots, ports, full._check_material, port_index=True)
    ports["in"]._lot_callback = first._port_callback
    second_lots = {"L2": Lot("L2", "release_queue")}
    with pytest.raises(ValueError, match="端口已有物料观察器"):
        IncrementalMaterialAudit(second_lots, ports, full._check_material, port_index=True)


def test_constructor_failure_detaches_observer():
    lot = Lot("bad", "missing")
    port = Port("in.port")
    with pytest.raises(RuntimeError, match="物料守恒错误"):
        IncrementalMaterialAudit({"bad": lot}, {"in": port}, full._check_material, port_index=True)
    assert lot._location_callback is None
    assert port._lot_callback is None


def test_equal_but_distinct_lot_still_notifies_port_callback():
    """两个字段完全相等的不同Lot对象也要标脏；否则替换会被漏检。"""
    first = Lot("L1", "release_queue")
    second = Lot("L1", "release_queue")
    assert first == second and first is not second
    port = Port("in.port", first)
    calls = []
    port._lot_callback = calls.append
    port.lot = second
    assert [item for item in calls] == [port]
    port.lot = second
    assert [item for item in calls] == [port]


def test_index_mode_does_not_rescan_unchanged_ports():
    lots = {f"L{i}": Lot(f"L{i}", "release_queue") for i in range(3)}
    ports = {"in": Port("in.port"), "out": Port("out.port"), "mid": Port("mid.port")}
    audit = IncrementalMaterialAudit(lots, ports, full._check_material, port_index=True)
    assert audit.ports_examined == 3
    for _ in range(3):
        assert audit.check({}) == 0
    assert audit.ports_examined == 3
    lots["L0"].location = "in.queue"
    assert audit.check({}) == 1
    assert audit.ports_examined == 3
    ports["in"].put(lots["L0"], "in.queue")
    assert audit.check({}) == 1
    assert audit.ports_examined == 4
    audit.check({}, full=True)
    assert audit.ports_examined == 7


def test_scan_mode_keeps_examining_all_ports():
    lots = {f"L{i}": Lot(f"L{i}", "release_queue") for i in range(3)}
    ports = {"in": Port("in.port"), "out": Port("out.port")}
    audit = IncrementalMaterialAudit(lots, ports, full._check_material)
    assert audit.ports_examined == 2
    audit.check({})
    assert audit.ports_examined == 4


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, lots, ports, vehicles, location_cache=None):
        self.calls.append(list(lots.values()))
        full._check_material(lots, ports, vehicles, location_cache)


@pytest.mark.parametrize("port_index", MODES)
def test_unchanged_lots_are_not_rechecked(port_index):
    lots = {f"L{i}": Lot(f"L{i}", "release_queue") for i in range(4)}
    ports = {"in": Port("in.port")}
    spy = _Spy()
    audit = IncrementalMaterialAudit(lots, ports, spy, port_index=port_index)
    assert len(spy.calls) == 1 and len(spy.calls[0]) == 4
    for _ in range(3):
        assert audit.check({}, {}) == 0
    assert len(spy.calls) == 1
    assert (audit.checks, audit.full_checks, audit.checked_lots) == (4, 1, 4)
    lots["L2"].location = "in.queue"
    assert audit.check({}, {}) == 1
    assert [lot.id for lot in spy.calls[1]] == ["L2"]


def test_random_legal_flow_matches_full_checker_every_step():
    """随机合法流转逐步对照原``_check_material``全量判定，不复用期望实现。"""
    rng = random.Random(20261019)
    lots = {f"L{i}": Lot(f"L{i}", "release_queue") for i in range(4)}
    ports = {name: Port(name + ".port") for name in ("p0", "p1", "p2")}
    vehicles = {f"v{i}": {"cargo": None} for i in range(2)}
    audit = IncrementalMaterialAudit(lots, ports, full._check_material, port_index=True)
    destinations = ["a.queue", "a.tool", "b.queue"]
    for step in range(120):
        actions = []
        for lot in lots.values():
            if lot.location.endswith(".port"):
                actions.append(("take", lot))
            elif lot.location in vehicles:
                actions.append(("unload", lot))
            else:
                actions.append(("load", lot))
        kind, lot = rng.choice(sorted(actions, key=lambda item: item[1].id))
        if kind == "load":
            free = [name for name, port in ports.items() if port.lot is None]
            if free:
                ports[rng.choice(free)].put(lot, lot.location)
        elif kind == "take":
            port = ports[lot.location.removesuffix(".port")]
            free_vehicles = [vid for vid, vehicle in vehicles.items() if vehicle["cargo"] is None]
            destination = rng.choice(destinations + free_vehicles)
            taken = port.take(destination)
            assert taken is lot
            if destination in vehicles:
                vehicles[destination]["cargo"] = lot
        else:
            vehicles[lot.location]["cargo"] = None
            lot.location = rng.choice(["completed", "a.queue", "b.tool"])
        assert _compare(lots, ports, vehicles, audit) is None, f"step {step}"
        if step % 17 == 0:
            assert audit.check(vehicles, {}, full=True) == len(lots)
