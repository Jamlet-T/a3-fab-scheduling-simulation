"""目的地选择：从合法设施中选出lot的下一台加工设备。

策略只表达偏好，不改变合法性。候选集由工艺与设备组资格决定；批次成员、
端口容量、区域路权与运输约束仍由引擎执行，策略无法绕过它们。
"""

from collections.abc import Callable, Mapping, Sequence

BATCH_HOST_PENALTY_M = 1_000_000
"""同产品同批处理工序已选机台的等效距离惩罚；

远大于全厂路网尺度，实际效果是避让，但保留"候选全被占用时仍能选出一台"的
回退，所以是偏好而不是过滤。
"""


def nearest_least_loaded(
    candidates: Sequence[str],
    *,
    distance: Callable[[str], float],
    queue_lengths: Mapping[str, int],
    penalty_m: float,
    used_hosts: frozenset[str] = frozenset(),
) -> str:
    """路径距离 + 本地队列长度×队列惩罚；同值按设备名。

    这是抽出策略层之前的原有表达式，用于逐字节复现既有运行结果。
    """
    return min(
        candidates,
        key=lambda name: (
            distance(name)
            + (BATCH_HOST_PENALTY_M if name in used_hosts else 0)
            + queue_lengths[name] * penalty_m,
            name,
        ),
    )


def nearest_only(
    candidates: Sequence[str],
    *,
    distance: Callable[[str], float],
    queue_lengths: Mapping[str, int],
    penalty_m: float,
    used_hosts: frozenset[str] = frozenset(),
) -> str:
    """只看路径距离，忽略本地队列长度；用于分离队列项的作用。"""
    return min(
        candidates,
        key=lambda name: (name in used_hosts, distance(name), name),
    )


def least_loaded(
    candidates: Sequence[str],
    *,
    distance: Callable[[str], float],
    queue_lengths: Mapping[str, int],
    penalty_m: float,
    used_hosts: frozenset[str] = frozenset(),
) -> str:
    """只看本地队列长度，忽略路径距离；用于分离距离项的作用。"""
    return min(
        candidates,
        key=lambda name: (name in used_hosts, queue_lengths[name], name),
    )
