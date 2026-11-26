"""加工单位、级联双模块与setup时序的人工可计算案例。"""

import simpy

from fabcosim.production.machines import Machine


def machine(env, cascading=False, setups=None):
    group = {"CASCADINGTOOL": "YES" if cascading else None, "LOADINGTIME": 0, "UNLOADINGTIME": 0}
    return Machine(env, "m", group, setups or [], 0.1, lambda *a, **k: None)


def operation(unit="Lot", interval=0, setup="", setup_s=None):
    return {
        "unit": unit,
        "interval_s": interval,
        "setup": setup,
        "setup_when": "need",
        "setup_s": setup_s,
    }


def test_wafer_and_batch_units_have_different_duration():
    def duration(unit):
        env = simpy.Environment()
        env.process(machine(env).process(operation(unit), [2, 3], lambda: 4, lambda *a, **k: None))
        env.run()
        return env.now

    assert duration("Wafer") == 20
    assert duration("Batch") == 4


def test_wafer_cascade_finishes_last_wafer_not_first_slot():
    env = simpy.Environment()
    env.process(
        machine(env, True).process(operation("Wafer", 3), [5], lambda: 4, lambda *a, **k: None)
    )
    env.run()
    assert env.now == 4 + 4 * 3


def test_lot_cascade_waits_for_second_module():
    env = simpy.Environment()
    m = machine(env, True)
    events = []

    def start(name, duration, interval):
        def log(kind, **fields):
            events.append((env.now, kind, name, fields))

        yield env.process(m.process(operation("Lot", interval), [25], lambda: duration, log))

    a = env.process(start("a", 10, 7))
    b = env.process(start("b", 4, 2))
    env.run()
    ends = {name: time for time, kind, name, _ in events if kind == "processing.end"}
    assert a.triggered and b.triggered and ends == {"a": 10, "b": 12}
    active = {}
    for _, kind, name, fields in events:
        if kind == "module.enter":
            assert fields["module"] not in active
            active[fields["module"]] = name
        elif kind == "module.exit":
            assert active.pop(fields["module"]) == name
    assert active == {}


def test_setup_matrix_and_need_only_on_change():
    env = simpy.Environment()
    setups = [{"CURRENT SETUP": "old", "NEW SETUP": "new", "SETUP TIME": 2}]
    m = machine(env, setups=setups)
    m.setup = "old"

    def run():
        for _ in range(2):
            yield env.process(
                m.process(operation(setup="new"), [25], lambda: 5, lambda *a, **k: None)
            )

    env.process(run())
    env.run()
    assert env.now == 120 + 5 + 5
