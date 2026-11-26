"""有限投片的合法批次分组：不降低源最小批量，不补造lot。"""

import math


def select_queue_batch(
    queue: list[dict],
    quantities: dict[str, int],
    now: float,
    wait_s: float,
    allow_underfilled: bool,
) -> list[dict]:
    """最早就绪的可开批组优先；欠批队头不能阻挡另一兼容组。"""
    candidates, seen = [], set()
    for first in queue:
        op = first["op"]
        key = first["route"], op["source_step"]
        if key in seen:
            continue
        seen.add(key)
        batch, wafers = [], 0
        for job in queue:
            if (job["route"], job["op"]["source_step"]) != key:
                continue
            quantity = quantities[job["lot_id"]]
            if wafers + quantity <= op["batch_max"]:
                batch.append(job)
                wafers += quantity
        if batch and (
            wafers >= op["batch_min"]
            or allow_underfilled
            and now >= first["ready_s"] + wait_s - 1e-8
        ):
            candidates.append(batch)
    return min(candidates, key=lambda jobs: min(j["ready_s"] for j in jobs)) if candidates else []


def finite_cohorts(config: dict) -> dict[tuple, tuple[str, ...]]:
    result = {}
    for route_name, route in config["routes"].items():
        lots = [lot for lot in config["lots"] if lot["route"] == route_name]
        if not lots:
            continue
        quantities = {lot["wafer_count"] for lot in lots}
        if len(quantities) != 1:
            raise ValueError("有限cohort策略当前要求同路线lot的wafer数一致")
        quantity = quantities.pop()
        for op in route["operations"]:
            if op["unit"] != "Batch":
                continue
            minimum = math.ceil(op["batch_min"] / quantity)
            maximum = op["batch_max"] // quantity
            groups = math.ceil(len(lots) / maximum)
            if minimum * groups > len(lots):
                raise ValueError(
                    f"{route_name}步骤{op['source_step']}的有限投片无法合法成批；不降低批量下限"
                )
            cursor = 0
            for index in range(groups):
                size = len(lots) // groups + int(index < len(lots) % groups)
                members = tuple(lot["id"] for lot in lots[cursor : cursor + size])
                cursor += size
                for member in members:
                    result[route_name, op["source_step"], member] = members
    return result
