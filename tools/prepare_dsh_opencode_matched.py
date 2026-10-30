"""Prepare an independent, predeclared 87-cell DSH reference-profile run."""
from pathlib import Path
import hashlib
import json
import subprocess

from tools.run_new_package_matrix import jobs, load, now, save

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / 'runs/dsh-deepseek-v4-flash-opencode-matched-20261016'
ORIGINAL = REPO / 'runs/dsh-deepseek-v4-flash-full-20261011'
REPAIR = REPO / 'runs/dsh-deepseek-v4-flash-repair-20261015'
REFERENCE = REPO.parent / 'tri-evobench-offline-opencode-dcbcc00/runs/harness-full-stream-20260928'
IMAGE = 'trievo-deepseek-harness:opencode-matched-20261016'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare():
    if ROOT.exists():
        raise SystemExit('root exists; inspect rather than prepare twice')
    reference_path = REFERENCE / 'config/tmp_opencode_docker.override.json'
    reference = load(reference_path)
    old_plan = load(ORIGINAL / 'run_plan.json')
    revision = subprocess.check_output(['git', '-C', str(REPO / 'baseline/deepseek-harness'),
                                        'rev-parse', 'HEAD', 'HEAD^{tree}'], text=True).split()
    assert revision == ['d743267388641bc76f17c45ce8b4c231aed1d32c',
                        '75a3d23c8b6db770a000081918c4b975912a9c39']
    # The container envelope matches the reference; --init only reaps children.
    command = [part.replace('trievo-opencode-', 'trievo-deepseek-harness-')
               for part in reference['adapter']['command']]
    command[command.index('run') + 1:command.index('run') + 1] = ['--init']
    command[-1] = '{image}'
    options = {key: reference['options'][key] for key in
               ('model', 'base_url', 'api_key_env', 'max_output_tokens',
                'request_timeout_seconds', 'l2_agent_limits')}
    options.update(image=IMAGE, execution_profile='opencode-full-stream-20260928',
                   provider_transport='friday_chat', reasoning=True, reasoning_effort=None,
                   context_window=128000, upstream_timeout_seconds=900,
                   patches=['/opt/trievo/baseline/adapters/deepseek_harness/opencode_reference.patch.yml'])
    override = {'adapter': {'command': command}, 'options': options,
                'state': {'adapter_path': '/state', 'enforce_turn_freeze': False,
                          'paths': ['learning/**/*', 'session.json', 'sessions/dsh/sessions/**/*']},
                'capabilities': ['direct_workspace', 'terminal', 'tool_calls', 'usage',
                                 'evolution_barrier', 'persistent_session'],
                'evolution': {'policy': 'disabled'}}
    rows = jobs(REPO, ROOT, None, l1_package=Path(old_plan['l1_package']),
                l2_package=Path(old_plan['l2_package']), baselines=['deepseek-harness'],
                l1_conditions=('reset', 'frozen', 'evolving'),
                l2_conditions=('reset', 'frozen', 'evolving'))
    assert len(rows) == 87 and sum(len(row['turns']) for row in rows) == 1671
    ROOT.mkdir()
    save(ROOT / 'config/tmp_deepseek-harness_docker.override.json', override)
    save(ROOT / 'config/provider-profile.json', load(ORIGINAL / 'config/provider-profile.json'))
    files = [reference_path, REPO / 'baseline/manifests/deepseek-harness.json',
             ROOT / 'config/tmp_deepseek-harness_docker.override.json',
             REPO / 'trievo_bench/verifier.py',
             *sorted((REPO / 'baseline/adapters/deepseek_harness').glob('*.py')),
             REPO / 'baseline/adapters/deepseek_harness/session_resume.mjs',
             REPO / 'baseline/adapters/deepseek_harness/opencode_reference.patch.yml']
    hashes = {str(path): digest(path) for path in files}
    for row in rows:
        output = Path(row['output'])
        save(output / 'fresh_run_authorization.json', {
            'status': 'authorized', 'max_attempts': 1, 'job_id': row['id'],
            'output': str(output), 'created_at': now(), 'source_sha256': hashes,
            'authorization': 'user: 修复第一个问题 然后让dsh的配置和之前测过的opencode保持一致 重测l1 l2的各自三个状态',
            'semantics': 'new complete experiment; no old prefixes; no best-score selection'})
    save(ROOT / 'matrix_status.json', {'status': 'prepared', 'pid': 0, 'jobs': rows})
    save(ROOT / 'run_plan.json', {
        'created_at': now(), 'model': 'deepseek-v4-flash', 'baseline': 'deepseek-harness',
        'expected_cells': 87, 'expected_turns': 1671, 'concurrency': 6,
        'reference_root': str(REFERENCE), 'original_roots_read_only': [str(ORIGINAL), str(REPAIR)],
        'selection': 'all 87 predeclared new attempts; no old/new splicing or score-based selection',
        'conditions': ['reset', 'frozen', 'evolving'], 'source_revision_tree': revision,
        'source_sha256': hashes, 'l1_package': old_plan['l1_package'],
        'l2_package': old_plan['l2_package'], 'dataset': 'L2_Demo_V2; V5 unavailable',
        'harness_learning': 'native learner unsupported; barrier skipped; session/workspace persist where condition allows',
        'runtime_alignment': {'max_output_tokens': 4096, 'context_window': 128000,
            'thinking': 'enabled', 'session': 'one_per_flow', 'terminal_timeout_seconds': 90,
            'l1_native_episode_seconds': 660, 'l2_native_episode_seconds': 900,
            'provider_request_interval_seconds': 0, 'bridge_model_call_cap': None,
            'l2_agent_limits': options['l2_agent_limits'],
            'transport_teardown_grace_seconds': 60},
        'known_limits': [
            'Reference OpenCode L1 has only a persistent-flow condition, not three-condition L1 results.',
            'Native tools/system prompts/retries remain baseline-specific; equal settings are not identical algorithms.',
            'DSH SDK durable-session compatibility plugin calls native agents.resume before any prompt.',
            'No new paid preflight; real requests belong to the new declared matrix.'],
        'aggregation': {'primary': 'pooled scored probes as in old OpenCode; L1 all turns, L2 explicit probes',
                        'secondary': 'equal-stream macro', 'pending': 'excluded, never zero-filled'},
        'job_error_policy': 'continue', 'max_attempts_per_cell': 1, 'jobs': rows})
    print(json.dumps({'root': str(ROOT), 'cells': len(rows), 'turns': 1671}))


if __name__ == '__main__':
    prepare()
