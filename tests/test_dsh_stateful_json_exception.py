import pytest
import json
import zipfile
from pathlib import Path
from tools.derive_dsh_stateful_json_exception import validate_native_append
from tools.derive_dsh_stateful_json_exception import derive
from tools.recover_dsh_reset_exceptions import digest, write_json


def test_only_already_adjudicated_native_events_can_be_preserved():
    before = [{'seq': 0, 'type': 'turn/end'}]
    event = {'seq': 1, 'type': 'assistant/message', 'data': {'text': 'saved'}}
    validate_native_append(before, before + [event], [{'events': [event], 'score': .7}, {'events': []}])
    validate_native_append(before, before, [{'events': []}])
    with pytest.raises(ValueError, match='unadjudicated'):
        validate_native_append(before, before + [event], [{'events': []}])
    with pytest.raises(ValueError, match='prefix changed'):
        validate_native_append(before, [{'seq': 0, 'type': 'different'}], [{'events': []}])


@pytest.mark.parametrize('has_native_state', [False, True])
def test_first_turn_requires_attested_workspace_and_entirely_empty_state(tmp_path, has_native_state):
    folder = tmp_path / 'job' / 'flow'
    folder.mkdir(parents=True)
    marker = 'DeepSeek Harness turn failed: error: URLError'
    log = folder / 'driver.log'
    log.write_text(marker)
    row = {'id': 'l1/frozen/deepseek-harness/s01', 'level': 'l1', 'condition': 'frozen',
        'agent': 'deepseek-harness', 'status': 'paused_error', 'flow_id': 'flow',
        'output': str(folder.parent), 'log': str(log), 'turns': ['t1', 't2']}
    record = {'turn_id': 't1', 'index': 1, 'response': '', 'events': [], 'tool_calls': [],
        'score': 0.0, 'passed': False, 'verifier_details': {'type': 'model_error', 'error': marker}}
    (folder / 'live_trace.jsonl').write_text(json.dumps(record) + '\n')
    with zipfile.ZipFile(folder / 'error_workspace.zip', 'w') as archive:
        archive.writestr('valid.txt', 'preserve actual workspace')
    with zipfile.ZipFile(folder / 'baseline_state.zip', 'w') as archive:
        if has_native_state:
            archive.writestr('session.json', 'cannot discard')
    write_json(folder / 'error_workspace_receipt.json', {'flow_id': 'flow', 'baseline': 'deepseek-harness',
        'condition': 'frozen', 'runtime_stopped_before_archive': True,
        'sha256': digest(folder / 'error_workspace.zip')})
    if has_native_state:
        with pytest.raises(ValueError, match='provably empty'):
            derive(row, tmp_path, cause='terminal_native_url_error')
        return
    checkpoint = derive(row, tmp_path, cause='terminal_native_url_error')
    assert digest(checkpoint / 'workspace.zip') == digest(folder / 'error_workspace.zip')
    assert json.loads((checkpoint / 'checkpoint.json').read_text())['next_turn_index'] == 1
    assert json.loads((checkpoint / 'completed_turns.json').read_text())[0]['score'] == 0
