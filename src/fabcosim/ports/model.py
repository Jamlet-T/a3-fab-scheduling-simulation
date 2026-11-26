"""端口只保存实际交接的物料；候选派工不改变物料位置。"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Lot:
    id: str = "lot0"
    location: str = "A.tool"
    completed_operations: int = 0
    _location_callback: Callable[["Lot"], None] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __setattr__(self, name: str, value: Any) -> None:
        """位置变更回调只用于增量审计标脏；无回调时行为与原赋值完全一致。"""
        if name != "location":
            object.__setattr__(self, name, value)
            return
        previous = self.__dict__.get("location")
        object.__setattr__(self, name, value)
        callback = getattr(self, "_location_callback", None)
        if callback is not None and previous != value:
            callback(self)


@dataclass
class Port:
    id: str
    lot: Lot | None = None
    reserved_by: str | None = None
    _lot_callback: Callable[["Port"], None] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __setattr__(self, name: str, value: Any) -> None:
        """物料指针变更回调只用于增量审计标脏；无回调时行为与原赋值完全一致。"""
        if name != "lot":
            object.__setattr__(self, name, value)
            return
        previous = self.__dict__.get("lot")
        object.__setattr__(self, name, value)
        callback = getattr(self, "_lot_callback", None)
        # 用is not比较：两个字段完全相等的Lot对象也是真实的不同持有者。
        if callback is not None and previous is not value:
            callback(self)

    def reserve(self, task_id: str) -> bool:
        if self.lot is not None or self.reserved_by is not None:
            return False
        self.reserved_by = task_id
        return True

    def release(self, task_id: str) -> None:
        if self.reserved_by != task_id:
            raise RuntimeError(f"端口{self.id}预留归属错误")
        self.reserved_by = None

    def put(self, lot: Lot, origin: str) -> None:
        if self.lot is not None or lot.location != origin:
            raise RuntimeError(f"端口{self.id}交接冲突：{lot.location} → {origin}")
        self.lot = lot
        lot.location = self.id

    def take(self, destination: str) -> Lot:
        if self.lot is None or self.lot.location != self.id:
            raise RuntimeError(f"端口{self.id}没有可交接物料")
        lot = self.lot
        self.lot = None
        lot.location = destination
        return lot
