"""八年时间量级下，ULP对齐不能把真实SimPy截止时刻留在过去。"""

import inspect
import math
import textwrap
from types import SimpleNamespace

import pytest
import simpy

from fabcosim.cosim import full
from fabcosim.production.model import grid_time_at_or_after, next_grid_time


@pytest.mark.parametrize("base", [8_640_000.0, 200_000_000.0, 252_448_220.0])
def test_calendar_deadline_one_ulp_after_grid_is_consumed(base):
    deadline = math.nextafter(base, math.inf)
    env = simpy.Environment(initial_time=base)
    fired = []
    event = env.timeout(deadline - base)
    event.callbacks.append(lambda _: fired.append(env.now))
    state = dict(
        math=math, next_grid_time=next_grid_time, grid_time_at_or_after=grid_time_at_or_after,
        now=base, step=0.1, config={"max_time_s": base + 1}, aggregate_transport=True,
        delay_event_wakeup=True, first_dispatch=False, assigned_last_iteration=False,
        ready=False, vehicles={}, engine=SimpleNamespace(next_arrival=lambda: None), env=env,
    )
    # 主循环现位于可保存的execute闭包，候选选择代码的行为仍须原样执行。
    source = "\n".join(line[4:] for line in inspect.getsource(full.run_full).splitlines())
    start = source.index('            next_time = min(next_grid_time(now, step)')
    end = source.index('            if (\n                stationary', start)
    exec(textwrap.dedent(source[start:end]), state)
    env.timeout(max(0, state["next_time"] - env.now))
    full.drain(env, state["next_time"])
    assert fired == [deadline], "主循环选出的边界必须能消费到期事件，否则会永久重复同一时刻"
    if deadline <= base + 1e-8:
        assert state["next_time"] == grid_time_at_or_after(deadline, 0.1)
    else:
        assert state["next_time"] == deadline
