"""调度策略层：默认值复现原行为、未知名称报错，以及四类决策确实由配置驱动。"""

import copy
import json
import random
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes
from fabcosim.policies import destination, dispatch, production, registry
from fabcosim.production import releases


def job(lot_id, processing_s, ready_s, unit="Lot"):
    return {
        "lot_id": lot_id,
        "route": "R",
        "op": {"source_step": 1, "processing_s": processing_s, "unit": unit},
        "ready_s": ready_s,
    }


def task(task_id, ready_s, requested_s, haul_m):
    return {"id": task_id, "ready_s": ready_s, "requested_s": requested_s, "haul_m": haul_m}


def test_defaults_are_the_pre_policy_behaviour():
    resolved = registry.resolve_policies({})
    assert resolved["names"] == registry.DEFAULTS
    assert resolved["rules"]["production.job"] is production.fifo
    assert resolved["rules"]["transport.vehicle"] is dispatch.choose_vehicle


def test_explicit_defaults_match_omitted_config():
    explicit = registry.resolve_policies(
        {
            "policies": {
                "destination": "nearest_least_loaded",
                "production": {"job": "fifo", "group": "earliest_ready"},
                "transport": {"task": "earliest_ready", "vehicle": "nearest_idle"},
            }
        }
    )
    assert explicit["names"] == registry.resolve_policies({})["names"]


@pytest.mark.parametrize(
    "bad",
    [
        {"policies": []},
        {"policies": {"destination": "nearest"}},
        {"policies": {"production": {"job": "sptf"}}},
        {"policies": {"production": {"order": "fifo"}}},
        {"policies": {"transport": {"vehicle": 3}}},
        {"policies": {"production": "fifo"}},
    ],
)
def test_unknown_or_malformed_policy_config_raises(bad):
    with pytest.raises(ValueError):
        registry.resolve_policies(bad)


def test_destination_rules_trade_distance_against_queue():
    kwargs = {
        "distance": lambda name: {"near_busy": 100.0, "far_idle": 900.0}[name],
        "queue_lengths": {"near_busy": 5, "far_idle": 0},
        "penalty_m": 120.0,
    }
    # 100+5*120=700 < 900，队列惩罚不足以推翻距离差。
    assert destination.nearest_least_loaded(["near_busy", "far_idle"], **kwargs) == "near_busy"
    assert destination.nearest_only(["near_busy", "far_idle"], **kwargs) == "near_busy"
    assert destination.least_loaded(["near_busy", "far_idle"], **kwargs) == "far_idle"


def test_destination_queue_penalty_can_redirect_to_idle_tool():
    kwargs = {
        "distance": lambda name: {"near_busy": 100.0, "far_idle": 900.0}[name],
        "queue_lengths": {"near_busy": 20, "far_idle": 0},
        "penalty_m": 120.0,
    }
    # 100+20*120=2500 > 900，此时队列长度把选择推向空闲机台。
    assert destination.nearest_least_loaded(["near_busy", "far_idle"], **kwargs) == "far_idle"


def test_destination_avoids_used_batch_host_first():
    kwargs = {
        "distance": lambda name: {"a": 10.0, "b": 20.0}[name],
        "queue_lengths": {"a": 0, "b": 0},
        "penalty_m": 120.0,
        "used_hosts": frozenset({"a"}),
    }
    assert destination.nearest_least_loaded(["a", "b"], **kwargs) == "b"
    assert destination.nearest_only(["a", "b"], **kwargs) == "b"
    assert destination.least_loaded(["a", "b"], **kwargs) == "b"


def test_production_job_rules_order_the_local_queue():
    queue = [job("lot0", 300, 0), job("lot1", 60, 5), job("lot2", 900, 1)]
    assert production.fifo(queue) is queue[0]
    assert production.spt(queue)["lot_id"] == "lot1"
    assert production.lpt(queue)["lot_id"] == "lot2"
    assert production.fifo([]) is None and production.spt([]) is None


def due_job(lot_id, due_s, ready_s=0, processing_s=60):
    entry = job(lot_id, processing_s, ready_s)
    entry["due_s"] = due_s
    return entry


def test_due_date_rules_require_a_due_date():
    without = [job("lot0", 60, 0)]
    for rule in (production.edd, production.cr):
        with pytest.raises(ValueError, match="交期"):
            rule(without, now=0, remaining=lambda j: 100)


def test_edd_orders_by_earliest_due_date():
    queue = [due_job("lot0", 900), due_job("lot1", 300), due_job("lot2", 600)]
    assert production.edd(queue, now=0, remaining=lambda j: 100)["lot_id"] == "lot1"
    assert production.edd([], now=0, remaining=lambda j: 100) is None


def test_critical_ratio_accounts_for_remaining_work():
    # 交期晚但剩余工作少得多 → 临界比更小，应先加工。
    # lotA: (500-0)/1000 = 0.5；lotB: (400-0)/100 = 4.0
    queue = [due_job("lotB", 400), due_job("lotA", 500)]
    remaining = {"lotA": 1000.0, "lotB": 100.0}

    def cr(j):
        return remaining[j["lot_id"]]

    assert production.cr(queue, now=0, remaining=cr)["lot_id"] == "lotA"
    # 只看交期会把lotB排在前面，说明剩余工作确实参与了比较。
    assert production.edd(queue, now=0, remaining=cr)["lot_id"] == "lotB"
    # 剩余工作相同时退回最早交期。
    same = {"lotA": 1000.0, "lotB": 1000.0}
    assert production.cr(queue, now=0, remaining=lambda j: same[j["lot_id"]])["lot_id"] == "lotB"


def test_critical_ratio_rejects_missing_context():
    with pytest.raises(ValueError, match="上下文"):
        production.cr([due_job("lot0", 900)], now=None, remaining=None)


def test_production_group_rules_pick_a_legal_group():
    small = [job("lot0", 1, 0)]
    late_but_large = [job("lot1", 1, 50), job("lot2", 1, 50)]
    groups = [late_but_large, small]
    assert production.earliest_ready(groups) is small
    assert production.largest_first(groups) is late_but_large
    assert production.earliest_ready([]) is None


def test_transport_task_rules_order_ready_tasks():
    tasks = [task("t0", 10, 1, 500), task("t1", 20, 0, 100), task("t2", 30, 2, 900)]

    def haul(t):
        return t["haul_m"]

    assert [t["id"] for t in dispatch.earliest_ready(tasks, haul=haul)] == ["t0", "t1", "t2"]
    assert [t["id"] for t in dispatch.earliest_request(tasks, haul=haul)] == ["t1", "t0", "t2"]
    assert [t["id"] for t in dispatch.shortest_haul(tasks, haul=haul)] == ["t1", "t0", "t2"]


def test_transport_vehicle_rules_pick_expected_idle_vehicle():
    def distance(vid):
        return {"a": 5.0, "b": 1.0, "c": 3.0}[vid]

    ids = ["a", "b", "c"]
    assert dispatch.choose_vehicle(ids, distance) == "b"
    assert dispatch.first_idle(ids, distance) == "a"
    assert dispatch.farthest_idle(ids, distance) == "a"


def test_take_matches_the_removal_it_replaced():
    """机台队列原用pop(0)/remove取值，改为按身份取出后必须等价。"""
    jobs = [job(f"lot{i}", 1, i) for i in range(4)]
    for target in jobs:
        by_remove = list(jobs)
        by_remove.remove(target)
        by_take = list(jobs)
        assert full.take(by_take, target) is target
        assert by_take == by_remove
    # 队头情形等价于原来的queue.pop(0)。
    queue = list(jobs)
    assert full.take(queue, queue[0]) is jobs[0]
    assert queue == jobs[1:]


def test_take_does_not_remove_an_equal_but_distinct_job():
    """值相同的两个job：等值删除会命中错的对象，按身份取出不会。"""
    first, second = job("lot0", 1, 0), job("lot0", 1, 0)
    assert first == second
    queue = [first, second]
    assert full.take(queue, second) is second
    assert queue == [first]
    with pytest.raises(StopIteration):
        full.take(queue, second)


def test_earliest_ready_matches_the_expression_it_replaced():
    """开批组默认规则是原表达式的逐字复制，含平局时取首个最小项。"""
    rng = random.Random(20261013)
    for _ in range(200):
        groups = [
            [job(f"l{i}", 1, rng.randrange(4)) for i in range(rng.randint(1, 3))]
            for _ in range(rng.randint(1, 5))
        ]
        expected = min(groups, key=lambda jobs: min(job["ready_s"] for job in jobs))
        assert production.earliest_ready(groups) is expected


def hot_job(lot_id, priority, ready_s=0, due_s=None):
    entry = job(lot_id, 60, ready_s)
    entry["priority"] = priority
    entry["due_s"] = due_s
    return entry


def test_priority_rules_require_a_priority():
    for rule in (production.priority, production.priority_edd):
        with pytest.raises(ValueError, match="优先级"):
            rule([due_job("lot0", 900)], now=0, remaining=lambda j: 1)


def test_priority_puts_hot_lots_first_regardless_of_arrival_order():
    queue = [hot_job("regular", 10, ready_s=0), hot_job("hot", 20, ready_s=5)]
    assert production.priority(queue, now=0)["lot_id"] == "hot"
    # 同一优先级内退回先到先加工。
    same = [hot_job("a", 10, ready_s=3), hot_job("b", 10, ready_s=1)]
    assert production.priority(same, now=0)["lot_id"] == "b"


def test_priority_edd_uses_due_date_only_within_the_same_priority():
    queue = [
        hot_job("regular_early", 10, ready_s=0, due_s=100),
        hot_job("hot_late", 20, ready_s=0, due_s=900),
    ]
    assert production.priority_edd(queue, now=0)["lot_id"] == "hot_late"
    same = [
        hot_job("a", 10, ready_s=0, due_s=500),
        hot_job("b", 10, ready_s=0, due_s=200),
    ]
    assert production.priority_edd(same, now=0)["lot_id"] == "b"


def test_assign_due_dates_uses_declared_flow_factor():
    config = {
        "production": {"due_flow_factor": 3.0},
        "routes": {"R": {"rpt_s": 1000.0}},
        "lots": [{"id": "a", "route": "R", "release_s": 500.0}, {"id": "b", "route": "R", "release_s": 0.0}],
    }
    releases.assign_due_dates(config)
    assert config["lots"][0]["due_s"] == 3500.0
    assert config["lots"][1]["due_s"] == 3000.0


def test_assign_due_dates_keeps_explicit_due_and_is_opt_in():
    explicit = {
        "production": {"due_flow_factor": 3.0},
        "routes": {"R": {"rpt_s": 1000.0}},
        "lots": [{"id": "a", "route": "R", "release_s": 0.0, "due_s": 42.0}],
    }
    releases.assign_due_dates(explicit)
    assert explicit["lots"][0]["due_s"] == 42.0
    # 未声明因子时不补交期，交期规则与延期指标随之不可用。
    absent = {"production": {}, "routes": {"R": {"rpt_s": 1000.0}}, "lots": [{"id": "a", "route": "R", "release_s": 0.0}]}
    releases.assign_due_dates(absent)
    assert "due_s" not in absent["lots"][0]
    with pytest.raises(ValueError, match="due_flow_factor"):
        releases.assign_due_dates(
            {"production": {"due_flow_factor": 0}, "routes": {"R": {"rpt_s": 1.0}}, "lots": []}
        )


CALENDAR = Path(__file__).resolve().parents[1] / "03-数据/基准-v1/production_release.csv"


def _calendar_config(products=("Product_1", "Product_2"), duration_s=3 * 86400):
    return {
        "production": {"routes": [f"Route_{p}" for p in products]},
        "release_calendar": {
            "products": list(products),
            "start_s": 0,
            "duration_s": duration_s,
        },
    }


@pytest.mark.skipif(not CALENDAR.exists(), reason="缺少冻结释放日历")
def test_calendar_releases_carry_real_due_dates_and_priorities():
    root = Path(__file__).resolve().parents[1]
    loaded = releases.load_calendar(_calendar_config(), root)
    lots = loaded["lots"]
    assert len(lots) > 20
    assert len({lot["id"] for lot in lots}) == len(lots)
    assert {lot["route"] for lot in lots} == {"Route_Product_1", "Route_Product_2"}
    # 交期是数据真值：全部晚于释放，且各批的允许时长不相等（不是单一倍数算出来的）。
    assert all(lot["due_s"] > lot["release_s"] for lot in lots)
    assert len({round(lot["due_s"] - lot["release_s"], 6) for lot in lots}) > 1
    assert {lot["priority"] for lot in lots} <= {10, 20, 30}
    assert loaded["source_calendar"]["selected_lots"] == len(lots)
    assert len(loaded["source_calendar"]["sha256"]) == 64
    # 取用顺序固定为源行顺序，重复调用结果一致（load_calendar会写回lots，故用新配置）。
    again = releases.load_calendar(_calendar_config(), root)
    assert [lot["id"] for lot in again["lots"]] == [lot["id"] for lot in lots]


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"products": ["Product_1"], "start_s": 8e7, "duration_s": 10}, "没有释放事件"),
        ({"products": [], "start_s": 0, "duration_s": 10}, "不能为空"),
        ({"products": ["Product_99"], "start_s": 0, "duration_s": 10}, "未知产品"),
        ({"products": ["Product_1"], "start_s": 0, "duration_s": 0}, "必须为正"),
    ],
)
@pytest.mark.skipif(not CALENDAR.exists(), reason="缺少冻结释放日历")
def test_calendar_rejects_invalid_windows(bad, message):
    root = Path(__file__).resolve().parents[1]
    config = {"production": {"routes": ["Route_Product_1"]}, "release_calendar": bad}
    with pytest.raises(ValueError, match=message):
        releases.load_calendar(config, root)


def test_calendar_conflicts_with_other_release_sources():
    with pytest.raises(ValueError, match="投片来源必须唯一"):
        releases.prepare_releases(
            {"release_calendar": {}, "lots": [{"id": "a"}]}
        )


def test_calendar_rejects_routes_that_are_not_compiled():
    root = Path(__file__).resolve().parents[1]
    if not CALENDAR.exists():
        pytest.skip("缺少冻结释放日历")
    config = {
        "production": {"routes": ["Route_Product_1"]},
        "release_calendar": {"products": ["Product_2"], "start_s": 0, "duration_s": 86400},
    }
    with pytest.raises(ValueError, match="未在production.routes中编译"):
        releases.load_calendar(config, root)


def _short_scenario(root: Path, batch_first: bool, dispatch_mode: str = "immediate") -> dict:
    """两步合成工艺：首道可设为Batch，用于覆盖开批组策略。"""
    config = json.loads((root / "configs/smat_full_product1.json").read_text("utf-8"))
    resolved = compile_full_routes(config, root)
    resolved["production"]["dispatch_mode"] = dispatch_mode
    ops = [
        copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][index])
        for index in (369, 370)
    ]
    for index, op in enumerate(ops, 1):
        op.update(
            source_step=index,
            processing_s=1,
            offset_s=0,
            interval_s=0,
            sample_probability=1,
            rework_probability=0,
            rework_step=0,
            dedication_step=0,
            cqt_step=0,
        )
    if batch_first:
        ops[0].update(unit="Batch", batch_min=1, batch_max=25)
        resolved["production"]["batch_policy"] = "finite_cohort"
    resolved["routes"]["Route_Product_1"]["operations"] = ops
    resolved["lots"] = resolved["lots"][:1]
    resolved["max_time_s"] = 10000
    resolved["replay"] = {"enabled": False}
    resolved["performance"] = {
        "stream_events": True,
        "route_event_geometry": False,
        "compact_json": True,
    }
    return resolved


def test_short_run_is_identical_with_and_without_explicit_defaults(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "configs/smat_full_product1.json").exists():
        pytest.skip("缺少原始路网")
    results = {}
    for label, policies in (
        ("omitted", None),
        ("explicit", {"policies": registry.DEFAULTS | {"production.group": "earliest_ready"}}),
    ):
        scenario = _short_scenario(root, batch_first=True)
        if policies is not None:
            scenario["policies"] = {
                "destination": "nearest_least_loaded",
                "production": {"job": "fifo", "group": "earliest_ready"},
                "transport": {"task": "earliest_ready", "vehicle": "nearest_idle"},
            }
        monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kw: copy.deepcopy(scenario))
        output = tmp_path / label
        summary = full.run_full(scenario, output)
        events = json.loads((output / "events.json").read_text("utf-8"))
        summary.pop("policies")
        results[label] = (summary, events)
        assert summary["completed_lots"] == 1
        assert summary["collisions"] == summary["teleports"] == []
    assert results["omitted"] == results["explicit"]


def test_single_item_route_is_identical_under_both_dispatch_modes(monkeypatch, tmp_path):
    """单件工序不涉及成批时机，两种派工模式必须给出逐字节相同的结果。"""
    root = Path(__file__).resolve().parents[1]
    if not (root / "configs/smat_full_product1.json").exists():
        pytest.skip("缺少原始路网")
    results = {}
    for mode in ("immediate", "deferred"):
        scenario = _short_scenario(root, batch_first=False, dispatch_mode=mode)
        monkeypatch.setattr(
            full, "compile_full_routes", lambda *args, **kw: copy.deepcopy(scenario)
        )
        output = tmp_path / mode
        summary = full.run_full(scenario, output)
        events = json.loads((output / "events.json").read_text("utf-8"))
        summary.pop("policies")
        summary.pop("dispatch_mode")
        assert summary["completed_lots"] == 1
        assert summary["completed_tasks"] >= 2
        assert summary["collisions"] == summary["teleports"] == []
        results[mode] = (summary, events)
    assert results["immediate"] == results["deferred"]


def test_deferred_mode_on_a_batching_route_completes_with_conservation(monkeypatch, tmp_path):
    """成批路线在推迟派工下仍排空；两种模式的成批时机不同，不要求结果相同。"""
    root = Path(__file__).resolve().parents[1]
    if not (root / "configs/smat_full_product1.json").exists():
        pytest.skip("缺少原始路网")
    outputs = {}
    for mode in ("immediate", "deferred"):
        scenario = _short_scenario(root, batch_first=True, dispatch_mode=mode)
        monkeypatch.setattr(
            full, "compile_full_routes", lambda *args, **kw: copy.deepcopy(scenario)
        )
        output = tmp_path / f"batch-{mode}"
        summary = full.run_full(scenario, output)
        assert summary["completed_lots"] == 1
        assert summary["collisions"] == summary["teleports"] == []
        outputs[mode] = summary["completed_operations"]
    assert all(value > 0 for value in outputs.values())


def test_dispatch_mode_is_recorded_and_validated(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "configs/smat_full_product1.json").exists():
        pytest.skip("缺少原始路网")
    scenario = _short_scenario(root, batch_first=False, dispatch_mode="deferred")
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kw: copy.deepcopy(scenario))
    summary = full.run_full(scenario, tmp_path / "recorded")
    assert summary["dispatch_mode"] == "deferred"
    scenario["production"]["dispatch_mode"] = "at-once"
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kw: copy.deepcopy(scenario))
    with pytest.raises(ValueError, match="dispatch_mode"):
        full.run_full(scenario, tmp_path / "rejected")


def test_configured_rules_reach_all_five_decision_points(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "configs/smat_full_product1.json").exists():
        pytest.skip("缺少原始路网")
    calls = {}
    for key, name in registry.DEFAULTS.items():
        rule = registry.RULES[key][name]

        def counted(*args, _key=key, _rule=rule, **kwargs):
            calls[_key] = calls.get(_key, 0) + 1
            return _rule(*args, **kwargs)

        monkeypatch.setitem(registry.RULES[key], name, counted)
    scenario = _short_scenario(root, batch_first=True)
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kw: copy.deepcopy(scenario))
    summary = full.run_full(scenario, tmp_path / "wired")
    assert summary["completed_lots"] == 1
    assert set(calls) == set(registry.DEFAULTS)
    assert all(count > 0 for count in calls.values())
