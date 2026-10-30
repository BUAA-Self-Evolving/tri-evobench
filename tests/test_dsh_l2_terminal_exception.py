import zipfile
import json
import pytest

from tools.derive_dsh_l2_terminal_exception import derive, validate_unchanged_state
from tools.recover_dsh_reset_exceptions import write_json, claim


def bundle(path, values):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, value in values.items():
            archive.writestr(name, value)
    return path


def test_all_native_files_and_workspace_must_remain_equal(tmp_path):
    workspace = bundle(tmp_path / 'w.zip', {'a': 'valid'})
    pre = bundle(tmp_path / 'pre.zip', {'session.json': 'saved', '.trievo_semantic_state.json': '{}'})
    post = bundle(tmp_path / 'post.zip', {'session.json': 'saved'})
    validate_unchanged_state(workspace, workspace, pre, post)
    changed = bundle(tmp_path / 'changed.zip', {'session.json': 'unjudged'})
    with pytest.raises(ValueError, match='native state changed'):
        validate_unchanged_state(workspace, workspace, pre, changed)
    mutated = bundle(tmp_path / 'mutation.zip', {'a': 'tool mutation'})
    with pytest.raises(ValueError, match='workspace changed'):
        validate_unchanged_state(workspace, mutated, pre, post)


@pytest.mark.parametrize('saved_reply', [False, True])
def test_l2_derivation_preserves_prior_score_and_never_skips_saved_reply(tmp_path, saved_reply):
    output = tmp_path / 'cell'
    base = output / '_checkpoints' / 'stream' / 'run' / 'i0001-02'
    post = output / 'stream' / 'run'
    base.mkdir(parents=True)
    post.mkdir(parents=True)
    log = output / 'driver.log'
    log.write_text('DeepSeek Harness turn failed: error: JSONDecodeError')
    row = {'id': 'l2/frozen/deepseek-harness/s01', 'level': 'l2', 'condition': 'frozen',
        'agent': 'deepseek-harness', 'status': 'paused_error', 'flow_id': 'stream',
        'output': str(output), 'log': str(log), 'turns': ['turn1', 'turn2', 'turn3']}
    prefix = [{'turn_id': 'turn1', 'turn_score': .8}]
    meta = {'phase': 'reply_received' if saved_reply else 'interact_started', 'stream_id': 'stream',
        'condition': 'frozen', 'baseline': 'deepseek-harness', 'run_id': 'run', 'turn_id': 'turn2',
        'interaction_index': 2, 'next_turn_index': 1}
    write_json(base / 'checkpoint.json', meta)
    write_json(base / 'completed_turns.json', prefix)
    write_json(post / 'report.json', {'run_id': 'run', 'stream_id': 'stream', 'condition': 'frozen',
        'baseline': 'deepseek-harness', 'finished_at': 'now', 'turns': prefix,
        'error': log.read_text(), 'token_budget': {'used': 12, 'calls': 2, 'limit': 100}})
    prior = {'turn_id': 'turn2', 'interaction_index': 1, 'score': .7, 'passed': False,
        'evaluation_status': 'failed', 'events': [{'seq': 1, 'type': 'assistant'}]}
    (post / 'trace.jsonl').write_text(json.dumps(prior) + '\n')
    bundle(base / 'workspace.zip', {'valid.py': 'result'})
    bundle(post / 'error_workspace.zip', {'valid.py': 'result'})
    bundle(base / 'baseline_state.zip', {'session.json': 'native', '.trievo_semantic_state.json': '{}'})
    bundle(post / 'baseline_state.zip', {'session.json': 'native'})
    if saved_reply:
        with pytest.raises(ValueError, match='saved reply'):
            derive(row, tmp_path)
        return
    cp = derive(row, tmp_path)
    result = json.loads((cp / 'completed_turns.json').read_text())
    assert result[0] == prefix[0]
    assert result[1]['turn_score'] == .7
    assert result[1]['preserved_interactions'] == [prior]
    assert result[1]['exception_accounting']['terminal_interaction_score'] == 0
    assert json.loads((cp / 'checkpoint.json').read_text())['next_turn_index'] == 2
    claim(row, cp)
    with pytest.raises(ValueError, match='consumed'):
        claim(row, cp)
