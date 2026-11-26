"""运输派单：可派任务的排序与任务—空车匹配。

抽出策略层之前，实现固定为"等待最久的可派任务优先，再选有向距离最近的空闲车"。
两者现在都是可替换规则，配置见`policies.registry`；`choose_vehicle`保留原名，
供`demo`/`multi`小场景入口直接调用。
"""

from collections.abc import Callable, Iterable


def choose_vehicle(ids: list[str], distance: Callable[[str], float]) -> str:
    """有向距离最近的空闲车；同距离按车辆ID。"""
    return min(ids, key=lambda vehicle_id: (distance(vehicle_id), vehicle_id))


def first_idle(ids: list[str], distance: Callable[[str], float]) -> str:
    """按车辆ID取车，忽略距离；用于分离距离项的作用。"""
    return min(ids)


def farthest_idle(ids: list[str], distance: Callable[[str], float]) -> str:
    """有向距离最远的空闲车优先。

    负向对照，用于验证评测流程确实能区分好坏规则；不是可用的调度建议。
    """
    return min(ids, key=lambda vehicle_id: (-distance(vehicle_id), vehicle_id))


def earliest_ready(tasks: Iterable[dict], *, haul: Callable[[dict], float]) -> list[dict]:
    """最先可运输的任务优先；抽出策略层之前的原有行为。"""
    return sorted(tasks, key=lambda task: (task["ready_s"], task["id"]))


def earliest_request(tasks: Iterable[dict], *, haul: Callable[[dict], float]) -> list[dict]:
    """最早提出运输需求的任务优先，包含等待输出端口的时长。"""
    return sorted(tasks, key=lambda task: (task["requested_s"], task["id"]))


def shortest_haul(tasks: Iterable[dict], *, haul: Callable[[dict], float]) -> list[dict]:
    """载货路程最短的任务优先，其次最先可运输。"""
    return sorted(tasks, key=lambda task: (haul(task), task["ready_s"], task["id"]))
