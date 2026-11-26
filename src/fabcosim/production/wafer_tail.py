"""受限末片保留模式：不支持完整快照；旧lot_service语义保持。"""

import simpy

from fabcosim.production.machine_routines import MachineRoutine, WaferRoutine
from fabcosim.production.model import grid_time_at_or_after
from fabcosim.production.wafer_service import fallback_reason


class TerminalWaferRoutine(MachineRoutine):
    """唯一候选：前N-1片只算数值，末片保留真实WaferRoutine与AllOf。

    独立terminal_tail实验模式，不改详细全局调度。a<b时回退详细路径，
    因为前一片module2可能仍占用，不能把空模块上的末片当原尾段。
    """

    def _stage_branch(self):
        op=self.op
        m=self.machine
        if m.wafer_detail=='terminal_tail' and op['unit']=='Wafer' and fallback_reason(m,op,self.wafer_counts) is None and op['interval_s'] and m.group['CASCADINGTOOL']=='YES':
            p=op['processing_s']
            interval=min(op['interval_s'],p)
            at=m.env.now
            a=grid_time_at_or_after(at+interval,m.step)-at
            b=grid_time_at_or_after(at+(p-interval),m.step)-at
            n=sum(self.wafer_counts)
            if n==25 and a>=b and not m.module1.count and not m.module2.count:
                previous_start=at
                end=at
                last_start=at
                for _ in range(n):
                    last_start=previous_start
                    first_end=grid_time_at_or_after(previous_start+interval,m.step)
                    previous_start=max(first_end,end)
                    end=grid_time_at_or_after(previous_start+(p-interval),m.step)
                self.stage='terminal_wafer'
                m.wafer_counts['aggregated_visits']+=1
                return m.env.timeout(max(0,last_start-at))
        # 不符合唯一尾段原型前提时走真正详细路径，不能退回旧的粗整lot分支。
        if m.wafer_detail=='terminal_tail':
            if op['unit']!='Wafer':
                m.wafer_counts['untouched_nonwafer_visits']+=1
            else:
                reason=fallback_reason(m,op,self.wafer_counts)
                if reason is None:
                    reason='serial' if not op['interval_s'] or m.group['CASCADINGTOOL']!='YES' else 'a_less_than_b'
                m.wafer_counts['fallback_wafer_visits']+=1
                m.wafer_counts['fallback.'+reason]+=1
        mode=m.wafer_detail
        try:
            m.wafer_detail='micro'
            return super()._stage_branch()
        finally:
            m.wafer_detail=mode

    def _stage_terminal_wafer(self):
        self.workers=[self.machine.env.process(WaferRoutine(self.machine,self.op['interval_s'],self.sample))]
        self.stage='wafer_all'
        return simpy.AllOf(self.machine.env,self.workers)
