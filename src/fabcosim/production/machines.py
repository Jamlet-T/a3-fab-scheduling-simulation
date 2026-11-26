"""机台加工：真实wafer单位、两模块级联、机台装卸与setup。"""

from collections import Counter
from collections.abc import Callable

import simpy

from fabcosim.production.machine_routines import DowntimeRoutine, MachineRoutine
from fabcosim.production.model import grid_delay
from fabcosim.production.routine import Routine
from fabcosim.production.wafer_tail import TerminalWaferRoutine


class Machine:
    def __init__(
        self,
        env: simpy.Environment,
        name: str,
        group: dict,
        setups: list[dict],
        step: float,
        record: Callable[..., None],
        availability=None,
        *,
        wafer_detail: str = 'micro',
        tool_group: str | None = None,
        processing_distribution: str = '',
        wafer_fast_context: bool = False,
    ) -> None:
        self.env, self.name, self.group, self.setups = env, name, group, setups
        self.step, self.record = step, record
        self.wafer_detail = wafer_detail
        self.tool_group = tool_group
        self.processing_distribution = processing_distribution
        self.wafer_fast_context = wafer_fast_context
        self.wafer_counts = Counter()
        # 未启用可用性时为None，加工路径与启用前完全一致。
        self.availability = availability
        # 日历制停机期间持住闸门，使机台不接受新开工；未启用时不存在该资源。
        self.gate = simpy.Resource(env, 1) if availability is not None else None
        self.admission = simpy.Resource(env, 1)
        self.module1, self.module2 = simpy.Resource(env, 1), simpy.Resource(env, 1)
        self.setup, self.run_count = "", 0
        # 计数制PM的周期单位是wafer片数，与run_count记的job数不是一回事，分开累计。
        self.wafer_count = 0

    def delay(self, duration: float) -> simpy.Event:
        return self.env.timeout(grid_delay(self.env.now, duration, self.step))

    def min_runs_required(self) -> int | None:
        """当前setup在源表声明的最小连续run数；未声明时返回None。

        与``MachineRoutine``的``setup.run_shortfall``使用同一行源记录，
        但这里用于派工排序，不改变换型本身是否发生。
        """
        for row in self.setups:
            if row["NEW SETUP"] == self.setup and row.get("MINMAL NUMBER OF RUNS"):
                return int(row["MINMAL NUMBER OF RUNS"])
        return None

    def setup_duration(self, op: dict) -> float:
        if not op["setup"] or (op["setup_when"] == "need" and self.setup == op["setup"]):
            return 0
        if op["setup_s"] is not None:
            return op["setup_s"]
        targets = [s for s in self.setups if s["NEW SETUP"] == op["setup"]]
        exact = [s for s in targets if s["CURRENT SETUP"] in (None, "", self.setup)]
        if not targets:
            raise ValueError(f"找不到setup时间：{self.setup}→{op['setup']}")
        # 冷启动setup没有源行时，使用进入该setup的最大已定义时间，显式保守假设。
        return max(float(s["SETUP TIME"]) * 60 for s in exact or targets)

    def downtime(self) -> Routine:
        """按日历推进故障与日历制PM，与机台是否在用无关，返回可保存续算的协程。

        停机期间持住闸门，正在加工的批次不受影响（它已持有闸门），
        但停机结束后才放行下一批。事件与资源顺序见DowntimeRoutine。
        """
        return DowntimeRoutine(self)

    def process(
        self,
        op: dict,
        wafer_counts: list[int],
        sample: Callable[[], float],
        log: Callable[..., None],
    ) -> Routine:
        """返回一次加工的机台协程，仍由`env.process`驱动。

        逐事件对应原机台生成器：不多加process或timeout，进度与已持请求都在对象字段上。
        """
        routine = TerminalWaferRoutine if self.wafer_detail == 'terminal_tail' else MachineRoutine
        return routine(self, op, wafer_counts, sample, log)
