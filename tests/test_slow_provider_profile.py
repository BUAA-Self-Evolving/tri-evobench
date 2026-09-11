import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trievo_bench.l2_agent import _work_limits, _effective_work_limits, L2AgentRunError
from trievo_bench.baselines.hermes_bridge import (
    _parse_l2_agent_limits, _configure_slow_provider_agent, _install_slow_provider_policy,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('slow_config', ROOT / 'tools/configure_slow_provider.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_profile_reaches_runtime_and_all_container_clients(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    original = {'adapter': {'command': ['docker', 'run', '-i', 'image:v1']},
                'options': {'l2_agent_limits': {'max_model_calls': 24, 'max_tool_calls': 20,
                    'max_output_tokens': 12000, 'episode_timeout_seconds': 900}}}
    for name in module.FILES.values():
        (source / name).write_text(json.dumps(original))
    module.configure(source, tmp_path / 'new')
    for name in module.FILES.values():
        result = json.loads((tmp_path / 'new' / name).read_text())
        assert json.loads((source / name).read_text()) == original
        assert result['environment']['set']['TRIEVO_SLOW_PROVIDER'] == '1'
        assert 'TRIEVO_SLOW_PROVIDER' in result['adapter']['command']
        limits = _work_limits(result)
        requirements = {'turn_timeout_seconds': 900, 'max_tool_calls_per_interaction': 20,
                        'max_output_tokens': 12000}
        assert _effective_work_limits(limits, requirements).episode_timeout_seconds == 900
        effective = _effective_work_limits(limits, requirements, slow_provider=True)
        assert effective.episode_timeout_seconds == 21600
        assert effective.evolution_timeout_seconds == 14400
        assert effective.max_tool_calls == 20 and effective.max_model_calls == 24
        assert effective.max_output_tokens == 12000
        metadata = {'slow_provider': True, 'agent_limits': vars(effective)}
        assert _parse_l2_agent_limits(metadata)['episode_timeout_seconds'] == 21600
    with pytest.raises(ValueError, match='empty'):
        module.configure(source, tmp_path / 'new')


def test_long_timeouts_are_opt_in():
    with pytest.raises(L2AgentRunError):
        _work_limits({'options': {'l2_agent_limits': {'episode_timeout_seconds': 21600}}})
    with pytest.raises(ValueError, match='outer budgets'):
        module.configure(Path('unused'), Path('unused2'), evolution_timeout=120)


def test_hermes_foreground_and_review_instances_share_policy(monkeypatch):
    monkeypatch.setenv('TRIEVO_API_TIMEOUT_SECONDS', '600')
    monkeypatch.setenv('TRIEVO_API_MAX_RETRIES', '20')
    class Agent:
        def __init__(self, name):
            self.name = name
            self.client = SimpleNamespace(max_retries=2)
            self._api_max_retries = 4
            self._resolved_api_call_timeout = lambda: 150
            self._resolved_api_call_stale_timeout_base = lambda: (90, True)
    _install_slow_provider_policy(Agent)
    constructor = Agent.__init__
    _install_slow_provider_policy(Agent)
    assert Agent.__init__ is constructor
    for agent in (Agent('foreground'), Agent('review')):
        assert agent._api_max_retries == 21
        assert agent.client.max_retries == 0
        assert agent._resolved_api_call_timeout() == 600
        assert agent._resolved_api_call_stale_timeout_base() == (600, False)


def test_mixed_timing_results_cannot_produce_matched_gain(monkeypatch):
    from tools import run_new_package_matrix as matrix
    rows = [{'level': 'l2', 'agent': 'skillopt', 'condition': c}
            for c in ('reset', 'frozen', 'evolving')]
    def report_for(row):
        return None, {'turns': [{'timing_profile': 'dataset' if row['condition'] == 'reset' else 'slow_provider'}]}
    monkeypatch.setattr(matrix, 'report_for', report_for)
    assert matrix.matched_l2_aggregations(rows) == {}
