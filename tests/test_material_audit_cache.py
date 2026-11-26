"""静态位置缓存仍逐lot检查动态持有状态，参考函数冻结自6e9d443。"""
import copy
import json
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.ports.model import Lot, Port


def _legacy_check_material(lots: dict, ports: dict, vehicles: dict) -> None:
    """事件边界检查真实物料归属；一次统计载荷，保留重复持有检测。"""
    cargo_counts = {}
    for vehicle in vehicles.values():
        cargo = vehicle["cargo"]
        if cargo is not None:
            cargo_counts[id(cargo)] = cargo_counts.get(id(cargo), 0) + 1
    for lot in lots.values():
        holders = int(
            lot.location in ("release_queue", "completed")
            or lot.location.endswith((".tool", ".queue"))
        )
        port = ports.get(lot.location.removesuffix(".port"))
        holders += int(port is not None and port.lot is lot)
        holders += cargo_counts.get(id(lot), 0)
        if holders != 1:
            raise RuntimeError(f"物料守恒错误：{lot.id} / {holders}")


def _outcome(call):
    try:
        call()
    except RuntimeError as error:
        return str(error)
    return None


def _compare(lots, ports, vehicles, cache):
    expected = _outcome(lambda: _legacy_check_material(lots, ports, vehicles))
    actual = _outcome(lambda: full._check_material(lots, ports, vehicles, cache))
    assert actual == expected
    return actual


@pytest.mark.parametrize("location", ["release_queue", "completed", "machine.tool", "machine.queue"])
def test_intrinsic_holder_matches_old(location):
    lot = Lot("lot", location)
    assert _compare({lot.id: lot}, {}, {}, {}) is None


@pytest.mark.parametrize("case", ["port", "vehicle", "both", "two_vehicles", "missing", "wrong_port", "intrinsic_and_vehicle"])
def test_ownership_counterexamples_match_old(case):
    lot = Lot("lot", "input.port" if case in ("port", "both", "wrong_port") else "oht")
    ports = {"input": Port("input.port", lot)} if case in ("port", "both") else {}
    if case == "wrong_port":
        ports = {"other": Port("other.port", lot)}
    vehicles = {"a": {"cargo": lot}} if case in ("vehicle", "both", "two_vehicles", "intrinsic_and_vehicle") else {}
    if case == "two_vehicles":
        vehicles["b"] = {"cargo": lot}
    if case == "intrinsic_and_vehicle":
        lot.location = "machine.queue"
    error = _compare({lot.id: lot}, ports, vehicles, {})
    assert (error is None) == (case in ("port", "vehicle"))


def test_cached_port_reads_current_lot_and_current_vehicle_cargo():
    lot = Lot("lot", "input.port")
    port = Port("input.port", lot)
    ports, lots, vehicles, cache = {"input": port}, {lot.id: lot}, {}, {}
    assert _compare(lots, ports, vehicles, cache) is None
    assert cache["input.port"] == (False, port)
    port.lot = None
    assert _compare(lots, ports, vehicles, cache).endswith(" / 0")
    vehicles["a"] = {"cargo": lot}
    assert _compare(lots, ports, vehicles, cache) is None
    port.lot = lot
    assert _compare(lots, ports, vehicles, cache).endswith(" / 2")


def test_location_changes_and_return_to_cached_location_are_rechecked():
    lot = Lot("lot", "release_queue")
    lots, cache = {lot.id: lot}, {}
    assert _compare(lots, {}, {}, cache) is None
    lot.location = "unknown"
    assert _compare(lots, {}, {}, cache).endswith(" / 0")
    lot.location = "machine.tool"
    assert _compare(lots, {}, {}, cache) is None
    lot.location = "release_queue"
    assert _compare(lots, {}, {}, cache) is None


def test_independent_run_caches_do_not_retain_other_port_objects():
    lot = Lot("lot", "input.port")
    first = Port("input.port", lot)
    second = Port("input.port")
    first_cache, second_cache = {}, {}
    assert _compare({lot.id: lot}, {"input": first}, {}, first_cache) is None
    assert _compare({lot.id: lot}, {"input": second}, {}, second_cache).endswith(" / 0")
    assert first_cache["input.port"][1] is first
    assert second_cache["input.port"][1] is second
    second.lot = lot
    assert _compare({lot.id: lot}, {"input": second}, {}, second_cache) is None


def test_three_argument_api_does_not_cache_between_calls():
    lot = Lot("lot", "input.port")
    full._check_material({lot.id: lot}, {"input": Port("input.port", lot)}, {})
    with pytest.raises(RuntimeError, match=" / 0"):
        full._check_material({lot.id: lot}, {"input": Port("input.port")}, {})


def test_exact_default_output_matches_legacy_checker(monkeypatch, tmp_path):
    from fabcosim.data.full_route import compile_full_routes

    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    if not (root / settings["network"]["workbook"]).exists():
        pytest.skip("缺少原始路网")
    resolved = compile_full_routes(settings, root)
    op = copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][369])
    op.update(source_step=1, processing_s=1, interval_s=0, sample_probability=1,
              rework_probability=0, dedication_step=0, cqt_step=0)
    resolved["routes"]["Route_Product_1"]["operations"] = [op]
    resolved["lots"] = resolved["lots"][:1]
    resolved["observation"] = {"warmup_s": 0, "end_s": 6000}
    resolved["max_time_s"] = 6000
    resolved["replay"] = {"enabled": False}
    resolved["performance"] = {}
    new_checker = full._check_material

    def old_compatible(lots, ports, vehicles, location_cache=None):
        return _legacy_check_material(lots, ports, vehicles)

    signatures = []
    for name, checker in [("old", old_compatible), ("new", new_checker)]:
        monkeypatch.setattr(full, "compile_full_routes", lambda *a: copy.deepcopy(resolved))
        monkeypatch.setattr(full, "_check_material", checker)
        output = tmp_path / name
        result = full.run_full(resolved, output)
        assert result["completed_lots"] == 1
        signatures.append(((output / "events.json").read_bytes(), (output / "summary.json").read_bytes()))
    assert signatures[0] == signatures[1]
