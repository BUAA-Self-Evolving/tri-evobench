import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from trievo_bench.arena_runtime import ArenaVerifier, apply_updates
from trievo_bench.verifier import CheckResult, L1LifeJudge


def test_empty_source_rubrics_are_explicit():
    class Client:
        def complete(self, messages, **kwargs):
            assert 'source_rubric_scores MUST be []' in messages[-1]['content']
            return json.dumps({'stream_id': 's', 'turn_id': 't', 'check_scores': [], 'source_rubric_scores': []})
    result = L1LifeJudge(Client()).evaluate({'stream_id': 's', 'turn_id': 't', 'judge_prompt': '{rubrics}'}, '', None)
    assert result.details['type'] == 'l1_life_llm_judge'


def test_updates_and_session_registration(tmp_path):
    stream = tmp_path / 'stream'
    stream.mkdir()
    (stream / 'payload').write_bytes(b'abc')
    workspace = tmp_path / 'work'
    workspace.mkdir()
    apply_updates({'arena_updates': [{'stream_dir': str(stream), 'type': 'session', 'files': [{
        'payload': 'payload', 'target': 'new.json', 'action': 'new',
        'sha256': hashlib.sha256(b'abc').hexdigest(),
        'session_index': {'register_if_missing': True, 'session_id': 'new', 'session_file': 'new.json', 'channel': 'test'}
    }]}]}, workspace)
    assert (workspace / '.session_store/new.json').read_bytes() == b'abc'
    assert json.loads((workspace / '.session_store/sessions.json').read_text())['new']['channel'] == 'test'


def test_mandatory_preference_cannot_pass_on_weighted_threshold(tmp_path, monkeypatch):
    (tmp_path / 'answer').touch()
    evaluation = {
        'primary_evaluation': {'type': 'deterministic_exec_check'},
        'exec_check': {'command': 'check_q1.py'},
        'preference_check': {'command': 'check_preferences.py', 'rules': 'P1', 'target': 'answer'},
        'checks': [{'check_id': 'task_execution', 'dimension': 'task_success', 'weight': .8},
                   {'check_id': 'preference_adherence', 'dimension': 'preference_adherence', 'weight': .2}],
        'success_policy': {'turn_score_threshold': .8, 'mandatory_checks_must_pass': ['task_execution', 'preference_adherence']},
    }
    monkeypatch.setattr(ArenaVerifier, '_exec', staticmethod(lambda d, *a: CheckResult(0 if 'preferences' in d['command'] else 1, 'preferences' not in d['command'], '', {})))
    result = ArenaVerifier().evaluate({'evaluation': evaluation}, '', SimpleNamespace(workspace_path=lambda: tmp_path), {})
    assert result.score == .8 and not result.passed


def test_distractor_rejects_persistence():
    evaluation = {'primary_evaluation': {'type': 'deterministic_response_check', 'expected_response': 'OK'},
                  'checks': [{'check_id': 'response_accuracy', 'dimension': 'task_success', 'weight': .7},
                             {'check_id': 'state_hygiene', 'dimension': 'state_hygiene', 'weight': .3}],
                  'success_policy': {'turn_score_threshold': 1, 'mandatory_checks_must_pass': ['response_accuracy', 'state_hygiene']}}
    result = ArenaVerifier().evaluate({'evaluation': evaluation}, 'OK', None, {'state_before': {}, 'state_after': {'leak': 1}})
    assert result.score == .7 and not result.passed
