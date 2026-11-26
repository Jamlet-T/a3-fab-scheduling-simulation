"""机台可用性：PM与非计划停机的调度与参数读取。

源表只给参数、不给停机与在制批次的交互细则；本实现固定一种规则（停机在机台空闲后、
下一批开工前执行，不打断在制批次），这里覆盖该规则的可观测行为。
"""

import json
from pathlib import Path

import pytest

from fabcosim.data.availability import load_availability
from fabcosim.production.availability import MachineAvailability, machine_rng

WORKBOOK = (
    Path(__file__).resolve().parents[1]
    / "03-数据/SMT2020-official/extracted/SMT_2020 - Final/General Data/dataset 4"
    / "SMT_2020_Model_Data_-_LVHM_E.xlsx"
)


def test_disabled_availability_never_reports_an_outage():
    engine = MachineAvailability("M1", {}, seed=1)
    assert engine.counter_due(10_000) is None
    assert engine.next_calendar_at() is None
    assert engine.has_calendar_events() is False
    empty = MachineAvailability("M1", None, seed=1)
    assert empty.counter_due(10**9) is None and empty.next_calendar_at() is None


def test_counter_based_pm_fires_after_the_configured_piece_count():
    params = {
        "pm": [
            {
                "event": "M1_MN",
                "counter": True,
                "period": 100,
                "first": 150,
                "distribution": "uniform",
                "mean": 3600.0,
                "offset": 0.0,
            }
        ]
    }
    engine = MachineAvailability("M1", params, seed=7)
    # 计数制PM的首次到期在`first`片，不是`period`片。
    assert engine.counter_due(149) is None
    event, duration = engine.counter_due(150)
    assert event == "M1_MN" and duration == 3600.0
    # 执行后按周期重新计数：再走100片才到期。
    assert engine.counter_due(249) is None
    assert engine.counter_due(250)[0] == "M1_MN"
    # 计数制不产生日历事件，因此不会有独立停机进程。
    assert engine.next_calendar_at() is None and engine.has_calendar_events() is False


def test_calendar_based_pm_fires_at_first_then_every_period():
    params = {
        "pm": [
            {
                "event": "M1_MN",
                "counter": False,
                "period": 30 * 86400,
                "first": 29.1 * 86400,
                "distribution": "uniform",
                "mean": 3600.0,
                "offset": 0.0,
            }
        ]
    }
    engine = MachineAvailability("M1", params, seed=3)
    first = 29.1 * 86400
    assert engine.has_calendar_events() is True
    assert engine.next_calendar_at() == first
    assert engine.take_calendar(first)[0] == "M1_MN"
    # 下一次到期相对本次执行时刻推进一个周期，且与机台是否在用无关。
    assert engine.next_calendar_at() == first + 30 * 86400


def test_most_overdue_entry_wins_so_short_periods_do_not_starve_others():
    """周期短的一项总是先到期；若只看第一项，长周期项永远轮不到。"""
    params = {
        "pm": [
            {"event": "FAST", "counter": True, "period": 10, "first": 10,
             "distribution": "uniform", "mean": 10.0, "offset": 0.0},
            {"event": "SLOW", "counter": True, "period": 100, "first": 100,
             "distribution": "uniform", "mean": 20.0, "offset": 0.0},
        ]
    }
    engine = MachineAvailability("M1", params, seed=1)
    # 走到120片时两项都逾期。关键是SLOW不会因为FAST周期短就永远轮不到。
    fired = [engine.counter_due(120)[0], engine.counter_due(120)[0]]
    assert set(fired) == {"FAST", "SLOW"}


def test_breakdown_first_time_is_sampled_from_the_exponential_foa():
    """源表的首次时刻分布是exponential，FOA是它的均值；首次故障时刻因此是抽样而非定值。"""
    params = {"breakdown": {"event": "BREAK_X", "ttf": 100.0, "ttr": 10.0, "first": 500.0}}
    engine = MachineAvailability("M1", params, seed=11)
    fired_at = engine.next_calendar_at()
    assert 0.0 < fired_at < 500.0 * 5  # 指数分布，落在均值数倍内
    event, duration = engine.take_calendar(fired_at)
    assert event == "BREAK_X" and duration > 0
    # 修复结束才开始重新计TTF：下一次到期必定晚于本次修复结束。
    assert engine.next_calendar_at() > fired_at + duration


def test_random_streams_are_per_machine_and_reproducible():
    assert machine_rng(5, "M1").random() == machine_rng(5, "M1").random()
    assert machine_rng(5, "M1").random() != machine_rng(5, "M2").random()
    a = MachineAvailability("M1", {"breakdown": {"event": "b", "ttf": 10.0, "ttr": 1.0, "first": 10.0}}, 5)
    b = MachineAvailability("M1", {"breakdown": {"event": "b", "ttf": 10.0, "ttr": 1.0, "first": 10.0}}, 5)
    assert a.down_at == b.down_at


@pytest.mark.skipif(not WORKBOOK.exists(), reason="缺少原始工作簿")
def test_source_tables_are_read_with_the_expected_semantics():
    tables = load_availability(WORKBOOK, True)
    pm, breakdown = tables["pm_by_group"], tables["breakdown_by_area"]
    assert len(pm) == 105 and len(breakdown) == 11
    calendar = [e for v in pm.values() for e in v if not e["counter"]]
    counter = [e for v in pm.values() for e in v if e["counter"]]
    assert len(calendar) == 79 and len(counter) == 213
    # 日历制周期以秒计（30/91/7日），计数制以片数计。
    assert {round(e["period"] / 86400) for e in calendar} == {7, 30, 91}
    assert all(e["period"] > 100 for e in counter)
    sample = breakdown["Def_Met"]
    assert sample["ttf"] == 604800.0 and abs(sample["ttr"] - 2116.8) < 1e-6


def _short_scenario(root, availability=None):
    import copy

    from fabcosim.data.full_route import compile_full_routes

    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    resolved = compile_full_routes(config, root)
    ops = [
        copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][index])
        for index in (369, 370)
    ]
    for index, op in enumerate(ops, 1):
        op.update(
            source_step=index, processing_s=1, offset_s=0, interval_s=0,
            sample_probability=1, rework_probability=0, rework_step=0,
            dedication_step=0, cqt_step=0,
        )
    resolved["routes"]["Route_Product_1"]["operations"] = ops
    resolved["lots"] = resolved["lots"][:1]
    resolved["max_time_s"] = 100000
    resolved["replay"] = {"enabled": False}
    resolved["performance"] = {
        "stream_events": True, "route_event_geometry": False, "compact_json": True,
    }
    if availability is not None:
        resolved["availability"] = availability
    return resolved, ops


@pytest.mark.skipif(not WORKBOOK.exists(), reason="缺少原始工作簿")
def test_calendar_outage_blocks_processing_until_it_ends(monkeypatch, tmp_path):
    """停机期间该机台不得开工：日历制停机持住闸门，正在加工的批次不被打断。"""
    from fabcosim.cosim import full

    root = Path(__file__).resolve().parents[1]
    scenario, ops = _short_scenario(root, {"enabled": True})
    group = ops[0]["tool_group"]
    area = scenario["toolgroups"][group]["AREA"]
    tables = {
        "pm_by_group": {},
        "breakdown_by_area": {
            area: {"event": "BREAK_TEST", "ttf": 1e9, "ttr": 7200.0, "first": 1.0}
        },
    }
    monkeypatch.setattr(full, "load_availability", lambda *a, **k: tables)
    monkeypatch.setattr(full, "compile_full_routes", lambda *a, **k: scenario)
    output = tmp_path / "outage"
    summary = full.run_full(scenario, output)
    events = json.loads((output / "events.json").read_text("utf-8"))
    downs = [e for e in events if e["kind"] == "availability.down"]
    ups = [e for e in events if e["kind"] == "availability.up"]
    assert downs and ups
    assert summary["availability_outages"] == len(downs) > 0
    assert summary["availability_down_s"] > 0
    # 每段停机期间，该机台不得有新的开工。
    for down in downs:
        equipment = down["equipment"]
        end = down["time"] + down["duration_s"]
        starts = [
            e["time"] for e in events
            if e["kind"] == "processing.start" and e["equipment"] == equipment
        ]
        assert not any(down["time"] < t < end for t in starts), "停机期间不得开工"
    assert summary["completed_lots"] == 1
    assert summary["collisions"] == summary["teleports"] == []


@pytest.mark.skipif(not WORKBOOK.exists(), reason="缺少原始工作簿")
def test_disabled_availability_emits_no_outage_events(monkeypatch, tmp_path):
    from fabcosim.cosim import full

    root = Path(__file__).resolve().parents[1]
    scenario, _ = _short_scenario(root, None)
    monkeypatch.setattr(full, "compile_full_routes", lambda *a, **k: scenario)
    output = tmp_path / "nofault"
    summary = full.run_full(scenario, output)
    events = json.loads((output / "events.json").read_text("utf-8"))
    assert not any(e["kind"].startswith("availability.") for e in events)
    assert summary["availability"] is None
    assert summary["completed_lots"] == 1


@pytest.mark.skipif(not WORKBOOK.exists(), reason="缺少原始工作簿")
def test_unsupported_distribution_is_rejected(tmp_path):
    import openpyxl

    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, header in (
        ("PM", ["PM EVENT NAME", "PM EVENT VALID FOR TYPE", "TYPE NAME", "PM TYPE",
                "MTBeforePM", "MTBPM UNITS", "TTR DISTRIBUTION", "MEAN", "OFFSET",
                "TTR UNITS", "FIRST ONE AT DISTRIBUTION", "FOA", "FOA UNITS"]),
        ("Breakdown", ["DOWN EVENT NAME", "DOWN EVENT VALID FOR TYPE", "TYPE NAME", "DOWN TYPE",
                       "TTF DISTRIBUTION", "MTTF", "MTTF UNITS", "TTR DISTRIBUTION", "MTTR",
                       "MTTR UNITS", "FIRST ONE AT DISTRIBUTION", "FOA", "FOA UNITS"]),
    ):
        sheet = book.create_sheet(name)
        sheet.append(header)
    sheet = book["PM"]
    sheet.append(["E", "toolgroup", "TG", "time based (calendar)", 30, "day",
                  "normal", 1, 0, "hr", "constant", 1, "day"])
    path = tmp_path / "book.xlsx"
    book.save(path)
    with pytest.raises(ValueError, match="时长分布不支持"):
        load_availability(path, False)
