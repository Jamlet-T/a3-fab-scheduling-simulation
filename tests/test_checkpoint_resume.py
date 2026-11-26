"""真实新进程恢复：事件与摘要严格等于不中断基线，不重放历史。"""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_production_only import fixture_config

from fabcosim.cosim import full

ROOT = Path(__file__).resolve().parents[1]


def config_for(kind):
    c=fixture_config(batch=kind=='batch',count=5 if kind=='random_availability' else 3,capacity=2)
    c['max_time_s']=150
    c['observation']={'warmup_s':0,'end_s':150}
    c['performance'].update(stream_events=True,compact_json=True)
    if kind=='batch':
        c['routes']['R']['operations'][1]['virtual']=True
        c['performance'].update(metric_history='sqlite',event_compression='gzip')
    if kind=='random_availability':
        c['production']['processing_distribution']='uniform'
        c['production']['max_visits_per_lot']=100
        c['toolgroups']['G'].update(AREA='A',LOADINGTIME=.025,UNLOADINGTIME=.025)
        c['routes']['R']['operations'][0].update(offset_s=1,setup='S',setup_s=4,rework_probability=.25,rework_step=1)
        c['availability']={'enabled':True,'pm':True,'breakdown':True}
        c['source_catalog']['production']={'path':'fixture-not-read.xlsx'}
        c['performance'].update(metric_history='sqlite',event_compression='gzip')
    return c


def resume(checkpoint,output):
    env=os.environ.copy()
    env['PYTHONPATH']=str(ROOT/'src')
    return subprocess.run([sys.executable,'-m','fabcosim','full','--resume',str(checkpoint),'--output',str(output)],
                          cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=90)


@pytest.mark.parametrize('kind,at',[('simple',1),('simple',3),('simple',8),('batch',6.5),('random_availability',4.3),('random_availability',17.7)])
def test_new_process_resume_exact(monkeypatch,tmp_path,kind,at):
    c=config_for(kind)
    monkeypatch.setattr(full,'compile_full_routes',lambda prepared,root:copy.deepcopy(prepared))
    pm=[{'event':'PM','counter':False,'period':50,'first':2,'distribution':'uniform','mean':3,'offset':.5},
        {'event':'COUNT','counter':True,'period':50,'first':25,'distribution':'uniform','mean':1,'offset':0}]
    breakdown={'event':'BREAK','ttf':30,'ttr':2,'first':8}
    monkeypatch.setattr(full,'load_availability',lambda *args:{'pm_by_group':{'G':pm},'breakdown_by_area':{'A':breakdown}})
    baseline=tmp_path/'baseline'
    expected=full.run_full(copy.deepcopy(c),baseline)
    c['checkpoint']={'interval_s':at,'stop_after_save':True}
    paused=tmp_path/'paused'
    saved=full.run_full(c,paused)
    assert saved['status']=='checkpoint_saved' and saved['end_time_s']==at
    snapshot=paused/'checkpoints/latest.json'
    assert snapshot.exists()
    output=tmp_path/'resumed'
    completed=resume(snapshot,output)
    assert completed.returncode==0,completed.stderr
    actual=json.loads((output/'summary.json').read_text('utf-8'))
    assert actual==expected
    log='events.json.gz' if kind!='simple' else 'events.json'
    assert (output/log).read_bytes()==(baseline/log).read_bytes()
    # 可从同一断点再建独立分支；源run及快照不被续算改写。
    original=snapshot.read_bytes()
    output2=tmp_path/'resumed-again'
    completed=resume(snapshot,output2)
    assert completed.returncode==0,completed.stderr
    assert snapshot.read_bytes()==original
    assert (output2/log).read_bytes()==(baseline/log).read_bytes()
