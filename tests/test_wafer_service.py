"""整lot快速档的源参数、外部资源、维护、边界和默认路径。"""

import copy
import json
from pathlib import Path

import pytest
import simpy
from test_production_only import execute, fixture_config

from fabcosim.production.availability import MachineAvailability
from fabcosim.production.machines import Machine
from fabcosim.production.wafer_service import WAFER_GROUPS, wafer_service_end


def make_machine(env, mode, *, group='Implant_132', cascading=True, availability=None, **kwargs):
    return Machine(env, 'M', {'CASCADINGTOOL': 'YES' if cascading else None,
        'LOADINGTIME': 0, 'UNLOADINGTIME': 0}, [], 0.1, lambda *a, **k: None,
        availability, wafer_detail=mode, tool_group=group,
        processing_distribution=kwargs.get('processing_distribution', 'nominal_mean'),
        wafer_fast_context=kwargs.get('safe', True))


def op(p=4, interval=3, unit='Wafer', setup=''):
    return dict(unit=unit, processing_s=p, interval_s=interval, setup=setup,
                setup_when='need', setup_s=3)


def test_all_source_parameters_same_completion_tick():
    data=json.loads((Path(__file__).parent/'fixtures/wafer_service_source_parameters.json').read_text('utf-8'))
    assert data['source_operation_count']==1774
    assert sum(len(p['source_steps']) for p in data['parameters'])==1774
    assert {p['group'] for p in data['parameters']}==WAFER_GROUPS
    for p in data['parameters']:
        for start in (0,30*86400,59*86400+0.1):
            ends=[]
            for mode in ('micro','lot_service'):
                env=simpy.Environment(initial_time=start)
                m=make_machine(env,mode,group=p['group'],cascading=p['cascading'])
                operation=op(p['processing_s'],p['interval_s'])
                env.process(m.process(operation,[25],lambda:p['processing_s'],lambda *a,**k:None))
                env.run()
                ends.append(env.now)
                assert m.admission.count==m.module1.count==m.module2.count==0
                assert m.wafer_count==25 and m.run_count==1
            assert ends[0]==ends[1], (p,start,ends)


@pytest.mark.parametrize('n',[1,25])
@pytest.mark.parametrize('interval',[0,1,3,4,8])
def test_service_calculator_small_and_clamped(n,interval):
    env=simpy.Environment(initial_time=3.1)
    m=make_machine(env,'micro')
    env.process(m.process(op(4,interval),[n],lambda:4,lambda *a,**k:None))
    env.run()
    assert env.now==wafer_service_end(3.1,4,interval,n,0.1,bool(interval))


@pytest.mark.parametrize('change,reason', [
    ({'group':'Unknown'},'tool_group'), ({'processing_distribution':'uniform'},'processing_distribution'),
    ({'safe':False},'model_or_module_context'), ({'quantity':24},'wafer_count')])
def test_fallback_keeps_detailed_processing(change,reason):
    change=dict(change)
    quantity=change.pop('quantity',25)
    env=simpy.Environment()
    m=make_machine(env,'lot_service',**change)
    env.process(m.process(op(),[quantity],lambda:4,lambda *a,**k:None))
    env.run()
    assert m.wafer_counts['fallback.'+reason]==1
    assert m.wafer_counts['aggregated_visits']==0
    assert env.now==4+(quantity-1)*3


def test_shared_module_at_entry_falls_back():
    env=simpy.Environment()
    m=make_machine(env,'lot_service')
    held=m.module2.request()
    def release():
        yield env.timeout(10)
        m.module2.release(held)
    env.process(release())
    env.process(m.process(op(),[25],lambda:4,lambda *a,**k:None))
    env.run()
    assert m.wafer_counts['fallback.modules_not_free']==1
    assert m.wafer_count==25 and m.module2.count==0


def test_other_grid_falls_back():
    env=simpy.Environment()
    m=make_machine(env,'lot_service')
    m.step=.2
    env.process(m.process(op(),[25],lambda:4,lambda *a,**k:None))
    env.run()
    assert m.wafer_counts['fallback.time_grid']==1 and env.now==76


@pytest.mark.parametrize('mode',['micro','lot_service'])
def test_cutoff_holds_external_admission_and_does_not_update_counts(mode):
    env=simpy.Environment()
    m=make_machine(env,mode)
    env.process(m.process(op(),[25],lambda:4,lambda *a,**k:None))
    env.run(until=10)
    assert m.admission.count==1 and m.wafer_count==m.run_count==0
    env.run()
    assert env.now==76 and m.admission.count==0 and m.wafer_count==25


def test_counter_pm_setup_loading_and_second_lot_keep_counts_and_times():
    results=[]
    for mode in ('micro','lot_service'):
        env=simpy.Environment()
        availability=MachineAvailability('M',{'pm':[dict(counter=True,first=25,period=25,
            event='PM',distribution='uniform',mean=4,offset=0)]},3)
        logs=[]
        m=make_machine(env,mode,availability=availability)
        m.record=lambda kind,**kw:logs.append((env.now,kind,kw))
        m.group.update(LOADINGTIME=1/60,UNLOADINGTIME=2/60)
        def run():
            for setup in ('A','B'):
                yield env.process(m.process(op(setup=setup),[25],lambda:4,
                    lambda kind,**kw:logs.append((env.now,kind,kw))))
        env.process(run())
        env.run()
        assert m.wafer_count==50 and m.run_count==1 and m.setup=='B'
        assert sum(k=='availability.down' for _,k,_ in logs)==1
        results.append((env.now,logs))
    assert results[0]==results[1]


@pytest.mark.parametrize('arrival',[75.9,76,76.1])
def test_calendar_and_arrival_same_as_completion(arrival):
    results=[]
    for mode in ('micro','lot_service'):
        env=simpy.Environment()
        av=MachineAvailability('M',{'pm':[dict(counter=False,first=76,period=10000,
            event='PM',distribution='uniform',mean=4,offset=0)]},3)
        m=make_machine(env,mode,availability=av)
        logs=[]
        def log(kind,**kw):
            logs.append((env.now,kind))
        m.record=log
        def second():
            yield env.timeout(arrival)
            yield env.process(m.process(op(),[25],lambda:4,log))
        env.process(m.downtime())
        env.process(m.process(op(),[25],lambda:4,log))
        env.process(second())
        env.run(until=160)
        assert m.wafer_count==50 and m.admission.count==0 and m.gate.count==0
        results.append(logs)
    # 外部事件序列可能同刻重排；时间和每lot顺序不得错误。
    assert sorted(results[0])==sorted(results[1])


def fast_fixture():
    cfg=fixture_config(count=3,capacity=1)
    for o in cfg['routes']['R']['operations']:
        o.update(unit='Wafer',tool_group='Implant_132',processing_s=0.8,interval_s=0.6)
    cfg['assets']['M']['tool_group']='Implant_132'
    cfg['candidates']['Implant_132']=cfg['candidates'].pop('G')
    cfg['toolgroups']['Implant_132']=cfg['toolgroups'].pop('G')
    cfg['toolgroups']['Implant_132']['CASCADINGTOOL']='YES'
    cfg['max_time_s']=1000
    return cfg


def test_full_default_micro_fast_and_truncation(monkeypatch,tmp_path):
    cfg=fast_fixture()
    a,ea=execute(monkeypatch,tmp_path/'default',cfg)
    micro=copy.deepcopy(cfg)
    micro['production']['wafer_detail']='micro'
    b,eb=execute(monkeypatch,tmp_path/'micro',micro)
    assert a==b and ea==eb and 'wafer_detail' not in a
    fast=copy.deepcopy(cfg)
    fast['production']['wafer_detail']='lot_service'
    c,ec=execute(monkeypatch,tmp_path/'fast',fast)
    assert c['completed_lots']==a['completed_lots']==3
    assert c['wafer_detail']['counts']['aggregated_visits']==6
    assert c['underfilled_batches']==0
    fast['observation']={'warmup_s':0,'end_s':10}
    fast['max_time_s']=10
    cut,events=execute(monkeypatch,tmp_path/'cut',fast)
    assert cut['completed_lots']==0 and cut['released_lots']==cut['wip_lots']==3
    assert any(e['kind']=='processing.start' for e in events)
    assert not any(e['kind']=='processing.end' for e in events)


def test_fast_age_override_at_real_dispatch(monkeypatch,tmp_path):
    orders=[]
    for mode in ('micro','lot_service'):
        cfg=fast_fixture()
        first=cfg['routes']['R']['operations'][0]
        first.update(processing_s=5000,interval_s=3750,setup='A',setup_s=3)
        cfg['routes']['R'].update(operations=[first],remaining_s={1:95000},rpt_s=95000)
        cfg['routes']['B']=copy.deepcopy(cfg['routes']['R'])
        cfg['routes']['B']['operations'][0]['setup']='B'
        cfg['source_catalog']['routes']={'R':1,'B':1}
        cfg['lots'][1].update(route='B',release_s=1)
        cfg['lots'][2]['release_s']=90000
        cfg['max_time_s']=400000
        cfg['policies']={'production':{'job':'implant_setup_fifo_3groups'}}
        cfg['production']['wafer_detail']=mode
        summary,events=execute(monkeypatch,tmp_path/mode,cfg)
        assert summary['completed_lots']==3
        starts=[(e['lot_id'],e['time']) for e in events if e['kind']=='processing.start']
        assert [x[0] for x in starts]==['L0','L1','L2']
        # 当前A零换型的L2刚到，B已老化，仍必须先B。
        assert starts[1][1]-3>=86400
        orders.append(starts)
    assert orders[0]==orders[1]


def test_full_uniform_fallback_keeps_random_draws(monkeypatch,tmp_path):
    cfg=fast_fixture()
    cfg['production']['processing_distribution']='uniform'
    for operation in cfg['routes']['R']['operations']:
        operation['offset_s']=.2
    a,ea=execute(monkeypatch,tmp_path/'uniform-micro',cfg)
    cfg['production']['wafer_detail']='lot_service'
    b,eb=execute(monkeypatch,tmp_path/'uniform-fallback',cfg)
    assert ea==eb
    assert b.pop('wafer_detail')['counts']['fallback.processing_distribution']==6
    assert a==b


def test_batch_and_lot_modes_untouched(monkeypatch,tmp_path):
    for batch in (True,False):
        cfg=fixture_config(batch=batch,count=3)
        cfg['observation']={'warmup_s':0,'end_s':100}
        a,ea=execute(monkeypatch,tmp_path/f'unchanged-{batch}-micro',cfg)
        cfg['production']['wafer_detail']='lot_service'
        b,eb=execute(monkeypatch,tmp_path/f'unchanged-{batch}-fast',cfg)
        assert ea==eb
        stats=b.pop('wafer_detail')['counts']
        assert stats['untouched_nonwafer_visits']>0
        assert stats.get('aggregated_visits',0)==0
        assert a==b


def test_lot_cascade_cross_lot_module_semantics_untouched():
    results=[]
    for mode in ('micro','lot_service'):
        env=simpy.Environment()
        m=make_machine(env,mode)
        logs=[]
        def run(name,p,interval):
            yield env.process(m.process(op(p,interval,unit='Lot'),[25],lambda:p,
                lambda kind,**kw:logs.append((env.now,name,kind,kw))))
        env.process(run('a',10,7))
        env.process(run('b',4,2))
        env.run()
        assert env.now==12
        assert m.wafer_count==50 and m.module1.count==m.module2.count==m.admission.count==0
        if mode=='lot_service':
            assert m.wafer_counts['untouched_nonwafer_visits']==2
        results.append(logs)
    assert results[0]==results[1]


@pytest.mark.parametrize('mode',['bad',True])
def test_bad_mode_rejected_before_output(mode,tmp_path):
    from fabcosim.cosim.full import run_full
    with pytest.raises(ValueError,match='wafer_detail'):
        run_full({'production':{'wafer_detail':mode}},tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_fast_snapshot_rejected_before_output(tmp_path):
    from fabcosim.cosim.full import run_full
    with pytest.raises(ValueError,match='不支持完整快照'):
        run_full({'production':{'wafer_detail':'lot_service'},'checkpoint':{'interval_s':86400}},tmp_path/'out')
    assert not (tmp_path/'out').exists()
