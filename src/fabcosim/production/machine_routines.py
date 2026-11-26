"""机台协程：可保存续算的加工与停机流程，仍由SimPy Process驱动。

旧实现把机台流程写成Python生成器，执行点藏在生成器帧里，无法做快照续算。这里把**同一份**
事件与资源顺序写成显式阶段机：每个``send``恰好返回旧生成器当时yield的那一个事件，
``request``/``release``发生的时刻与先后和旧``with``块完全一致（不多加process或timeout），
进度、中间量（setup时长、加工时间、已建子进程）与已持有的请求都放在对象字段上，
主控因此可以在两次``send``之间保存并在另一进程里继续算。
"""

import simpy

from fabcosim.production.routine import Routine
from fabcosim.production.wafer_service import fallback_reason, wafer_service_end


class StageRoutine(Routine):
    """显式阶段机基类：``send``推进到下一个yield，状态全部在字段中。

    ``stage``保存“下一次send该执行哪一步”，等待事件期间它也指向恢复点，可直接进快照。
    """

    def __init__(self) -> None:
        self.stage = "start"
        # 已创建、尚未释放的请求，按取得顺序排列；异常收尾按逆序释放（与with块嵌套一致）。
        self.held: list[simpy.resources.resource.Request] = []
        # SimPy的Process.name读的是生成器的__name__；这里给普通对象补上，便于日志与排查。
        self.__name__ = type(self).__name__

    # -- generator协议 ---------------------------------------------------
    def send(self, value=None) -> simpy.Event:
        try:
            while self.stage != "done":
                event = getattr(self, "_stage_" + self.stage)()
                if event is not None:
                    return event
        except BaseException:
            # 机台内部异常（例如找不到setup时间）在旧实现里会沿with块展开并释放资源，
            # 这里做同样的事：先按逆序归还已持请求，再把原异常抛给SimPy。
            self._release_all()
            self.stage = "done"
            raise
        raise StopIteration

    def throw(self, error, value=None, traceback=None):
        """异常穿出旧with块时逐层退出；这里先按逆序释放全部已持请求，再原样抛出。

        抛出后标为结束：旧生成器帧已随异常销毁，再send只能是StopIteration。
        """
        self._release_all()
        self.stage = "done"
        return super().throw(error, value, traceback)

    def close(self) -> None:
        """生成器清理路径：释放已持请求，避免退出后机台资源被永久占住。"""
        self._release_all()
        self.stage = "done"

    # -- 资源请求与释放（语义等同``with resource.request() as request``）----
    def _request(self, resource: simpy.Resource) -> simpy.resources.resource.Request:
        request = resource.request()
        self.held.append(request)
        return request

    def _release(self, request: simpy.resources.resource.Request) -> None:
        for index in range(len(self.held) - 1, -1, -1):
            if self.held[index] is request:
                del self.held[index]
                break
        else:
            raise RuntimeError(f"{type(self).__name__}未持有该请求，不能释放")
        if not request.triggered:
            # 与Request.__exit__一致：还没授予的排队请求先撤销，再release（此时是no-op）。
            request.cancel()
        request.resource.release(request)

    def _release_all(self) -> None:
        while self.held:
            self._release(self.held[-1])


class DowntimeRoutine(StageRoutine):
    """日历制停机：按日历推进故障与日历制PM，与机台是否在用无关。

    停机期间持住机台闸门：正在加工的批次不受影响（它已持有闸门），
    停机结束后才放行下一批。
    """

    def __init__(self, machine) -> None:
        super().__init__()
        self.machine = machine
        self.gate_request: simpy.resources.resource.Request | None = None
        self.event: str | None = None
        self.stage = "start"

    def _stage_start(self) -> simpy.Event | None:
        availability = self.machine.availability
        at = availability.next_calendar_at()
        if at is None:
            self.stage = "done"
            return None
        self.stage = "wait"
        # 与旧实现同一表达式：这里不对齐时间网格，逾期时是零时长timeout。
        return self.machine.env.timeout(max(0.0, at - self.machine.env.now))

    def _stage_wait(self) -> simpy.Event:
        self.gate_request = self._request(self.machine.gate)
        self.stage = "gate"
        return self.gate_request

    def _stage_gate(self) -> simpy.Event:
        event, duration = self.machine.availability.take_calendar(self.machine.env.now)
        self.event = event
        self.machine.record(
            "availability.down",
            equipment=self.machine.name,
            event=event,
            duration_s=round(duration, 9),
        )
        self.stage = "down"
        return self.machine.delay(duration)

    def _stage_down(self) -> None:
        self.machine.record("availability.up", equipment=self.machine.name, event=self.event)
        self._release(self.gate_request)  # 旧实现：退出gate的with块后才算下一段日历
        self.gate_request = None
        self.stage = "start"
        return None


class WaferRoutine(StageRoutine):
    """级联机台的逐wafer子进程：先占module1一段interval，再占module2完成剩余加工。

    由``MachineRoutine``用``env.process``建成子进程，与旧实现一样等``AllOf``全部完成。
    构造时不得抽样：旧生成器体在第一次send（SimPy的Initialize）才执行``sample()``。
    """

    def __init__(self, machine, interval_s: float, sample) -> None:
        super().__init__()
        self.machine = machine
        self.interval_s = interval_s
        self.sample = sample
        self.module1_request: simpy.resources.resource.Request | None = None
        self.module2_request: simpy.resources.resource.Request | None = None
        self.process_time: float | None = None
        self.interval: float | None = None

    def _stage_start(self) -> simpy.Event:
        self.process_time = self.sample()
        self.interval = min(self.interval_s, self.process_time)
        self.module1_request = self._request(self.machine.module1)
        self.stage = "module1"
        return self.module1_request

    def _stage_module1(self) -> simpy.Event:
        self.stage = "first_delay"
        return self.machine.delay(self.interval)

    def _stage_first_delay(self) -> simpy.Event:
        self.module2_request = self._request(self.machine.module2)
        self.stage = "module2"
        return self.module2_request

    def _stage_module2(self) -> simpy.Event:
        self._release(self.module1_request)  # 旧实现：退出module1的with块
        self.module1_request = None
        self.stage = "remaining"
        return self.machine.delay(self.process_time - self.interval)

    def _stage_remaining(self) -> None:
        self._release(self.module2_request)
        self.module2_request = None
        self.stage = "done"
        return None


class MachineRoutine(StageRoutine):
    """一台机台的一次加工：准入、计数制PM、setup屏障、装卸、按单位加工与计数。

    阶段顺序逐条对应旧``Machine._process``的yield点；``log``是工序日志，
    ``machine.record``是停机记录，两者用途不同，不能互换。
    """

    def __init__(self, machine, op: dict, wafer_counts: list[int], sample, log) -> None:
        super().__init__()
        self.machine = machine
        self.op = op
        self.wafer_counts = wafer_counts
        self.sample = sample
        self.log = log
        # 旧生成器在第一次send时算这个值；它只依赖op与机台组属性，无副作用。
        self.cascading_lot = bool(
            op["unit"] == "Lot" and op["interval_s"] and machine.group["CASCADINGTOOL"] == "YES"
        )
        self.gate_request: simpy.resources.resource.Request | None = None
        self.admission_request: simpy.resources.resource.Request | None = None
        self.barrier_request: simpy.resources.resource.Request | None = None
        self.module1_request: simpy.resources.resource.Request | None = None
        self.module2_request: simpy.resources.resource.Request | None = None
        self.outage_event: str | None = None
        self.setup_seconds: float | None = None
        self.process_time: float | None = None
        self.interval: float | None = None
        self.workers: list[simpy.Process] | None = None
        self.wafer_index = 0
        self.wafer_total = 0
        self.stage = "gate" if machine.gate is not None else "admission"

    # -- 开工前：闸门、准入、计数制PM、setup ------------------------------
    def _stage_gate(self) -> simpy.Event:
        self.gate_request = self._request(self.machine.gate)
        self.stage = "admission"
        return self.gate_request

    def _stage_admission(self) -> simpy.Event:
        self.admission_request = self._request(self.machine.admission)
        self.stage = "counter_pm"
        return self.admission_request

    def _stage_counter_pm(self) -> simpy.Event | None:
        """计数制PM按已加工片数到期，在开工前执行：持着准入，不打断在制批次。"""
        availability = self.machine.availability
        outage = None
        if availability is not None:
            outage = availability.counter_due(self.machine.wafer_count)
        if outage is None:
            self.stage = "setup"
            return None
        event, duration = outage
        self.outage_event = event
        self.machine.record(
            "availability.down",
            equipment=self.machine.name,
            event=event,
            duration_s=round(duration, 9),
        )
        self.stage = "counter_pm_up"
        return self.machine.delay(duration)

    def _stage_counter_pm_up(self) -> None:
        self.machine.record("availability.up", equipment=self.machine.name, event=self.outage_event)
        self.outage_event = None
        self.stage = "setup"
        return None

    def _stage_setup(self) -> simpy.Event | None:
        machine = self.machine
        self.setup_seconds = machine.setup_duration(self.op)
        if not self.setup_seconds:
            self.stage = "load_start"
            return None
        old = next(
            (
                s
                for s in machine.setups
                if s["NEW SETUP"] == machine.setup and s.get("MINMAL NUMBER OF RUNS")
            ),
            None,
        )
        if old and machine.run_count < int(old["MINMAL NUMBER OF RUNS"]):
            self.log(
                "setup.run_shortfall",
                equipment=machine.name,
                actual=machine.run_count,
                required=int(old["MINMAL NUMBER OF RUNS"]),
            )
        # setup在第二模块上做，期间仍占准入；屏障与旧实现一样只取一次。
        self.barrier_request = self._request(machine.module2)
        self.stage = "setup_start"
        return self.barrier_request

    def _stage_setup_start(self) -> simpy.Event:
        self.log(
            "setup.start",
            equipment=self.machine.name,
            from_setup=self.machine.setup,
            to_setup=self.op["setup"],
        )
        self.stage = "setup_end"
        return self.machine.delay(self.setup_seconds)

    def _stage_setup_end(self) -> None:
        self.machine.setup, self.machine.run_count = self.op["setup"], 0
        self.log("setup.end", equipment=self.machine.name, to_setup=self.machine.setup)
        self._release(self.barrier_request)  # 旧实现：退出module2屏障的with块
        self.barrier_request = None
        self.setup_seconds = None
        self.stage = "load_start"
        return None

    # -- 装卸与加工 ------------------------------------------------------
    def _stage_load_start(self) -> simpy.Event:
        self.log("tool.load_start", equipment=self.machine.name)
        self.stage = "load_end"
        return self.machine.delay(float(self.machine.group["LOADINGTIME"] or 0) * 60)

    def _stage_load_end(self) -> None:
        self.log("tool.load_end", equipment=self.machine.name)
        self.log("processing.start", equipment=self.machine.name)
        self.stage = "branch"
        return None

    def _stage_branch(self) -> simpy.Event | None:
        machine = self.machine
        if machine.wafer_detail == 'lot_service':
            if self.op['unit'] != 'Wafer':
                machine.wafer_counts['untouched_nonwafer_visits'] += 1
            else:
                reason = fallback_reason(machine, self.op, self.wafer_counts)
                if reason is None:
                    end = wafer_service_end(
                        machine.env.now, self.op['processing_s'], self.op['interval_s'] or 0,
                        25, machine.step,
                        bool(self.op['interval_s']) and machine.group['CASCADINGTOOL'] == 'YES',
                    )
                    machine.wafer_counts['aggregated_visits'] += 1
                    self.stage = 'processing_done'
                    return machine.env.timeout(max(0, end - machine.env.now))
                machine.wafer_counts['fallback_wafer_visits'] += 1
                machine.wafer_counts['fallback.' + reason] += 1
        if self.cascading_lot:
            # Lot级联：整批先占module1一段，再占module2做剩余部分。
            self.process_time = self.sample()
            self.interval = min(self.op["interval_s"], self.process_time)
            self.module1_request = self._request(machine.module1)
            self.stage = "lot_module1"
            return self.module1_request
        if self.op["unit"] == "Wafer":
            if self.op["interval_s"] and machine.group["CASCADINGTOOL"] == "YES":
                # 逐wafer子进程：先全部建进程（首个send才采样），再等AllOf全部完成。
                self.workers = [
                    machine.env.process(WaferRoutine(machine, self.op["interval_s"], self.sample))
                    for _ in range(sum(self.wafer_counts))
                ]
                self.stage = "wafer_all"
                return simpy.AllOf(machine.env, self.workers)
            self.wafer_index = 0
            self.wafer_total = sum(self.wafer_counts)
            self.stage = "wafer_serial"
            return None
        # Batch仅采样一次，不乘lot数；Lot也仅采样一次。
        self.stage = "processing_done"
        return machine.delay(self.sample())

    def _stage_wafer_serial(self) -> simpy.Event | None:
        if self.wafer_index < self.wafer_total:
            self.wafer_index += 1
            return self.machine.delay(self.sample())
        self.stage = "processing_done"
        return None

    def _stage_wafer_all(self) -> None:
        self.workers = None  # 子进程已结束，快照不必再保存它们
        self.stage = "processing_done"
        return None

    def _stage_lot_module1(self) -> simpy.Event:
        self.log("module.enter", equipment=self.machine.name, module=1)
        self.stage = "lot_interval"
        return self.machine.delay(self.interval)

    def _stage_lot_interval(self) -> simpy.Event:
        self.module2_request = self._request(self.machine.module2)
        self.stage = "lot_module2"
        return self.module2_request

    def _stage_lot_module2(self) -> simpy.Event:
        self.log("module.exit", equipment=self.machine.name, module=1)
        self.log("module.enter", equipment=self.machine.name, module=2)
        self._release(self.module1_request)  # 旧实现：退出module1的with块
        self.module1_request = None
        # 旧实现里Lot级联的准入在module2开工后立即释放，随后才等剩余加工时间。
        self._release(self.admission_request)
        self.admission_request = None
        self.stage = "lot_remaining"
        return self.machine.delay(self.process_time - self.interval)

    def _stage_lot_remaining(self) -> simpy.Event:
        self.log("processing.end", equipment=self.machine.name)
        self.log("tool.unload_start", equipment=self.machine.name)
        self.stage = "lot_unload"
        return self.machine.delay(float(self.machine.group["UNLOADINGTIME"] or 0) * 60)

    def _stage_lot_unload(self) -> None:
        self.log("tool.unload_end", equipment=self.machine.name)
        self.log("module.exit", equipment=self.machine.name, module=2)
        self._release(self.module2_request)  # 旧实现：module2最后释放
        self.module2_request = None
        self.machine.run_count += 1
        self.machine.wafer_count += sum(self.wafer_counts)
        self.stage = "finish"
        return None

    def _stage_processing_done(self) -> simpy.Event:
        self.log("processing.end", equipment=self.machine.name)
        self.log("tool.unload_start", equipment=self.machine.name)
        self.stage = "unload"
        return self.machine.delay(float(self.machine.group["UNLOADINGTIME"] or 0) * 60)

    def _stage_unload(self) -> None:
        self.log("tool.unload_end", equipment=self.machine.name)
        self.machine.run_count += len(self.wafer_counts)
        self.machine.wafer_count += sum(self.wafer_counts)
        self._release(self.admission_request)  # 旧实现：退出admission的with块
        self.admission_request = None
        self.stage = "finish"
        return None

    def _stage_finish(self) -> None:
        if self.gate_request is not None:
            self._release(self.gate_request)
            self.gate_request = None
        self.stage = "done"
        return None
