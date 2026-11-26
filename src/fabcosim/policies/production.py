"""生产派工：机台本地队列先加工谁，以及合法开批组选哪一组。

派工只决定顺序。批次成员是否符合源批量上下限、设备资格、setup与CQT约束
仍由引擎执行，策略不能把不合法的组变成合法。

规则签名统一为`(queue, *, now, remaining)`：`now`为当前仿真时刻，`remaining(job)`
给出该job剩余工序的名义加工时间合计。`remaining`是名义值估计——它按冻结路线的
源均值逐工序求和，不含抽检跳过、返工重访与实际setup，只用于比较相对紧迫度。

队列内重排依赖一个数据事实：一个机台只服务一个设备组，而源工艺中每个设备组
只出现一种加工单位（Lot/Wafer/Batch），因此同一队列内的job加工单位一致。
"""

from collections.abc import Callable


def fifo(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """先到先加工；抽出策略层之前的原有行为。"""
    return queue[0] if queue else None


def implant_setup_fifo(
    queue: list[dict], *, now: float, remaining=None,
    tool_group: str, setup_duration: Callable[[dict], float],
) -> dict | None:
    """仅两植入组：等待满24h的最老job优先，否则实际换型时长最短；同值保持队列顺序。

    不主动等待凑同型，不改变目的地或合法批量。24h从到站入本台队列算起，
    只在派工时保护已有候选，不保证准入前或厂级等待有上界。
    """
    if not queue:
        return None
    if tool_group not in ("Implant_128", "Implant_132"):
        return queue[0]
    return _setup_with_age(queue, now=now, setup_duration=setup_duration)


def implant_setup_fifo_3groups(
    queue: list[dict], *, now: float, remaining=None,
    tool_group: str, setup_duration: Callable[[dict], float],
) -> dict | None:
    """独立三组候选：仅增加Implant_91；排序、24h保护及Batch回退复用旧规则。"""
    if not queue:
        return None
    if tool_group not in ("Implant_128", "Implant_132", "Implant_91"):
        return queue[0]
    return _setup_with_age(queue, now=now, setup_duration=setup_duration)


def _setup_with_age(
    queue: list[dict], *, now: float, setup_duration: Callable[[dict], float],
) -> dict:
    """调用者已确认非空与作用组；保持旧二组的选择顺序与相同值稳定性。"""
    # 本轮只重排非Batch；不以job选择绕过后续合法开批组选择。
    if any(job["op"]["unit"] == "Batch" for job in queue):
        return queue[0]
    aged = [job for job in queue if now - job["ready_s"] >= 86400]
    if aged:
        return min(aged, key=lambda job: job["ready_s"])
    return min(queue, key=lambda job: setup_duration(job["op"]))


def source_baseline(
    queue: list[dict],
    *,
    now: float | None = None,
    remaining=None,
    tool_group: str | None = None,
    machine=None,
    cqt_deadline: Callable[[dict], float | None] | None = None,
) -> dict | None:
    """SMT2020源控制语义的复合排序基线，用于替换"裸FIFO"基座。

    排序键依次为：

    1. **最小连续run保护**：机台当前setup未跑够源表``MINMAL NUMBER OF RUNS``时，
       同setup作业整体排在换型作业之前。与源``Setupavoidancerule``一致的是"优先不换型"，
       但不硬阻塞：队列里没有同setup作业时仍允许换型，避免机台空转。
    2. **CQT剩余**：挂有未闭CQT窗口的作业优先，按截止时刻升序。
    3. **优先级**：源表10普通/20热批/30超热批，数值大者优先。
    4. **换型时长**：无需换型或换型更短的作业优先。
    5. **FIFO**：到站时刻、lot ID。

    第2—4项的顺序遵循源表Ranking（优先级、最小setup、CR）并把CQT作为硬约束前置；
    PySCFabSim把setup置于优先级之前，与本项目读到的源表字段不一致，这里以源表为准。
    """
    if not queue:
        return None

    current = getattr(machine, "setup", "") if machine is not None else ""
    run_count = getattr(machine, "run_count", 0) if machine is not None else 0
    required = machine.min_runs_required() if machine is not None else None
    holding = required is not None and run_count < required

    def key(job: dict):
        op = job["op"]
        # 无setup的工序本来就不换型，与当前setup一致时同样受最小run保护。
        same_setup = not op.get("setup") or (bool(current) and op.get("setup") == current)
        guard = 1 if holding and not same_setup else 0
        deadline = cqt_deadline(job) if cqt_deadline is not None else None
        cqt_rank = (0, deadline) if deadline is not None else (1, 0.0)
        priority = job.get("priority")
        priority_rank = -priority if priority is not None else 0
        setup_s = machine.setup_duration(op) if machine is not None else 0.0
        return (
            guard,
            cqt_rank[0],
            cqt_rank[1],
            priority_rank,
            setup_s,
            job["ready_s"],
            job["lot_id"],
        )

    return min(queue, key=key)


def spt(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """最短源加工时间优先；同值按就绪时刻、lot ID。"""
    if not queue:
        return None
    return min(
        queue, key=lambda job: (job["op"]["processing_s"], job["ready_s"], job["lot_id"])
    )


def lpt(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """最长源加工时间优先；作为SPT的反向对照。"""
    if not queue:
        return None
    return min(
        queue, key=lambda job: (-job["op"]["processing_s"], job["ready_s"], job["lot_id"])
    )


def _require_due(queue: list[dict], rule: str) -> None:
    if any(job.get("due_s") is None for job in queue):
        raise ValueError(
            f"{rule}需要lot交期：请在production中设置due_flow_factor，或为每个lot提供due_s"
        )


def edd(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """最早交期优先；同交期按就绪时刻、lot ID。"""
    if not queue:
        return None
    _require_due(queue, "edd")
    return min(queue, key=lambda job: (job["due_s"], job["ready_s"], job["lot_id"]))


def cr(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """临界比最小者优先：CR = (交期 - 当前时刻) / 剩余名义加工时间。

    CR小于1表示按名义加工时间已经来不及。分母取下限避免除零。
    """
    if not queue:
        return None
    _require_due(queue, "cr")
    if now is None or remaining is None:
        raise ValueError("cr需要now与remaining上下文")
    return min(
        queue,
        key=lambda job: (
            (job["due_s"] - now) / max(remaining(job), 1e-9),
            job["ready_s"],
            job["lot_id"],
        ),
    )


def _require_priority(queue: list[dict], rule: str) -> None:
    if any(job.get("priority") is None for job in queue):
        raise ValueError(
            f"{rule}需要lot优先级：请用release_calendar投片（带源表PRIORITY），"
            "或为每个lot提供priority"
        )


def priority(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """优先级高者先加工（源表10=普通、20=热批、30=超热批）；同优先级按就绪时刻、lot ID。"""
    if not queue:
        return None
    _require_priority(queue, "priority")
    return min(
        queue,
        key=lambda job: (-job["priority"], job["ready_s"], job["lot_id"]),
    )


def priority_edd(queue: list[dict], *, now: float | None = None, remaining=None) -> dict | None:
    """先按优先级、再按交期；同级别内最早交期优先。**同时需要优先级与交期。**"""
    if not queue:
        return None
    _require_priority(queue, "priority_edd")
    _require_due(queue, "priority_edd")
    return min(
        queue,
        key=lambda job: (-job["priority"], job["due_s"], job["ready_s"], job["lot_id"]),
    )


def earliest_ready(groups: list[list[dict]], *, now=None, remaining=None) -> list[dict] | None:
    """最早有成员就绪的合法开批组优先。

    抽出策略层之前的原有行为，不设次级键；开批组成员本身由cohort约束决定。
    """
    if not groups:
        return None
    return min(groups, key=lambda jobs: min(job["ready_s"] for job in jobs))


def largest_first(groups: list[list[dict]], *, now=None, remaining=None) -> list[dict] | None:
    """成员最多的合法开批组优先，其次最早有成员就绪。"""
    if not groups:
        return None
    return min(groups, key=lambda jobs: (-len(jobs), min(job["ready_s"] for job in jobs)))


def earliest_due_group(
    groups: list[list[dict]],
    *,
    now=None,
    remaining: Callable[[dict], float] | None = None,
) -> list[dict] | None:
    """组内最早交期最小的合法开批组优先，其次最早就绪。"""
    if not groups:
        return None
    _require_due([job for group in groups for job in group], "earliest_due_group")
    return min(
        groups,
        key=lambda jobs: (min(job["due_s"] for job in jobs), min(job["ready_s"] for job in jobs)),
    )
