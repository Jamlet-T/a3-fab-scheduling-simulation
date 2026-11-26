"""投片日历：按固定节拍合成，或按时间窗取用冻结的真实释放日历。

合成节拍用于可控实验；真实日历用于让交期、优先级与成批节奏都来自数据本身。
"""

import copy
import csv
import hashlib
from datetime import datetime
from pathlib import Path

CALENDAR_CSV = "03-数据/基准-v1/production_release.csv"
"""冻结的生产释放日历：167,129条生产批事件，2018-01-01至2025-12-31。"""


def load_calendar(config: dict, root: Path) -> dict:
    """按`release_calendar`取用冻结日历的一段，并带上真实交期与优先级。

    配置：

        "release_calendar": {"products": ["Product_1"], "start_s": 0, "duration_s": 259200}

    `start_s`/`duration_s`以日历自身首个释放时刻为原点。取用顺序固定为源行顺序，
    不循环、不复制、不补造；同一次运行只读一个时间窗，窗口外的事件不进入仿真。
    """
    window = config["release_calendar"]
    if config.get("lots") or config.get("release_stream"):
        raise ValueError("release_calendar与lots/release_stream不能同时提供")
    source = root / CALENDAR_CSV
    products = set(window["products"])
    start, duration = float(window["start_s"]), float(window["duration_s"])
    if duration <= 0:
        raise ValueError("release_calendar.duration_s必须为正")
    if not products:
        raise ValueError("release_calendar.products不能为空")
    unknown = products - {f"Product_{i}" for i in range(1, 11)}
    if unknown:
        raise ValueError(f"release_calendar.products含未知产品：{sorted(unknown)}")
    with source.open(encoding="utf-8-sig", newline="") as stream:
        rows = [
            row
            for row in csv.DictReader(stream)
            if row["PRODUCT NAME"] in products and row["START DATE"] and row["DUE DATE"]
        ]
    if not rows:
        raise ValueError("释放日历中没有匹配的产品")
    epoch = min(datetime.fromisoformat(row["START DATE"]) for row in rows)
    selected = []
    for row in rows:
        release = (datetime.fromisoformat(row["START DATE"]) - epoch).total_seconds()
        if not start <= release < start + duration:
            continue
        selected.append(
            {
                "id": row["_event_id"].replace(":", "_"),
                "route": row["ROUTE NAME"],
                "wafer_count": int(row["WAFERS PER LOT"]),
                "release_s": round(release - start, 6),
                "due_s": round(
                    (datetime.fromisoformat(row["DUE DATE"]) - epoch).total_seconds() - start, 6
                ),
                "priority": int(row["PRIORITY"]),
                "superhot": row["SUPERHOTLOT"] == "yes",
                "source_row": int(row["_source_row"]),
            }
        )
    if not selected:
        raise ValueError("该时间窗内没有释放事件；检查start_s与duration_s")
    ids = [lot["id"] for lot in selected]
    if len(set(ids)) != len(ids):
        raise ValueError("释放日历事件ID重复，不能重复投片")
    routes = {lot["route"] for lot in selected}
    missing = routes - set(config["production"]["routes"])
    if missing:
        raise ValueError(f"释放日历用到的路线未在production.routes中编译：{sorted(missing)}")
    config["lots"] = sorted(selected, key=lambda lot: (lot["release_s"], lot["id"]))
    config["production"]["routes"] = sorted(routes)
    config["source_calendar"] = {
        "path": CALENDAR_CSV,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "products": sorted(products),
        "epoch": epoch.isoformat(),
        "window": {"start_s": start, "duration_s": duration},
        "selected_lots": len(selected),
    }
    return config


def prepare_releases(settings: dict) -> dict:
    config = copy.deepcopy(settings)
    if "release_calendar" in config and (config.get("lots") or config.get("release_stream")):
        raise ValueError("release_calendar与lots/release_stream不能同时提供，投片来源必须唯一")
    observation = config.get("observation")
    if observation is not None:
        end = float(observation["end_s"])
        warmup = float(observation["warmup_s"])
        step = config["step_s"]
        if not 0 <= warmup < end or any(
            abs(t / step - round(t / step)) > 1e-6 for t in (warmup, end)
        ):
            raise ValueError("观测区间必须有效，并对齐仿真时间网格")
        config["max_time_s"] = end
    stream = config.get("release_stream")
    if stream is None:
        return config
    if config.get("lots"):
        raise ValueError("持续投片配置不同时提供lots；初始投片由bootstrap_lots_per_route声明")
    if observation is None:
        raise ValueError("持续投片需要明确的observation预热期和结束时刻")
    end = float(observation["end_s"])
    warmup = float(observation["warmup_s"])
    interval = float(stream["interval_s"])
    start = float(stream.get("start_s", interval))
    release_end = float(stream.get("end_s", end))
    pattern = stream["route_pattern"]
    if not pattern or interval <= 0 or not 0 <= warmup < end or not 0 <= start < release_end <= end:
        raise ValueError("投片节拍、路线模式或观测区间无效")
    quantity = int(stream.get("wafer_count", 25))
    bootstrap = int(stream.get("bootstrap_lots_per_route", 0))
    if quantity <= 0 or bootstrap < 0:
        raise ValueError("wafer数量必须为正，初始投片数量不能为负")
    counts = dict.fromkeys(pattern, 0)
    lots = []

    def append(route: str, time: float) -> None:
        index = counts[route]
        counts[route] += 1
        lots.append(
            {
                "id": f"{route}-{index:05d}",
                "route": route,
                "wafer_count": quantity,
                "release_s": round(time, 9),
            }
        )

    for route in counts:
        for _ in range(bootstrap):
            append(route, 0)
    index = 0
    while start + index * interval < release_end - 1e-8:
        append(pattern[index % len(pattern)], start + index * interval)
        index += 1
    config["lots"] = sorted(lots, key=lambda lot: (lot["release_s"], lot["id"]))
    config["production"]["routes"] = list(counts)
    config["production"]["batch_policy"] = "queue_batch"
    config["production"]["allow_underfilled_after_timeout"] = False
    config["max_time_s"] = end
    return config


def assign_due_dates(config: dict) -> None:
    """按声明的流动因子为lot补交期；已显式给出`due_s`的lot保持不变。

    `production.due_flow_factor`为缺省时不补交期，交期相关的规则与延期指标随之不可用。
    因子依据：冻结日历中各路线`(DUE DATE - START DATE)/RPT`的中位比值稳定在2.89—3.11，
    即数据自身把交期约定为释放后约3倍纯加工时间。见开发记录。
    """
    factor = config["production"].get("due_flow_factor")
    if factor is None:
        return
    if factor <= 0:
        raise ValueError("due_flow_factor必须为正")
    for lot in config["lots"]:
        lot.setdefault(
            "due_s",
            round(lot["release_s"] + factor * config["routes"][lot["route"]]["rpt_s"], 9),
        )
