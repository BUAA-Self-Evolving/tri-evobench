import json
import zipfile
from dataclasses import replace

import pytest

from trievo_bench.baselines.base import BaselineReply, EvolutionResult, UsageDetails
from trievo_bench.l2_agent import L2AgentRunError, L2AgentRunOptions, L2AgentStreamRunner


def test_resume_judge_failure_reuses_paid_reply_and_completed_prefix(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    stream = package / 'streams/s'
    (stream / 'agent_workspace').mkdir(parents=True)
    manifest = {
        'schema_version': 'l2_taskstream/1.0',
        'stream_id': 's',
        'agent_workspace': 'agent_workspace',
        'ordering': {'turn_order': ['turn1', 'turn2']},
        'execution_requirements': {
            'network': 'disabled', 'path_scope': 'agent_workspace',
            'max_interactions': 3, 'max_tool_calls_per_interaction': 20,
            'turn_timeout_seconds': 900, 'max_output_tokens': 12000,
        },
        'turns': [
            {'turn_id': turn, 'turn_index': index, 'sequence_role': role}
            for index, (turn, role) in enumerate(
                [('turn1', 'learning'), ('turn2', 'probe')], start=1
            )
        ],
    }
    (stream / 'stream.json').write_text(json.dumps(manifest))
    for index, (turn, role) in enumerate(
        [('turn1', 'learning'), ('turn2', 'probe')], start=1
    ):
        (stream / turn).mkdir()
        (stream / turn / 'task.json').write_text(json.dumps({
            'stream_id': 's', 'turn_id': turn, 'turn_index': index,
            'sequence_role': role,
            'agent_input': {'messages': [{'role': 'user', 'content': 'write result'}]},
            'workspace_transition': {'mode': 'continue'}, 'evaluation': {'success_policy': {'turn_score_threshold': 1}},
        }))
    calls = []
    class Runtime:
        def start_flow(self, context):
            self.workspace = context.workspace
        def start_turn(self, context):
            self.turn = context.turn_id
        def interact(self, context):
            calls.append(self.turn)
            (self.workspace / self.turn).write_text('real artifact')
            return BaselineReply(text=self.turn, usage=UsageDetails(total_tokens=100, api_calls=1))
        def save_checkpoint_state(self, path):
            with zipfile.ZipFile(path, 'w'):
                pass
        def evolve(self, context):
            return EvolutionResult()
        def end_flow(self):
            pass
        def metadata(self):
            return {}
    monkeypatch.setattr(L2AgentStreamRunner, '_create_runtime', lambda *a: Runtime())
    monkeypatch.setattr(L2AgentStreamRunner, '_verify_taskstream', lambda *a, **k: {'category': 'pass', 'score': 1, 'native_result': {'status': 'pass'}})
    def judge(self, task, reply, *args):
        if task['turn_id'] == 'turn2':
            raise ValueError('injected judge parser failure')
        return {'score': 1}
    monkeypatch.setattr(L2AgentStreamRunner, '_judge_taskstream', judge)
    options = L2AgentRunOptions(package, 's', tmp_path / 'out', 'hermes', {}, 10000, 'test', 'test', tmp_path)
    with pytest.raises(L2AgentRunError, match='injected'):
        L2AgentStreamRunner(options).run()
    pending = next((tmp_path / 'out/_checkpoints').rglob('p0001-01'))
    assert json.loads((pending / 'completed_turns.json').read_text())[0]['turn_id'] == 'turn1'
    monkeypatch.setattr(L2AgentStreamRunner, '_judge_taskstream', lambda *a: {'score': 1})
    report = L2AgentStreamRunner(replace(options, resume_checkpoint=pending)).run()
    assert report['success']
    assert calls == ['turn1', 'turn2']  # neither prefix nor paid failed-judge reply rerun
    assert [t['turn_id'] for t in report['turns']] == ['turn1', 'turn2']
    assert report['turns'][1]['interaction_count'] == 1
