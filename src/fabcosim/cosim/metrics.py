"""观测窗口统计：全局时间积分，未完成lot/运输等待按右删失保留。"""

import math
from statistics import mean

from fabcosim.cosim.metric_history import MetricHistory


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "mean_s": None, "p50_s": None, "p95_s": None}

    def percentile(p: float) -> float:
        position = (len(ordered) - 1) * p
        left = int(position)
        return ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * (
            position - left
        )

    return {
        "count": len(ordered),
        "mean_s": mean(ordered),
        "p50_s": percentile(0.5),
        "p95_s": percentile(0.95),
    }


class TrafficMetrics:
    def __init__(
        self,
        start: float,
        end: float,
        ids: list[str],
        length_m: float,
        congestion_free: bool = False,
    ) -> None:
        # 无拥堵的运输档（transport.model=delay）没有"想动却动不了"这件事，
        # 阻塞/跟车/区域等待在该档结构上未定义；显式报None而不是报一个像拥堵的数。
        self.congestion_free = congestion_free
        self.start, self.end, self.time = start, end, 0.0
        self.length_m, self.minimum_gap = length_m, math.inf
        self.previous = {
            vid: {"distance_m": 0, "phase": "idle", "lot_id": None, "waiting_reason": None}
            for vid in ids
        }
        self.rows = {
            vid: dict.fromkeys(
                (
                    "busy_s",
                    "parked_s",
                    "blocked_s",
                    "zcu_wait_s",
                    "following_wait_s",
                    "unclassified_wait_s",
                    "empty_distance_m",
                    "loaded_distance_m",
                    "idle_distance_m",
                ),
                0.0,
            )
            for vid in ids
        }

    def observe(self, now: float, snapshots: list[dict]) -> None:
        dt = now - self.time
        overlap = max(0.0, min(now, self.end) - max(self.time, self.start))
        if overlap:
            lanes = {}
            for snapshot in snapshots:
                # 空edge表示该档不给出车辆在轨道上的位置（聚合运输档），
                # 不能把它们归入同一车道，否则会算出负间距、看起来像碰撞。
                if snapshot["edge"]:
                    lanes.setdefault(snapshot["edge"], []).append(snapshot["position_m"])
            for positions in lanes.values():
                positions.sort()
                for a, b in zip(positions, positions[1:]):
                    self.minimum_gap = min(self.minimum_gap, b - a - self.length_m)
        for snapshot in snapshots:
            vid = snapshot["id"]
            before = self.previous[vid]
            if snapshot["distance_m"] < before["distance_m"] - 1e-6:
                raise RuntimeError(f"{vid}累计行驶距离回退，不能用零距离掩盖")
            if overlap and dt > 0:
                row = self.rows[vid]
                distance = max(0.0, snapshot["distance_m"] - before["distance_m"])
                row["busy_s"] += overlap * (before["phase"] not in ("idle", "parked"))
                row["parked_s"] += overlap * (before["phase"] == "parked")
                field = "loaded_distance_m" if before["lot_id"] else "empty_distance_m"
                row[field] += distance * overlap / dt
                if before["phase"] == "idle":
                    row["idle_distance_m"] += distance * overlap / dt
                reason = before["waiting_reason"] or snapshot.get("waiting_reason")
                if distance < 1e-8 and before["phase"] in ("idle", "empty", "loaded"):
                    row["blocked_s"] += overlap
                    key = (
                        "zcu_wait_s"
                        if reason and reason.startswith("zcu:")
                        else "following_wait_s"
                        if reason and reason.startswith("following:")
                        else "unclassified_wait_s"
                    )
                    row[key] += overlap
            self.previous[vid] = snapshot
        self.time = now

    def result(self) -> dict:
        totals = {
            key: sum(row[key] for row in self.rows.values())
            for key in next(iter(self.rows.values()))
        }
        exposure = len(self.rows) * (self.end - self.start)
        totals["busy_fraction"] = totals["busy_s"] / exposure
        if self.congestion_free:
            for field in (
                "blocked_s",
                "blocked_fraction",
                "zcu_wait_s",
                "following_wait_s",
                "unclassified_wait_s",
                "minimum_same_edge_gap_m",
            ):
                totals.pop(field, None)
            return {
                "fleet": totals,
                "by_vehicle": self.rows,
                "minimum_same_edge_gap_m": None,
                "congestion_metrics": "not_available_for_congestion_free_transport",
            }
        totals["blocked_fraction"] = totals["blocked_s"] / exposure
        totals["empty_distance_fraction"] = (
            totals["empty_distance_m"] / (totals["empty_distance_m"] + totals["loaded_distance_m"])
            if totals["empty_distance_m"] + totals["loaded_distance_m"]
            else None
        )
        return {
            "fleet": totals,
            "by_vehicle": self.rows,
            "minimum_same_edge_gap_m": self.minimum_gap
            if math.isfinite(self.minimum_gap)
            else None,
        }


def tardiness_metrics(definitions: dict, completions: dict, releases: dict, start, end) -> dict:
    """交期表现：只统计窗口内完工的lot，未完工的单独按已逾期计数。

    完工样本是右删失的——窗口结束时仍在制的lot不进入均值/比例，
    否则会把"还没做完"当成"准时"。母数口径写进返回值，避免只引用比例。
    """
    due = {lid: d["due_s"] for lid, d in definitions.items() if d.get("due_s") is not None}
    if not due:
        return {}
    finished = [
        lid for lid, time in completions.items() if lid in due and start < time <= end
    ]
    lags = sorted(completions[lid] - due[lid] for lid in finished)
    late = [lag for lag in lags if lag > 0]
    released_due = [
        lid
        for lid in due
        if releases.get(lid, float("inf")) <= end
        and completions.get(lid, float("inf")) > end
    ]
    return {
        "definition": "交期来自production.due_flow_factor（释放+因子×路线纯加工时间）",
        "window_completed_with_due": len(finished),
        "on_time_count": sum(lag <= 0 for lag in lags),
        "on_time_fraction": 1 - len(late) / len(lags) if lags else None,
        "completed_lag": distribution(lags),
        "late_count": len(late),
        "late_mean_lag_s": mean(late) if late else None,
        "late_max_lag_s": late[-1] if late else None,
        "censored_in_progress_count": len(released_due),
        "censored_in_progress_overdue_count": sum(end > due[lid] for lid in released_due),
    }


def window_metrics(
    config: dict, events: list[dict], traffic: dict, history: MetricHistory | None = None
) -> dict:
    start, end = (config["observation"][key] for key in ("warmup_s", "end_s"))
    definitions = {lot["id"]: lot for lot in config["lots"]}
    releases, completions, requests, pickups = {}, {}, {}, {}
    changes = []
    if history is None:
        for event in events:
            kind, time = event["kind"], event["time"]
            lid, tid = event.get("lot_id"), event.get("task_id")
            if kind == "lot.released":
                releases[lid] = time
                changes.append((time, 1))
            elif kind == "lot.completed":
                completions[lid] = time
                changes.append((time, -1))
            elif kind == "transport.requested":
                requests[tid] = time
            elif kind == "transport.pickup_start":
                pickups[tid] = time
    else:
        # SQLite统计缓存只保存释放/完工的最小投影；WIP面积与产品口径仍走原算法。
        for kind, time, lid in history.lot_events:
            if kind == "lot.released":
                releases[lid] = time
                changes.append((time, 1))
            else:
                completions[lid] = time
                changes.append((time, -1))
    level, peak, area, previous = 0, 0, 0.0, start
    for time, delta in sorted(changes):
        if time <= start:
            level += delta
            continue
        if time > end:
            break
        peak = max(peak, level)
        area += level * (time - previous)
        level += delta
        peak = max(peak, level)
        previous = time
    area += level * (end - previous)
    peak = max(peak, level)
    by_product = {}
    for route in config["routes"]:
        released = [
            lid
            for lid, time in releases.items()
            if definitions[lid]["route"] == route and time <= end
        ]
        finished = [
            lid
            for lid, time in completions.items()
            if definitions[lid]["route"] == route and start < time <= end
        ]
        wip = [lid for lid in released if lid not in completions or completions[lid] > end]
        by_product[route] = {
            "window_released_lots": sum(start < releases[lid] <= end for lid in released),
            "window_completed_lots": len(finished),
            "throughput_wafers_per_day": sum(definitions[lid]["wafer_count"] for lid in finished)
            * 86400
            / (end - start),
            "completed_cycle": distribution([completions[lid] - releases[lid] for lid in finished]),
            "end_wip_lots": len(wip),
            "end_wip_wafers": sum(definitions[lid]["wafer_count"] for lid in wip),
            "end_wip_age": distribution([end - releases[lid] for lid in wip]),
        }
    requested = [tid for tid, time in requests.items() if start < time <= end]
    uncensored = [tid for tid in requested if tid in pickups and pickups[tid] <= end]
    completed_count = sum(row["window_completed_lots"] for row in by_product.values())
    released_count = sum(row["window_released_lots"] for row in by_product.values())
    start_wip = sum(t <= start for t in releases.values()) - sum(
        t <= start for t in completions.values()
    )
    result = {
        "window_completed_lots": completed_count,
        "window_released_lots": released_count,
        "start_wip_lots": start_wip,
        "throughput_lots_per_day": completed_count * 86400 / (end - start),
        "completed_cycle": distribution(
            [time - releases[lid] for lid, time in completions.items() if start < time <= end]
        ),
        "window_cqt_violations": (
            history.cqt_count(start, end)
            if history is not None
            else sum(
                e["kind"] == "cqt.checked" and e["violated"]
                for e in events
                if start < e["time"] <= end
            )
        ),
        "window": {"start_exclusive_s": start, "end_inclusive_s": end, "duration_s": end - start},
        "by_product": by_product,
        "throughput_wafers_per_day": sum(
            row["throughput_wafers_per_day"] for row in by_product.values()
        ),
        "mean_wip_lots": area / (end - start),
        "peak_wip_lots": peak,
        "end_wip_lots": sum(row["end_wip_lots"] for row in by_product.values()),
        "transport_wait": history.transport_wait(start, end)
        if history is not None
        else {
            "definition": "运输需求提出到实际开始取货，含输出端口等待与空驶取货",
            "requested_count": len(requested),
            "uncensored": distribution([pickups[tid] - requests[tid] for tid in uncensored]),
            "right_censored_count": len(requested) - len(uncensored),
            "all_observed_lower_bound": distribution(
                [min(pickups.get(tid, end), end) - requests[tid] for tid in requested]
            ),
        },
        "traffic": traffic,
    }
    # 未声明交期的场景不写该键，既有运行的summary保持逐字节可比。
    late = tardiness_metrics(definitions, completions, releases, start, end)
    if late:
        result["tardiness"] = late
    return result
