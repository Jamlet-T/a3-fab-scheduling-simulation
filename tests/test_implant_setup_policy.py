"""换型偏好只重排合法本地job；老化保护、非目标回退及真实加工链验证。"""
import copy

import pytest
from test_production_only import execute, fixture_config

from fabcosim.policies.production import implant_setup_fifo, implant_setup_fifo_3groups


def test_setup_cost_and_age_override_without_mutation():
    queue = [dict(ready_s=0, op={'unit': 'Wafer', 'setup_s': 60}),
             dict(ready_s=10, op={'unit': 'Wafer', 'setup_s': 0})]
    original = copy.deepcopy(queue)
    kwargs = dict(tool_group='Implant_132', setup_duration=lambda op: op['setup_s'])
    assert implant_setup_fifo(queue, now=20, **kwargs) is queue[1]
    assert implant_setup_fifo(queue, now=86400, **kwargs) is queue[0]
    assert queue == original
    assert implant_setup_fifo([], now=20, **kwargs) is None
    assert implant_setup_fifo(queue, now=20, **(kwargs | {'tool_group': 'Litho'})) is queue[0]


def test_batch_falls_back_and_equal_cost_keeps_fifo():
    queue = [dict(ready_s=0, op={'unit': 'Batch'}), dict(ready_s=1, op={'unit': 'Batch'})]
    kwargs = dict(tool_group='Implant_128', setup_duration=lambda op: 0)
    assert implant_setup_fifo(queue, now=10, **kwargs) is queue[0]
    for job in queue:
        job['op']['unit'] = 'Lot'
    assert implant_setup_fifo(queue, now=10, **kwargs) is queue[0]


@pytest.mark.parametrize('group', ['Implant_128', 'Implant_132', 'Implant_91'])
def test_real_dispatch_reduces_setups_without_changing_work(monkeypatch, tmp_path, group):
    results = {}
    for rule in ('fifo', 'implant_setup_fifo', 'implant_setup_fifo_3groups'):
        cfg = fixture_config(count=3, capacity=3)
        cfg['assets']['M']['tool_group'] = group
        cfg['candidates'][group] = cfg['candidates'].pop('G')
        cfg['toolgroups'][group] = cfg['toolgroups'].pop('G')
        op = cfg['routes']['R']['operations'][0] | dict(
            tool_group=group, processing_s=20, setup='A', setup_s=10)
        cfg['routes']['R'].update(operations=[op], remaining_s={1:20}, rpt_s=20)
        cfg['routes']['B'] = copy.deepcopy(cfg['routes']['R'])
        cfg['routes']['B']['operations'][0]['setup'] = 'B'
        cfg['lots'][1]['route'] = 'B'
        cfg['source_catalog']['routes'] = {'R':1, 'B':1}
        cfg['policies'] = {'production': {'job':rule}}
        summary, events = execute(monkeypatch, tmp_path / rule, cfg)
        assert summary['completed_lots'] == 3 and summary['completed_operations'] == 3
        assert summary['underfilled_batches'] == 0
        order = [e['lot_id'] for e in events if e['kind'] == 'processing.start']
        setups = [e for e in events if e['kind'] == 'setup.start']
        results[rule] = order, len(setups)
    assert results['fifo'] == (['L0', 'L1', 'L2'], 3)
    expected = results['fifo'] if group == 'Implant_91' else (['L0', 'L2', 'L1'], 2)
    assert results['implant_setup_fifo'] == expected
    assert results['implant_setup_fifo_3groups'] == (['L0', 'L2', 'L1'], 2)


@pytest.mark.parametrize('rule', ['implant_setup_fifo', 'implant_setup_fifo_3groups'])
def test_immediate_rejected(monkeypatch, tmp_path, rule):
    cfg = fixture_config()
    cfg['production']['dispatch_mode'] = 'immediate'
    cfg['policies'] = {'production': {'job':rule}}
    with pytest.raises(ValueError, match='要求dispatch_mode=deferred'):
        execute(monkeypatch, tmp_path, cfg)


@pytest.mark.parametrize('rule', ['implant_setup_fifo', 'implant_setup_fifo_3groups'])
def test_strict_batch_still_blocks_incomplete_group(monkeypatch, tmp_path, rule):
    cfg = fixture_config(batch=True, count=1)
    cfg['observation'] = {'warmup_s': 0, 'end_s': 100}
    cfg['policies'] = {'production': {'job':rule}}
    summary, events = execute(monkeypatch, tmp_path, cfg)
    assert summary['completed_lots'] == 0
    assert not any(e['kind'] == 'processing.start' for e in events)


def test_three_group_age_boundary_stable_ties_and_fallback():
    queue = [dict(ready_s=0, op={'unit': 'Wafer', 'setup_s': 80}),
             dict(ready_s=1, op={'unit': 'Wafer', 'setup_s': 0})]
    before = copy.deepcopy(queue)
    args = dict(tool_group='Implant_91', setup_duration=lambda op: op['setup_s'])
    assert implant_setup_fifo_3groups(queue, now=86399, **args) is queue[1]
    assert implant_setup_fifo_3groups(queue, now=86400, **args) is queue[0]
    assert implant_setup_fifo_3groups(queue, now=86401, **args) is queue[0]
    assert queue == before
    assert implant_setup_fifo(queue, now=20, **args) is queue[0]
    assert implant_setup_fifo_3groups([], now=20, **args) is None
    assert implant_setup_fifo_3groups(queue, now=20,
                                    **(args | {'tool_group': 'Implant_99'})) is queue[0]
    assert implant_setup_fifo_3groups(queue, now=20,
                                    **(args | {'setup_duration': lambda op: 0})) is queue[0]
    queue[1]['op']['unit'] = 'Batch'
    assert implant_setup_fifo_3groups(queue, now=20, **args) is queue[0]
