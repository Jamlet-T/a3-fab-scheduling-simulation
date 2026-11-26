"""最小顺序工艺；B加工必须等待目标端口实际收货。"""

import math
from collections.abc import Callable, Generator

import simpy

from fabcosim.ports.model import Lot, Port


def grid_time_at_or_after(time: float, step: float) -> float:
    """向上对齐，容差仅补偿大步号浮点表示误差，不额外增加整步。"""
    ticks = time / step
    tolerance = max(1e-9, 2 * math.ulp(ticks))
    return round(math.ceil(ticks - tolerance) * step, 9)


def grid_delay(now: float, duration: float, step: float) -> float:
    """完成时刻向上对齐到主控时间网格，避免SUMO向非整步目标超前推进。"""
    return max(0.0, grid_time_at_or_after(now + duration, step) - now)


def next_grid_time(now: float, step: float) -> float:
    """由整数步号计算下一时刻，避免长期反复累加浮点步长产生漂移。"""
    return round((round(now / step) + 1) * step, 9)


def process_lot(
    env: simpy.Environment,
    lot: Lot,
    ports: dict[str, Port],
    tools: dict[str, simpy.Resource],
    processing: dict[str, float],
    step: float,
    request_transport: Callable[[], simpy.Event],
    record: Callable[[str], None],
) -> Generator:
    with tools["A"].request() as request:
        yield request
        record("A.processing_start")
        yield env.timeout(grid_delay(env.now, processing["A"], step))
        lot.completed_operations += 1
        record("A.processing_end")
        ports["A"].put(lot, "A.tool")
        record("A.port_ready")
    yield request_transport()
    with tools["B"].request() as request:
        yield request
        received = ports["B"].take("B.tool")
        if received is not lot:
            raise RuntimeError("目标端口收到错误物料")
        record("B.processing_start")
        yield env.timeout(grid_delay(env.now, processing["B"], step))
        lot.completed_operations += 1
        record("B.processing_end")
        lot.location = "completed"
        record("lot.completed")
