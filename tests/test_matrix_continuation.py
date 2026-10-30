"""Dispatcher handoff must never turn a stopped paid worker into a retry."""
from copy import deepcopy
from pathlib import Path
import pytest
from tools import run_new_package_matrix as matrix


def fixture_rows(tmp_path):
    rows=[]
    for i,status in enumerate(['running','completed','paused_error','pending']):
        rows.append({'id':str(i),'level':'l2','flow_id':'s'+str(i),'agent':'opencode',
            'package':'package','condition':'frozen','turns':['t1','t2'],
            'output':str(tmp_path/'old'/str(i)),'status':status,'pid':100+i,
            'process_start_ticks':'known-birth','credential_slot':i+1,
            'credential_env':'FRIDAY_APP_ID_'+str(i+1),'credential_lane':1})
    source={'pid':99,'jobs':rows,'provider_gateway':{'policy':'unchanged'},
        'continuation_authorization':{'user_authorized':True},
        'credential_schedule':{'slot_names':['one'],'strategy':'stable','affinity_assignments':{},'slot_capacities':[2]}}
    fresh=[{**r,'output':str(tmp_path/'new'/r['id'])} for r in deepcopy(rows)]
    for r in fresh:r.pop('status')
    return fresh,source


def prepare(rows,source):
    return matrix.prepare_continuation_rows(rows,source,source['provider_gateway'],source['credential_schedule'])


def test_handoff_adopts_live_worker_preserves_paid_outputs_and_skips_errors(tmp_path,monkeypatch):
    rows,source=fixture_rows(tmp_path);adopted=[]
    monkeypatch.setattr(matrix,'process_start_ticks',lambda pid:None)
    monkeypatch.setattr(matrix,'AdoptedProcess',lambda row:adopted.append(row['id']))
    monkeypatch.setattr(matrix,'report_for',lambda row:(Path('report'),{'turns':[]}))
    result=prepare(rows,source)
    assert adopted==['0']
    assert [r.get('status','pending') for r in result]==['running','completed','paused_error','pending']
    assert all(result[i]['output']==source['jobs'][i]['output'] for i in range(3))
    assert result[3]['output']==str(tmp_path/'new'/'3')
    assert source['jobs'][0]['status']=='running'


@pytest.mark.parametrize('has_complete_report',[False,True])
def test_worker_exiting_in_handoff_never_replays(tmp_path,monkeypatch,has_complete_report):
    rows,source=fixture_rows(tmp_path)
    monkeypatch.setattr(matrix,'process_start_ticks',lambda pid:None)
    def exited(row):raise ValueError('worker exited')
    monkeypatch.setattr(matrix,'AdoptedProcess',exited)
    monkeypatch.setattr(matrix,'report_for',lambda row:(Path('report'),{} if has_complete_report or row['id']=='1' else None))
    result=prepare(rows,source)
    assert result[0]['status']==('completed' if has_complete_report else 'blocked_recovery')
    assert result[0]['output']==source['jobs'][0]['output']


@pytest.mark.parametrize('change',['live_driver','no_authorization','gateway','scope','slot','turns','pending_evidence','bad_completed'])
def test_unsafe_continuation_is_rejected(tmp_path,monkeypatch,change):
    rows,source=fixture_rows(tmp_path)
    monkeypatch.setattr(matrix,'process_start_ticks',lambda pid:'live' if change=='live_driver' else None)
    monkeypatch.setattr(matrix,'AdoptedProcess',lambda row:None)
    monkeypatch.setattr(matrix,'report_for',lambda row:(None,None) if change=='bad_completed' else (Path('report'),{}))
    gateway=source['provider_gateway']
    if change=='no_authorization':source['continuation_authorization']['user_authorized']=False
    if change=='gateway':gateway={'different':True}
    if change=='scope':rows.pop()
    if change=='slot':rows[0]['credential_slot']=6
    if change=='turns':rows[0]['turns']=['t1']
    if change=='pending_evidence':
        p=Path(source['jobs'][3]['output']);p.mkdir(parents=True);(p/'paid-trace').write_text('evidence')
    with pytest.raises(ValueError):matrix.prepare_continuation_rows(rows,source,gateway,source['credential_schedule'])
