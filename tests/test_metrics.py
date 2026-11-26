"""人工可计算的窗口边界、WIP面积、右删失和全局车辆时间积分。"""

import pytest

from fabcosim.cosim.metrics import TrafficMetrics, tardiness_metrics, window_metrics


def test_congestion_free_transport_reports_no_blocked_or_following_metrics():
    """无拥堵的运输档没有"想动却动不了"，这些量结构上未定义，必须报不可用而不是报数。"""
    stats = TrafficMetrics(0, 10, ["v"], 0.784, congestion_free=True)
    stats.observe(0, [{"id": "v", "distance_m": 0, "phase": "empty", "lot_id": None,
                       "waiting_reason": None, "edge": "", "position_m": 0.0}])
    stats.observe(10, [{"id": "v", "distance_m": 0, "phase": "empty", "lot_id": None,
                       "waiting_reason": None, "edge": "", "position_m": 0.0}])
    result = stats.result()
    assert result["congestion_metrics"] == "not_available_for_congestion_free_transport"
    assert result["minimum_same_edge_gap_m"] is None
    for field in ("blocked_s", "blocked_fraction", "zcu_wait_s", "following_wait_s",
                  "unclassified_wait_s"):
        assert field not in result["fleet"], field
    # 仍有意义的量照常给出。
    assert "busy_fraction" in result["fleet"]


def test_empty_edge_vehicles_are_not_grouped_into_one_lane():
    """聚合档不给位置；若把空edge归入同一车道会算出负间距、看起来像碰撞。"""
    stats = TrafficMetrics(0, 10, ["a", "b"], 0.784)
    snapshots = [
        {"id": "a", "distance_m": 0, "phase": "empty", "lot_id": None,
         "waiting_reason": None, "edge": "", "position_m": 0.0},
        {"id": "b", "distance_m": 0, "phase": "empty", "lot_id": None,
         "waiting_reason": None, "edge": "", "position_m": 0.0},
    ]
    stats.observe(0, snapshots)
    assert stats.result()["minimum_same_edge_gap_m"] is None


def test_tardiness_counts_only_completed_and_reports_censored_separately():
    definitions = {
        "early": {"due_s": 100.0},
        "late": {"due_s": 100.0},
        "running": {"due_s": 100.0},
        "nodue": {},
    }
    completions = {"early": 90.0, "late": 160.0}
    releases = {"early": 0.0, "late": 0.0, "running": 0.0}
    result = tardiness_metrics(definitions, completions, releases, 0, 200)
    assert result["window_completed_with_due"] == 2
    assert result["on_time_count"] == 1
    assert result["on_time_fraction"] == 0.5
    assert result["late_count"] == 1
    assert result["late_mean_lag_s"] == 60.0
    # 窗口结束时仍在制的逾期lot单列，不进入均值，否则"没做完"会被算成"准时"。
    assert result["censored_in_progress_count"] == 1
    assert result["censored_in_progress_overdue_count"] == 1
    assert result["completed_lag"]["mean_s"] == 25.0


def test_tardiness_absent_without_due_dates():
    assert tardiness_metrics({"a": {}}, {}, {"a": 0.0}, 0, 10) == {}


def test_tardiness_keeps_later_completions_censored_at_earlier_window_end():
    result = tardiness_metrics(
        {"a": {"due_s": 5.0}, "b": {"due_s": 10.0}, "future": {"due_s": 8.0}},
        {"a": 20.0, "b": 10.0},
        {"a": 0.0, "b": 0.0, "future": 15.0},
        0.0,
        10.0,
    )
    assert result["window_completed_with_due"] == 1
    assert result["on_time_count"] == 1
    assert result["censored_in_progress_count"] == 1
    assert result["censored_in_progress_overdue_count"] == 1


def test_tardiness_excludes_lots_completed_before_window():
    definitions = {"a": {"due_s": 100.0}, "b": {"due_s": 100.0}}
    completions = {"a": 50.0, "b": 150.0}
    result = tardiness_metrics(definitions, completions, {"a": 0.0, "b": 0.0}, 100, 200)
    assert result["window_completed_with_due"] == 1
    assert result["late_count"] == 1


def test_unknown_stationary_wait_is_not_called_following():
    stats = TrafficMetrics(1, 2, ["v"], 0.784)
    snapshot = {
        "id": "v",
        "distance_m": 0,
        "phase": "empty",
        "waiting_reason": None,
        "lot_id": None,
        "edge": "e",
        "position_m": 2,
    }
    stats.observe(1, snapshot_list := [snapshot])
    stats.observe(2, snapshot_list)
    fleet = stats.result()["fleet"]
    assert fleet["blocked_s"] == fleet["unclassified_wait_s"] == 1
    assert fleet["following_wait_s"] == fleet["zcu_wait_s"] == 0


def test_traffic_integral_clips_window_and_counts_stationary_wait():
    stats = TrafficMetrics(5, 10, ["v"], 0.784)

    def sample(time, distance, phase, reason=None, cargo=None):
        stats.observe(
            time,
            [
                {
                    "id": "v",
                    "distance_m": distance,
                    "phase": phase,
                    "waiting_reason": reason,
                    "lot_id": cargo,
                    "edge": "e",
                    "position_m": 2,
                }
            ],
        )

    sample(4, 4, "empty")
    sample(6, 6, "empty", "zcu:zone")
    sample(8, 6, "loaded", cargo="lot")
    sample(10, 8, "parked")
    sample(100, 8, "parked")
    fleet = stats.result()["fleet"]
    assert fleet["busy_s"] == 5 and fleet["busy_fraction"] == 1
    assert fleet["empty_distance_m"] == 1
    assert fleet["loaded_distance_m"] == 2
    assert fleet["blocked_s"] == fleet["zcu_wait_s"] == 2
    assert fleet["parked_s"] == 0


def test_window_wip_balance_and_censoring():
    config = {
        "observation": {"warmup_s": 5, "end_s": 10},
        "routes": {"A": {}, "B": {}},
        "lots": [
            {"id": "a0", "route": "A", "wafer_count": 25},
            {"id": "a1", "route": "A", "wafer_count": 25},
            {"id": "b0", "route": "B", "wafer_count": 25},
        ],
    }
    events = [
        {"time": 0, "kind": "lot.released", "lot_id": "a0"},
        {"time": 5, "kind": "lot.released", "lot_id": "b0"},
        {"time": 6, "kind": "transport.requested", "task_id": "r1"},
        {"time": 7, "kind": "transport.requested", "task_id": "r2"},
        {"time": 7, "kind": "lot.released", "lot_id": "a1"},
        {"time": 8, "kind": "lot.completed", "lot_id": "a0"},
        {"time": 9, "kind": "transport.pickup_start", "task_id": "r1"},
        {"time": 10, "kind": "lot.completed", "lot_id": "b0"},
    ]
    result = window_metrics(config, events, {})
    assert result["start_wip_lots"] == 2
    assert result["window_released_lots"] == 1
    assert result["window_completed_lots"] == 2
    assert result["end_wip_lots"] == 1
    assert result["mean_wip_lots"] == pytest.approx(2.2)
    assert result["peak_wip_lots"] == 3
    assert result["transport_wait"]["uncensored"]["mean_s"] == 3
    assert result["transport_wait"]["right_censored_count"] == 1
    assert result["by_product"]["A"]["end_wip_age"]["mean_s"] == 3
