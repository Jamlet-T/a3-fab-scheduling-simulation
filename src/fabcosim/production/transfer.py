"""生产输入缓冲：生产输入queue与在途预留容量。

本模块只表示**生产输入queue + 在途预留容量**：

- 一台"设备"是一份按名字登记的输入缓冲（例如某步骤前的入料位），容量单位为lot；
- `reserve`表示lot已进入该缓冲、占用一个位置，直到`release`；
- 容量不足时lot挂在simpy.Resource队列上等待，不占容量。

它**不表示**端口、车辆或加工机台：端口交接容量归`ports/`，车辆归`transport/`，
机台加工归`machines.py`。主流程用`resources`与`tokens`审计实际占用。
"""

from collections.abc import Iterable

import simpy

from fabcosim.production.pure_routines import ReserveRoutine


class ProductionTransferBuffers:
    """每台设备一个simpy.Resource；tokens记录lot当前持有的准入。

    `tokens`的值为`(设备名, 已授予的Request)`：只有获准后才记账，
    因此`verify()`能把每个token与对应Resource的`users`逐项对照。
    """

    def __init__(
        self, env: simpy.Environment, names: Iterable[str], capacity: int
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("生产输入容量必须为正整数")
        names = list(names)
        if len(set(names)) != len(names):
            raise ValueError("生产输入设备名不得重复")
        self.env = env
        self.capacity = capacity
        self.resources = {name: simpy.Resource(env, capacity) for name in names}
        self.tokens: dict[str, tuple[str, simpy.Request]] = {}
        # 已登记但尚未获准的lot；防止同一lot并发reserve时静默覆盖。
        self._pending: set[str] = set()

    def reserve(
        self, lot_id: str, target: str
    ) -> ReserveRoutine:
        """申请`target`的一个输入位；获准后把token记入`tokens`。

        调用方在lot进程内`yield from`本方法，获准后才继续加工。
        同一lot已有未释放的预留（等待中或已获准）时直接报错，不覆盖。
        """
        if lot_id in self.tokens or lot_id in self._pending:
            raise ValueError(f"lot {lot_id} 已有未释放的生产输入预留")
        if target not in self.resources:
            raise ValueError(f"未知的生产输入设备：{target}")
        self._pending.add(lot_id)
        return ReserveRoutine(self, lot_id, target)

    def release(self, lot_id: str) -> None:
        """释放lot持有的输入位；该lot没有token时不做任何操作。"""
        token = self.tokens.pop(lot_id, None)
        if token is None:
            return
        target, request = token
        self.resources[target].release(request)

    def committed_lots(self, target: str) -> int:
        """目标已承诺的输入工作量：在途/到站占位与等待准入各计一次。

        已到站加工队列包含在users中，不能再叠加job队列而重复计数。
        Resource.request立即登记users/queue，故同刻后续选择也能看到刚发出的申请。
        这只是目的地规则的负载信号，不增加输入容量或改变准入顺序。
        """
        resource = self.resources[target]
        return len(resource.users) + len(resource.queue)

    def verify(self) -> bool:
        """校验tokens与各Resource.users一致，且每台设备未超容量。"""
        held: dict[str, set[simpy.Request]] = {name: set() for name in self.resources}
        for target, request in self.tokens.values():
            if target not in self.resources or request in held[target]:
                return False
            held[target].add(request)
        for name, resource in self.resources.items():
            if len(held[name]) > resource.capacity:
                return False
            if set(resource.users) != held[name]:
                return False
        return True
