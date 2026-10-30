import json
import pytest

from tools.summarize_dsh_matched_partial import select, terminal_score
from tools.recover_dsh_reset_exceptions import write_json


def test_terminal_failure_preserves_valid_score_and_excludes_future_turn(tmp_path):
    output = tmp_path / 'cell'
    folder = output / 'flow'
    folder.mkdir(parents=True)
    cp = output / '_checkpoints' / 't0001'
    cp.mkdir(parents=True)
    prefix = [{'turn_id': 't1', 'score': 1.0}]
    write_json(cp / 'completed_turns.json', prefix)
    write_json(cp / 'checkpoint.json', {'condition': 'evolving', 'flow_id': 'flow', 'next_turn_index': 1})
    records = [{'turn_id': 't2', 'index': 1, 'score': .7, 'response': 'already judged'},
        {'turn_id': 't2', 'index': 2, 'score': 0.0, 'response': '', 'events': [], 'tool_calls': [],
         'verifier_details': {'type': 'model_error', 'error': 'DeepSeek Harness turn failed: error: JSONDecodeError'}}]
    (folder / 'live_trace.jsonl').write_text('\n'.join(json.dumps(r) for r in records))
    row = {'level': 'l1', 'condition': 'evolving', 'flow_id': 'flow', 'agent': 'deepseek-harness',
           'status': 'paused_error', 'output': str(output), 'turns': ['t1', 't2', 't3']}
    turns, _, terminal = select(row)
    assert turns[0] == prefix[0]
    assert terminal['score'] == .7 and terminal['terminal_attempt_score'] == 0
    assert [r['turn_id'] for r in turns] == ['t1', 't2']
    assert not list(output.rglob('report.json'))


def test_invalid_scores_are_not_silently_aggregated():
    for score in [float('nan'), float('inf'), -1, 2]:
        with pytest.raises(ValueError):
            terminal_score([{'score': score}])
