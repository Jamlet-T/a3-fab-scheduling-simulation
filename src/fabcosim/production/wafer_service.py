"""确定性整lot Wafer服务：仅消去片内事件，不改外部准入/换型/装卸。

白名单来自2026-10-24开发资料；不是任意设备或随机加工模式的等价保证。
"""

import math

from fabcosim.production.model import grid_time_at_or_after

WAFER_GROUPS = frozenset((
    'EPI_36', 'EPI_38', 'Implant_119', 'Implant_128', 'Implant_132', 'Implant_74',
    'Implant_88', 'Implant_90', 'Implant_91', 'LithoTrack_BE_117', 'LithoTrack_FE_115',
    'LithoTrack_FE_95', 'LithoTrack_FE_96', 'Litho_BE_110', 'Litho_BE_93', 'Litho_BE_99',
    'Litho_FE_111', 'Litho_FE_35', 'Litho_FE_92', 'Litho_FE_98', 'WE_BE_14', 'WE_BE_16',
    'WE_BE_17', 'WE_BE_7', 'WE_BE_81', 'WE_BE_82', 'WE_BE_9', 'WE_FE_108', 'WE_FE_41',
    'WE_FE_47', 'WE_FE_8', 'WE_FE_83', 'WE_FE_84', 'WE_FE_85',
))


def wafer_service_end(
    start_s: float, processing_s: float, interval_s: float, wafers: int,
    step_s: float, cascading: bool,
) -> float:
    """原网格语义下末片结束时刻；数值递推，无SimPy对象或随机抽样。

    module1在取得module2后释放，故下一片module1开始是上一片module2开始。
    不能对连续闭式结果只取整一次，否则如63/47.25秒的源参数会累积误差。
    """
    if type(wafers) is not int or wafers <= 0:
        raise ValueError('wafer服务需要正整数片数')
    if not all(math.isfinite(v) for v in (start_s, processing_s, interval_s, step_s)):
        raise ValueError('wafer服务时间必须有限')
    if min(start_s, processing_s, interval_s) < 0 or step_s <= 0:
        raise ValueError('wafer服务时长非负且step必须正')
    end = start_s
    if not cascading:
        for _ in range(wafers):
            end = grid_time_at_or_after(end + processing_s, step_s)
        return end
    interval = min(interval_s, processing_s)
    previous_start = start_s
    for _ in range(wafers):
        first_end = grid_time_at_or_after(previous_start + interval, step_s)
        previous_start = max(first_end, end)
        end = grid_time_at_or_after(previous_start + (processing_s - interval), step_s)
    return end


def fallback_reason(machine, op: dict, wafer_counts: list[int]) -> str | None:
    """仅已声明独占外部任务、内部模块无其他使用者时允许聚合。"""
    if machine.tool_group not in WAFER_GROUPS:
        return 'tool_group'
    if not machine.wafer_fast_context:
        return 'model_or_module_context'
    if machine.processing_distribution != 'nominal_mean':
        return 'processing_distribution'
    if wafer_counts != [25] or type(wafer_counts[0]) is not int:
        return 'wafer_count'
    if machine.step != 0.1:
        return 'time_grid'
    if any(r.count or r.queue for r in (machine.module1, machine.module2)):
        return 'modules_not_free'
    if not math.isfinite(op['processing_s']) or op['processing_s'] <= 0:
        return 'processing_duration'
    return None
