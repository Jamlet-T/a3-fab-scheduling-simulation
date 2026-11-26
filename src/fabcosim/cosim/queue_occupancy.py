"""按目标设备维护处于 ``target.queue`` 的 lot 数量。

``full.py`` 派工时目前反复扫描 active_lots 来统计队列占用；本模块用显式位置
变更登记同样的计数。它不注册位置回调、不持有 lot、不改变任何位置，也不参与
仿真事件调度。调用方在每次位置变化后传入变更前后的位置即可。
"""

from collections import Counter
from collections.abc import Iterable

from fabcosim.ports.model import Lot

_QUEUE_SUFFIX = ".queue"


class QueueOccupancy:
    """维护 target -> 处于 ``target.queue`` 的 lot 数量。"""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def transition(self, previous: str, current: str) -> None:
        """登记一次位置变化；同一位置重复赋值不重复计数。

        旧队列计数不足时，在改动任何计数之前抛 RuntimeError。
        """
        if previous == current:
            return
        old_target = self._target(previous)
        new_target = self._target(current)
        if old_target is not None and self._counts.get(old_target, 0) <= 0:
            raise RuntimeError(f"队列{old_target}.queue计数下溢：{previous} → {current}")
        if old_target is not None:
            self._add(old_target, -1)
        if new_target is not None:
            self._add(new_target, 1)

    def count(self, target: str) -> int:
        """返回目标队列占用数；从未登记过的目标为 0。"""
        return self._counts.get(target, 0)

    def verify(self, lots: Iterable[Lot]) -> None:
        """用传入 lot 的当前位置全量重建计数并对照，不一致时抛 RuntimeError。"""
        expected: Counter[str] = Counter()
        for lot in lots:
            target = self._target(lot.location)
            if target is not None:
                expected[target] += 1
        if expected != self._counts:
            raise RuntimeError(
                f"队列占用计数不一致：登记={dict(self._counts)} 实际={dict(expected)}"
            )

    @staticmethod
    def _target(location: str) -> str | None:
        if not location.endswith(_QUEUE_SUFFIX):
            return None
        return location[: -len(_QUEUE_SUFFIX)]

    def _add(self, target: str, delta: int) -> None:
        value = self._counts.get(target, 0) + delta
        if value == 0:
            del self._counts[target]
        else:
            self._counts[target] = value
