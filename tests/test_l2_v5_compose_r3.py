"""Composition availability must never imply native red/green admission."""
import hashlib
import json
from pathlib import Path

import pytest

from tools import l2_v5_codebook as codebook
from tools import l2_v5_compose as compose
from tools.l2_v5_native_sources import tree_sha256


def _record(root, sid='skillsbench:example', cap='skillsbench.natural-science'):
    task = root / sid.replace(':', '__')
    (task / 'environment').mkdir(parents=True)
    (task / 'environment/Dockerfile').write_text('FROM python:3.12-slim\n')
    (task / 'evaluation_assets/native_tests').mkdir(parents=True)
    (task / 'evaluation_assets/native_tests/test.sh').write_text('exit 1\n')
    return task, {
        'source_id': sid,
        'source': {'benchmark_id': 'skillsbench'},
        'capability_hint': {'capability_id': cap, 'category': 'natural-science'},
        'runtime': {'turn_timeout_seconds': 600, 'network': 'disabled'},
        'environment': {
            'kind': 'dockerfile', 'recipe_path': 'environment/Dockerfile',
            'context_path': 'environment',
            'recipe_sha256': hashlib.sha256((task / 'environment/Dockerfile').read_bytes()).hexdigest(),
            'context_sha256': tree_sha256(task / 'environment'),
            'build_contexts': {'trievo_verifier': {
                'path': 'evaluation_assets/native_tests',
                'sha256': tree_sha256(task / 'evaluation_assets/native_tests')}},
        },
        'qualification': {'admitted': False, 'stage': 'recipe_ready'},
    }


def test_recipe_availability_preserves_unverified_status(tmp_path):
    task, record = _record(tmp_path / 'pool/native')
    (task / 'pool_task.json').write_text(json.dumps(record))
    available = compose.load_pool(tmp_path / 'pool')
    assert available[record['source_id']]['qualification'] == record['qualification']
    profile = compose.runtime_profile(available[record['source_id']])
    assert profile['image_build_required'] and profile['runtime_materialization_required']
    assert profile['source_id'] == record['source_id']
    assert profile['task_sandbox_image_digest'] is None
    assert profile['verifier_image_digest'] is None
    assert profile['resource_enforcement'] == 'declared_not_enforced'
    record['qualification']['stage'] = 'deferred_download'
    assert not compose.composition_eligible(record)
    record['qualification']['stage'] = 'recipe_ready'
    del record['environment']
    assert not compose.composition_eligible(record)


def test_codebook_counts_recipes_separately(tmp_path, monkeypatch):
    task, record = _record(tmp_path / 'pool/native')
    (task / 'pool_task.json').write_text(json.dumps(record))
    target = tmp_path / 'codebook.json'
    monkeypatch.setattr('sys.argv', ['codebook', '--pool', str(tmp_path / 'pool'), '--out', str(target)])
    assert codebook.main() == 0
    cap = json.loads(target.read_text())['capabilities'][0]
    assert cap['admitted_pool_tasks'] == 0
    assert cap['recipe_ready_pool_tasks'] == cap['composition_eligible_pool_tasks'] == 1
    assert cap['domain_id'] == 'natural_science'


def test_copy_recipe_and_private_context_without_fabricated_fixtures(tmp_path):
    task, record = _record(tmp_path / 'pool/native')
    record['_dir'] = str(task)
    destination = tmp_path / 'turn1'
    compose.copy_pool_task(record, destination)
    assert tree_sha256(destination / 'environment') == record['environment']['context_sha256']
    assert (destination / 'evaluation_assets/native_tests/test.sh').read_text() == 'exit 1\n'
    assert not (destination / 'evaluation_assets/source_verifier.py').exists()
    assert not list((destination / 'evaluation_assets/fixtures').iterdir())
    (task / 'environment/Dockerfile').write_text('FROM broken\n')
    with pytest.raises(ValueError, match='Dockerfile hash mismatch'):
        compose.copy_pool_task(record, tmp_path / 'turn2')


@pytest.mark.parametrize('problem', ['missing', 'wrong_capability', 'reused', 'duplicate'])
def test_plan_rejects_invalid_pinned_selection(tmp_path, problem):
    task, record = _record(tmp_path / 'pool/native')
    (task / 'pool_task.json').write_text(json.dumps(record))
    cap = record['capability_hint']['capability_id']
    stream = {'stream_id': 'intra_example', 'pool_id': 'intra_benchmark_multi_capability',
              'schedule': 'blocked', 'tasks_per_capability': 1, 'capabilities': [cap],
              'pinned': {cap: [record['source_id']]}}
    spec = {'package_id': 'test', 'seed': 7, 'streams': [stream]}
    if problem == 'missing':
        stream['pinned'][cap] = ['skillsbench:missing']
    elif problem == 'wrong_capability':
        record['capability_hint']['capability_id'] = 'another'
        (task / 'pool_task.json').write_text(json.dumps(record))
    elif problem == 'duplicate':
        stream['pinned'][cap] *= 2
    else:
        spec['streams'].append({**stream, 'stream_id': 'intra_second'})
    spec_path, book_path = tmp_path / 'spec.json', tmp_path / 'book.json'
    spec_path.write_text(json.dumps(spec))
    book_path.write_text(json.dumps({'taxonomy_version': 'v', 'capabilities': [{'capability_id': cap}]}))
    with pytest.raises(ValueError, match='pinned source'):
        compose.plan(tmp_path / 'pool', book_path, spec_path, tmp_path / 'plan.json')


def test_r3_spec_has_only_intra_and_retained_autoresearch():
    spec = json.loads((Path(__file__).parents[1] / 'tools/l2_v5_stream_spec.json').read_text())
    streams = spec['streams']
    assert len(streams) == 21
    assert all(not s['stream_id'].startswith('cross_') for s in streams)
    assert sum(3 * s.get('tasks_per_capability', 5) for s in streams) == 297
    theorem = next(s for s in streams if s['stream_id'] == 'intra_theoremqa_delayed_01')
    ids = {sid for values in theorem['pinned'].values() for sid in values}
    assert 'sr_agents:theoremqa_00292' in ids
    assert not ids & {'sr_agents:theoremqa_00048', 'sr_agents:theoremqa_00135'}


def test_stale_verifier_cannot_clear_pending_task_materialization(tmp_path):
    task, record = _record(tmp_path / 'pool/native')
    (task / 'evaluation_assets/source_verifier.py').write_text('# stale, not materialized\n')
    record.update(prompt='Native prompt', evaluation_profile='general',
                  submission={'mode': 'workspace_artifact', 'payload_kind': 'directory', 'relative_path': ''})
    record['source'].update(source_task_id='example', source_hash='a' * 64)
    (task / 'pool_task.json').write_text(json.dumps(record))
    second, other = _record(tmp_path / 'pool/native', sid='skillsbench:second')
    other.update(prompt=record['prompt'], evaluation_profile='general', submission=record['submission'])
    other['source'].update(source_task_id='second', source_hash='b' * 64)
    (second / 'pool_task.json').write_text(json.dumps(other))
    cap = record['capability_hint']['capability_id']
    book = {'taxonomy_version': 'test', 'capabilities': [{
        'capability_id': cap, 'benchmark_id': 'skillsbench', 'domain_id': 'natural_science',
        'task_type_ids': ['scientific_data_analysis']}]}
    plan = {'package_id': 'test', 'seed': 7, 'taxonomy_version': 'test', 'streams': [{
        'stream_id': 'intra_test', 'pool_id': 'intra_benchmark_multi_capability',
        'schedule': 'blocked', 'tasks_per_capability': 2,
        'capabilities': [{'capability_id': cap, 'source_ids': [record['source_id'], other['source_id']]}]}]}
    book_path, plan_path = tmp_path / 'book.json', tmp_path / 'plan.json'
    book_path.write_text(json.dumps(book))
    plan_path.write_text(json.dumps(plan))
    output = tmp_path / 'package'
    compose.compose(tmp_path / 'pool', plan_path, book_path, output, None)
    composed = json.loads((output / 'streams/intra_test/turn1/task.json').read_text())
    assert composed['runtime_materialization_required'] is True
    assert composed['qualification']['admitted'] is False


def test_materialized_native_copy_refreshes_stale_adapter_not_tests(tmp_path):
    from tools.l2_v5_native_environment import verifier_text
    task, record = _record(tmp_path / 'pool/native')
    record['_dir'] = str(task)
    record['runtime']['native_environment'] = {'schema_version': 'l2_native_environment/1.0'}
    (task / 'evaluation_assets/source_verifier.py').write_text('# stale adapter\n')
    target = tmp_path / 'turn1'
    compose.copy_pool_task(record, target)
    assert (target / 'evaluation_assets/source_verifier.py').read_text() == verifier_text()
    assert (target / 'evaluation_assets/native_tests/test.sh').read_text() == 'exit 1\n'
    assert (task / 'evaluation_assets/source_verifier.py').read_text() == '# stale adapter\n'


def test_verifier_cli_preserves_source_error_diagnostics(tmp_path):
    import subprocess
    import sys
    assets = tmp_path / 'turn/evaluation_assets'
    assets.mkdir(parents=True)
    (assets.parent / 'task.json').write_text(json.dumps({'evaluation': {'native_verifier': {
        'verifier_id': 'native-test',
        'input_bindings': [{'name': 'submission', 'source': 'interaction_record', 'path': ''}],
    }}}))
    (assets / 'source_verifier.py').write_text(
        "def verify(submission, turn_dir):\n"
        "    return {'status': 'error', 'score': None, 'details': "
        "{'infrastructure_error': 'ModuleNotFoundError', 'output_tail': 'missing dependency'}}\n")
    (assets / 'verifier_cli.py').write_text(compose.VERIFIER_CLI)
    request = {'schema_version': 'l2_native_verifier_request/1.0',
               'bindings': {'submission': {'kind': 'inline', 'value': 'answer'}}}
    proc = subprocess.run([sys.executable, str(assets / 'verifier_cli.py')],
                          input=json.dumps(request), text=True, capture_output=True, check=True)
    result = json.loads(proc.stdout)
    assert result['status'] == 'error' and result['score'] is None
    assert result['error'] == 'ModuleNotFoundError'
    assert result['private_diagnostics']['output_tail'] == 'missing dependency'
    assert result['checks'] == []
