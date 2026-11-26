"""纯生产的人工小工艺：严格成批、输入容量与非零转运，不依赖运输执行。"""
import copy
import json

import pytest

from fabcosim.cosim import full


def fixture_config(batch=False, count=3, capacity=2):
    op = dict(source_step=1, tool_group='G', unit='Batch' if batch else 'Lot',
              processing_s=5, offset_s=0, interval_s=0, virtual=False,
              sample_probability=1, rework_probability=0, rework_step=0,
              dedication_step=0, cqt_step=0, cqt_s=0, setup='', setup_when='need',
              setup_s=0, batch_min=50, batch_max=50)
    ops = [op, {**op, 'source_step': 2, 'unit': 'Lot'}]
    assets = {name: dict(kind=kind, tool_group=group, input_port=name+'.in', output_port=name+'.out')
              for name, kind, group in [('C','commit','COMMIT'), ('M','tool','G'), ('E','complete','COMPLETE')]}
    return dict(name='pure-fixture', seed=3, step_s=0.1, max_time_s=100,
                transport={'model':'production_only'}, replay={'enabled':False},
                production=dict(routes=['R'], processing_distribution='nominal_mean',
                                input_buffer_lots=capacity, fixed_transfer_s=2,
                                batch_wait_s=5, allow_underfilled_after_timeout=False,
                                queue_penalty_m=1, max_visits_per_lot=10, dispatch_mode='deferred'),
                lots=[dict(id=f'L{i}', route='R', wafer_count=25, release_s=i*3) for i in range(count)],
                routes={'R':dict(operations=ops, remaining_s={1:10,2:5}, rpt_s=10)},
                source_catalog={'routes':{'R':2}}, assets=assets, ports={},
                candidates={'COMMIT':['C'],'G':['M'],'COMPLETE':['E']},
                toolgroups={'G':{'CASCADINGTOOL':None,'LOADINGTIME':0,'UNLOADINGTIME':0}}, setups=[],
                performance={'incremental_material_audit':True, 'queue_occupancy_index':True})


def execute(monkeypatch, tmp_path, config):
    def forbidden(*args, **kwargs):
        raise AssertionError('纯生产不得调用运输/端口/路由')
    monkeypatch.setattr(full, 'compile_full_routes', lambda prepared, root: copy.deepcopy(config))
    for name in ('DelayTransport','FleetTransport','Port','route_between','route_shapes'):
        monkeypatch.setattr(full, name, forbidden)
    summary = full.run_full(config, tmp_path/'run')
    events = json.loads((tmp_path/'run/events.json').read_text('utf-8'))
    return summary, events


def test_complete_multistep_capacity_and_transfers(monkeypatch, tmp_path):
    summary, events = execute(monkeypatch, tmp_path, fixture_config(capacity=1))
    assert summary['status']=='completed' and summary['completed_lots']==3
    assert summary['completed_operations']==6 and summary['vehicle_count']==0
    assert summary['completed_tasks']==0 and summary['collisions'] is None
    assert not any(e['kind'].startswith(('transport.','vehicle.','task.','material.route')) for e in events)
    arrivals, prior_ends, release = {}, {}, {}
    for event in events:
        lid, kind = event.get('lot_id'), event['kind']
        if kind=='lot.released':
            release[lid]=event['time']
        elif kind=='production.transfer_start':
            assert event['time']>=release[lid]
            prior_ends[lid]=event['time']+2
        elif kind=='production.transfer_end':
            assert event['time']==prior_ends[lid]
            arrivals[lid]=event['time']
        elif kind=='processing.start':
            assert event['time']>=arrivals[lid]
    assert len(release)==3


def test_strict_batch_and_remaining_wip(monkeypatch, tmp_path):
    config=fixture_config(batch=True, count=3)
    config['observation']={'warmup_s':0,'end_s':100}
    summary, events=execute(monkeypatch,tmp_path,config)
    assert summary['completed_lots']==2 and summary['wip_lots']==1
    batches=[e for e in events if e['kind']=='batch.formed']
    assert len(batches)==1 and batches[0]['wafers']==50
    assert len(set(batches[0]['lot_ids']))==2 and not batches[0]['underfilled']
    assert summary['metrics']['transport_wait'] is None


def test_future_release_is_not_skipped(monkeypatch,tmp_path):
    config=fixture_config(count=2)
    config['lots'][1]['release_s']=60
    summary,events=execute(monkeypatch,tmp_path,config)
    assert summary['released_lots']==summary['completed_lots']==2
    assert next(e['time'] for e in events if e['kind']=='lot.released' and e['lot_id']=='L1')==60


@pytest.mark.parametrize('delay',[0,-1,float('inf'),True])
def test_invalid_transfer_rejected_before_output(tmp_path,delay):
    config=fixture_config()
    config['production']['fixed_transfer_s']=delay
    with pytest.raises(ValueError,match='fixed_transfer_s'):
        full.run_full(config,tmp_path/'invalid')
    assert not (tmp_path/'invalid').exists()

def test_rework_keeps_predecessor_and_transfer_order(monkeypatch,tmp_path):
    config=fixture_config(count=1)
    config['routes']['R']['operations'][1].update(rework_probability=1,rework_step=1)
    instances=[]
    class Decisions:
        def __init__(self,seed):
            self.purpose=len(instances)%3
            self.calls=0
            instances.append(self)
        def random(self):
            self.calls+=1
            return 0 if self.purpose==0 or self.purpose==1 and self.calls==2 else 1
    monkeypatch.setattr(full.random,'Random',Decisions)
    summary,events=execute(monkeypatch,tmp_path,config)
    assert [e['source_step'] for e in events if e['kind']=='processing.start']==[1,2,1,2]
    assert summary['reworks']==1 and summary['completed_operations']==4
    assert sum(e['kind']=='production.transfer_end' for e in events)==5


def test_calendar_pm_blocks_new_processing(monkeypatch,tmp_path):
    config=fixture_config(count=1)
    config['availability']={'enabled':True,'pm':True,'breakdown':False}
    config['source_catalog']['production']={'path':'not-read.xlsx'}
    pm=dict(event='PM',counter=False,period=1000,first=1,distribution='uniform',mean=4,offset=0)
    monkeypatch.setattr(full,'load_availability',lambda *args: {'pm_by_group':{'G':[pm]}})
    summary,events=execute(monkeypatch,tmp_path,config)
    starts=[e['time'] for e in events if e['kind']=='processing.start']
    assert starts[0]==5
    assert summary['availability_outages']==1 and summary['availability_down_s']==4


def test_virtual_stage_retains_its_production_capacity(monkeypatch,tmp_path):
    config=fixture_config(count=2,capacity=1)
    config['routes']['R']['operations'][0]['virtual']=True
    summary,events=execute(monkeypatch,tmp_path,config)
    assert summary['completed_lots']==2 and summary['delay_operations']==2
    first_end=next(e['time'] for e in events if e['kind']=='delay.end' and e['lot_id']=='L0')
    second_start=next(e['time'] for e in events if e['kind']=='production.transfer_start' and e['lot_id']=='L1')
    assert second_start>=first_end

def test_stream_sqlite_and_incremental_options_preserve_business(monkeypatch,tmp_path):
    import gzip
    config=fixture_config(batch=True,count=4)
    config['observation']={'warmup_s':0,'end_s':100}
    (tmp_path/'memory').mkdir()
    summary,events=execute(monkeypatch,tmp_path/'memory',config)
    optimized=copy.deepcopy(config)
    optimized['performance'].update(stream_events=True,compact_json=True,
                                    metric_history='sqlite',event_compression='gzip',
                                    material_audit_port_index=True)
    monkeypatch.setattr(full,'compile_full_routes',lambda *args:copy.deepcopy(optimized))
    second=full.run_full(optimized,tmp_path/'optimized')
    with gzip.open(tmp_path/'optimized/events.json.gz','rt',encoding='utf-8') as stream:
        assert json.load(stream)==events
    assert second==summary
