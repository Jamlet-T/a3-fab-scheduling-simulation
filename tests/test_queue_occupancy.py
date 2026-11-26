"""QueueOccupancy 的关键行为：三种真实位置变化、失败不改计数、全量核对。

只覆盖会影响队列占用正确性的边界，不逐字段测试。
"""

import pytest

from fabcosim.cosim.queue_occupancy import QueueOccupancy
from fabcosim.ports.model import Lot, Port


def test_transport_handoffs_keep_hand_computed_counts():
    """内部运输直接赋值、源端 put 离队、目标端 take 入队都按实际位置登记。"""
    occupancy = QueueOccupancy()
    lot = Lot("L1", "T1.queue")
    occupancy.transition("release_queue", lot.location)
    assert occupancy.count("T1") == 1

    # 同一设备内的内部运输：直接改 lot.location。
    previous = lot.location
    lot.location = "T2.queue"
    occupancy.transition(previous, lot.location)
    assert (occupancy.count("T1"), occupancy.count("T2")) == (0, 1)

    # 源端口 put：lot 从 T2 队列离开，进入输出端口（端口位置不是队列）。
    source = Port("P_SOURCE")
    previous = lot.location
    source.put(lot, previous)
    occupancy.transition(previous, lot.location)
    assert lot.location == "P_SOURCE"
    assert occupancy.count("T2") == 0

    # 目标端口 take：lot 经输入端口进入 T3 队列。
    target = Port("P_TARGET")
    previous = lot.location
    target.put(lot, previous)
    occupancy.transition(previous, lot.location)
    assert occupancy.count("T3") == 0
    previous = lot.location
    target.take("T3.queue")
    occupancy.transition(previous, lot.location)
    assert occupancy.count("T3") == 1
    occupancy.verify([lot])


def test_repeated_identical_location_is_not_counted_twice():
    occupancy = QueueOccupancy()
    occupancy.transition("A.tool", "A.queue")
    occupancy.transition("A.queue", "A.queue")
    assert occupancy.count("A") == 1
    occupancy.transition("A.tool", "A.tool")
    assert occupancy.count("A") == 1


def test_moving_between_queues_moves_one_count():
    occupancy = QueueOccupancy()
    occupancy.transition("A.tool", "A.queue")
    occupancy.transition("A.queue", "B.queue")
    assert occupancy.count("A") == 0
    assert occupancy.count("B") == 1


def test_leaving_queue_removes_zero_count():
    occupancy = QueueOccupancy()
    occupancy.transition("A.tool", "A.queue")
    occupancy.transition("A.queue", "A.tool")
    assert occupancy.count("A") == 0
    assert "A" not in occupancy._counts
    occupancy.verify([])


def test_underflow_raises_before_any_count_changes():
    occupancy = QueueOccupancy()
    occupancy.transition("A.tool", "A.queue")
    before = dict(occupancy._counts)

    # 旧队列没有可减的计数：不能先给新队列加一。
    with pytest.raises(RuntimeError, match="下溢"):
        occupancy.transition("B.queue", "C.queue")
    assert dict(occupancy._counts) == before

    occupancy.transition("A.queue", "A.tool")
    before = dict(occupancy._counts)
    with pytest.raises(RuntimeError, match="下溢"):
        occupancy.transition("A.queue", "C.queue")
    assert dict(occupancy._counts) == before


def test_verify_rejects_unregistered_queue_entry():
    occupancy = QueueOccupancy()
    lot = Lot("L1", "A.queue")
    with pytest.raises(RuntimeError, match="不一致"):
        occupancy.verify([lot])


def test_verify_accepts_matching_snapshot_and_rejects_drift():
    occupancy = QueueOccupancy()
    lots = [Lot("L1", "A.queue"), Lot("L2", "A.queue"), Lot("L3", "B.queue"), Lot("L4", "A.tool")]
    for lot in lots[:3]:
        occupancy.transition("A.tool", lot.location)
    occupancy.verify(lots)

    # lot 已离队但未登记：登记值比实际多一条。
    lots[0].location = "A.tool"
    with pytest.raises(RuntimeError, match="不一致"):
        occupancy.verify(lots)
