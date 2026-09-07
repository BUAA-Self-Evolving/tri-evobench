import copy
import json
import zipfile
from pathlib import Path

import pytest

from tools.merge_l2_reset_retry import merge_reset_retry


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def fixture(tmp_path):
    root = tmp_path / 'job'
    manifest = save(tmp_path / 'stream.json', {'stream_id': 's', 'ordering': {'turn_order': ['t1','t2','t3']}})
    def turn(name, index, score, label):
        trace = root / label / 'trace.jsonl'
        trace.parent.mkdir(parents=True, exist_ok=True)
        with trace.open('a') as f:
            f.write(json.dumps({'turn_id':name,'interaction_index':1,'response':'real answer','score':score,'evaluation_status':'failed'})+'\n')
        checkpoint = root / label / name
        checkpoint.mkdir()
        item = {'turn_id':name,'turn_index':index,'stream_id':'s','condition':'reset','turn_score':score,'evaluation_status':'failed','passed':False,'final_category':'fail','interaction_count':1,'trace_file':str(trace),'checkpoint':str(checkpoint),'evolution':{'completed':True,'events':[]}}
        for archive in ['workspace.zip','baseline_state.zip']:
            with zipfile.ZipFile(checkpoint/archive,'w') as z:z.writestr('file','preserved')
        return item
    first=turn('t1',1,0,'prefix')
    retry=turn('t2',2,.5,'retry')
    invalid=copy.deepcopy(retry);invalid['evolution']={'completed':False,'events':[{'reason':'runtime_unavailable'}]}
    last=turn('t3',3,0,'suffix')
    common={'schema_version':'l2-agent-runtime-0.2-taskstream','stream_id':'s','baseline':'hermes','condition':'reset','work_limits':{'max_output_tokens':12000},'started_at':'2026-09-07T00:00:00+00:00','finished_at':'2026-09-07T01:00:00+00:00','wall_clock_seconds':100}
    a={**common,'run_id':'retry','turns':[first,retry],'stopped_after_turn':'t2','token_budget':{'limit':1000,'used':25,'calls':2}}
    b={**common,'run_id':'suffix','turns':[first,invalid,last],'completed':True,'evaluation_complete':True,'token_budget':{'limit':1000,'used':30,'calls':3}}
    for t, budget in [(retry,{'limit':1000,'used':20,'calls':2}), (last,b['token_budget'])]:
        save(Path(t['checkpoint'])/'checkpoint.json',{'stream_id':'s','baseline':'hermes','condition':'reset','next_turn_index':t['turn_index'],'token_budget':budget})
    save(Path(last['checkpoint'])/'completed_turns.json',b['turns'])
    a=copy.deepcopy(a);b=copy.deepcopy(b)
    pa=save(root/'s/retry/report.json',a);pb=save(root/'s/suffix/report.json',b)
    return root,manifest,pa,pb,a,b


def test_merge_preserves_valid_suffix_and_deduplicates_budget(tmp_path):
    root,manifest,pa,pb,a,b=fixture(tmp_path)
    original=pb.read_bytes()
    result=merge_reset_retry(pa,pb,manifest,'t2',root)
    report=json.loads(result.read_text())
    assert report['turns']==a['turns']+[b['turns'][2]]
    assert report['token_budget']=={'limit':1000,'used':35,'calls':3}
    assert report['completed'] and report['evaluation_complete'] and not report['success']
    assert pb.read_bytes()==original
    assert Path(report['merge_provenance']['checkpoint']).is_dir()


@pytest.mark.parametrize('failure',['persistent','prefix','synthetic_suffix','limits','unfinished','trace_score','valid_target'])
def test_merge_rejects_unsafe_sources(tmp_path,failure):
    root,manifest,pa,pb,a,b=fixture(tmp_path)
    if failure=='persistent':a['condition']=b['condition']='evolving'
    elif failure=='prefix':b['turns'][0]['turn_score']=1
    elif failure=='synthetic_suffix':b['turns'][2]['evolution']={'events':[{'reason':'runtime_unavailable'}]}
    elif failure=='limits':b['work_limits']['max_output_tokens']=40000
    elif failure=='unfinished':b['completed']=False
    elif failure=='trace_score':b['turns'][2]['turn_score']=.9
    elif failure=='valid_target':
        b['turns'][1]['evolution']={'completed':True,'events':[]}
        save(Path(b['turns'][-1]['checkpoint'])/'completed_turns.json',b['turns'])
    save(pa,a);save(pb,b)
    with pytest.raises(ValueError):merge_reset_retry(pa,pb,manifest,'t2',root)
