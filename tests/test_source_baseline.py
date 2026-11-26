"""源语义基线派工：最小run保护、CQT紧迫、优先级与换型时长的组合排序。"""

from fabcosim.policies.production import source_baseline


class FakeMachine:
    def __init__(self, setup, run_count, min_runs=None, durations=None):
        self.setup = setup
        self.run_count = run_count
        self._min_runs = min_runs
        self._durations = durations or {}

    def min_runs_required(self):
        return self._min_runs

    def setup_duration(self, op):
        return self._durations.get(op.get("setup"), 0.0)


def make_job(lot_id, setup, ready=0.0, priority=10, step=1, unit="Lot"):
    return {
        "lot_id": lot_id,
        "route": "R",
        "op": {"setup": setup, "source_step": step, "unit": unit},
        "ready_s": ready,
        "due_s": None,
        "priority": priority,
    }


def test_empty_queue_returns_none():
    assert source_baseline([]) is None


def test_without_context_falls_back_to_priority_then_fifo():
    queue = [make_job("a", "A", ready=0, priority=10), make_job("b", "B", ready=5, priority=20)]
    assert source_baseline(queue)["lot_id"] == "b"
    queue = [make_job("a", "A", ready=0), make_job("b", "B", ready=5)]
    assert source_baseline(queue)["lot_id"] == "a"


def test_min_run_guard_prefers_same_setup():
    machine = FakeMachine("A", run_count=1, min_runs=3)
    queue = [make_job("b", "B", ready=0), make_job("a", "A", ready=100)]
    assert source_baseline(queue, machine=machine)["lot_id"] == "a"


def test_min_run_guard_released_after_required_runs():
    machine = FakeMachine("A", run_count=3, min_runs=3)
    queue = [make_job("b", "B", ready=0), make_job("a", "A", ready=100)]
    # 已跑够最小run后不再保护，回到FIFO。
    assert source_baseline(queue, machine=machine)["lot_id"] == "b"


def test_min_run_guard_allows_setup_change_without_same_setup_job():
    machine = FakeMachine("A", run_count=0, min_runs=3)
    queue = [make_job("b", "B", ready=0), make_job("c", "C", ready=10)]
    assert source_baseline(queue, machine=machine)["lot_id"] == "b"


def test_cqt_deadline_jumps_ahead_of_fifo():
    machine = FakeMachine("A", run_count=0)
    queue = [make_job("a", "A", ready=0), make_job("b", "A", ready=100)]

    def cqt_deadline(job):
        return 50.0 if job["lot_id"] == "b" else None

    assert source_baseline(queue, machine=machine, cqt_deadline=cqt_deadline)["lot_id"] == "b"


def test_cqt_deadline_earliest_wins():
    machine = FakeMachine("A", run_count=0)
    queue = [make_job("a", "A", ready=0), make_job("b", "A", ready=1)]
    deadlines = {"a": 80.0, "b": 40.0}
    picked = source_baseline(
        queue, machine=machine, cqt_deadline=lambda job: deadlines.get(job["lot_id"])
    )
    assert picked["lot_id"] == "b"


def test_priority_precedes_setup_avoidance():
    # 源表Ranking以Lotpriority为第一层，热批即使需要换型也先加工。
    machine = FakeMachine("A", run_count=0, durations={"A": 0.0, "B": 3600.0})
    queue = [make_job("a", "A", priority=10), make_job("b", "B", priority=20)]
    assert source_baseline(queue, machine=machine)["lot_id"] == "b"


def test_setup_avoidance_precedes_fifo_for_equal_priority():
    machine = FakeMachine("A", run_count=0, durations={"A": 0.0, "B": 3600.0})
    queue = [make_job("b", "B", ready=0), make_job("a", "A", ready=100)]
    assert source_baseline(queue, machine=machine)["lot_id"] == "a"
