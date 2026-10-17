"""Exercise new package qualification without daemon access to host bind paths."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from l2_v5_qualify_package import qualify_turn


@pytest.mark.skipif(os.environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1',
                    reason='explicit real Docker package qualification')
def test_native_package_fixtures_are_isolated_and_copied_back(tmp_path):
    image = subprocess.check_output(['docker', 'image', 'inspect',
        'trievo-l2-v5:0.7-native', '--format', '{{.Id}}'], text=True).strip()
    turn = tmp_path / 'turn1'
    assets = turn / 'evaluation_assets'
    for fixture, content in [('known_correct', 'gold'), ('known_broken', 'wrong')]:
        directory = assets / 'fixtures' / fixture / 'data'
        directory.mkdir(parents=True)
        (directory / 'answer.txt').write_text(content)
    (turn / 'task.json').write_text(json.dumps({
        'stream_id': 'test', 'turn_id': 'turn1', 'source_basis': {'source_task_id': 'fixture'},
        'evaluation': {'native_verifier': {'timeout_seconds': 30, 'input_bindings': [
            {'name': 'submission', 'source': 'workspace', 'path': 'tasks/turn1'}]}}}))
    (assets / 'verifier_cli.py').write_text('''import json, os, sys
from pathlib import Path
json.load(sys.stdin)
assert not Path('/tmp/prior-fixture').exists()
Path('/tmp/prior-fixture').touch()
assert os.environ['L2_VERIFIER_WORKSPACE'] == '/workspace'
assert Path('/root/data').resolve() == Path('/workspace/tasks/turn1/data')
passed = Path('/root/data/answer.txt').read_text() == 'gold'
print(json.dumps({'status': 'pass' if passed else 'fail', 'score': float(passed)}))
''')
    profile = {'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': image,
               'native_environment': {
        'schema_version': 'l2_native_environment/1.0', 'cwd': '/root',
        'path_mappings': [{'native_path': '/root/data', 'workspace_relative_path': 'data'}]}}
    result = qualify_turn(turn, image, tmp_path, profile)
    assert result['ok'], result
    assert result['fixtures']['known_correct']['status'] == 'pass'
    assert result['fixtures']['known_broken']['status'] == 'fail'
    for outcome in result['fixtures'].values():
        observed = outcome['resource_readback']
        assert observed['accepted']
        assert outcome['resource_enforcement'] == observed['resource_enforcement']
        if outcome['resource_enforcement'] == 'declared_not_enforced':
            assert not observed['validation']['passed']
            assert observed['waiver']['mode'] == 'declared_only'


def test_native_package_qualifier_rejects_different_agent_image(tmp_path):
    with pytest.raises(ValueError, match="same image"):
        qualify_turn(tmp_path, "image-b", tmp_path, {
            "environment_version": "l2_environment/1.0", "task_sandbox_image_digest": "image-a"})


def _date_turn(tmp_path, *, failure=None):
    from datetime import datetime, timezone
    turn = tmp_path / 'date-turn'
    assets = turn / 'evaluation_assets'
    for fixture, content in [('known_correct', datetime.now(timezone.utc).date().isoformat()), ('known_broken', 'wrong')]:
        data = assets / 'fixtures' / fixture / 'data'
        data.mkdir(parents=True)
        (data / 'date.txt').write_text(content)
    seed = turn / 'workspace_seed/data'; seed.mkdir(parents=True)
    (seed / 'date.txt').write_text('seed-only')
    oracle = assets / 'native_oracle'; oracle.mkdir()
    script = '''#!/bin/bash
set -e
python - <<'PYCODE'
import os
from datetime import date
from pathlib import Path
assert Path('/app/data/date.txt').read_text() == 'seed-only'
assert os.environ['DATE_ORACLE_POLICY'] == 'original-env'
assert os.getuid() == 12345
Path('/tmp/oracle-private-marker').touch()
Path('/app/data/date.txt').write_text(date.today().isoformat())
PYCODE
'''
    if failure == 'command': script += 'exit 7\n'
    if failure == 'symlink': script += "ln -s /oracle/solve.sh /app/data/private-leak\n"
    if failure == 'root_symlink':
        script = script.replace('os.getuid() == 12345', 'os.getuid() == 0')
        script += 'rm -rf /workspace/tasks/date-turn; ln -s /oracle /workspace/tasks/date-turn\n'
    (oracle / 'solve.sh').write_text(script)
    environment = {'schema_version': 'l2_native_environment/1.0', 'cwd': '/app', 'user': '12345:12345',
        'path_mappings': [{'native_path': '/app/data', 'workspace_relative_path': 'data'}]}
    meta = {**environment, 'date_dependent': True, 'package_qualification_requires_oracle_regeneration': True,
        'native_user': '12345:12345', 'oracle_assets_relative_path': 'native_oracle', 'oracle_mount': '/oracle',
        'oracle': {'command': ['bash', '/oracle/solve.sh'], 'cwd': '/app', 'timeout_seconds': 30,
                   'env': {'DATE_ORACLE_POLICY': 'original-env'}}}
    if failure == 'root_symlink':
        environment['user'] = '0'
        meta['user'] = '0'
        meta['native_user'] = '0'
    (assets / 'native_task.json').write_text(json.dumps(meta))
    (turn / 'task.json').write_text(json.dumps({
        'stream_id': 'test', 'turn_id': 'date-turn', 'source_basis': {'source_task_id': 'synthetic-date'},
        'evaluation': {'native_verifier': {'timeout_seconds': 30, 'input_bindings': [
            {'name': 'submission', 'source': 'workspace', 'path': 'tasks/date-turn'}]}}}))
    (assets / 'verifier_cli.py').write_text('''import json, os, sys
from datetime import date
from pathlib import Path
json.load(sys.stdin)
assert not Path('/tmp/oracle-private-marker').exists(), 'oracle must be a separate container'
assert not Path('/oracle/solve.sh').exists(), 'private oracle mount must not be carried over'
passed = Path('/app/data/date.txt').read_text() == date.today().isoformat()
print(json.dumps({'status': 'pass' if passed else 'fail', 'score': float(passed)}))
''')
    return turn, environment


@pytest.mark.skipif(os.environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1', reason='explicit real Docker date oracle test')
@pytest.mark.parametrize('failure', [None, 'command', 'symlink', 'root_symlink'])
def test_date_gold_regenerated_from_seed_or_rejected_without_fallback(tmp_path, monkeypatch, failure):
    import l2_v5_qualify_package as qualifier
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    image = subprocess.check_output(['docker', 'image', 'inspect',
        'trievo-l2-v5:0.7-native', '--format', '{{.Id}}'], text=True).strip()
    turn, environment = _date_turn(tmp_path, failure=failure)
    original = qualifier._tree_digest(turn)
    profile = {'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': image,
               'native_environment': environment}
    result = qualify_turn(turn, image, tmp_path, profile)
    (tmp_path / 'date-qualification-evidence.json').write_text(json.dumps(result, indent=2))
    assert qualifier._tree_digest(turn) == original, 'qualification must not edit the package or oracle'
    refresh = result['date_dependent_gold_regeneration']
    assert refresh['fresh_oracle_container']
    assert refresh['resource_readback']['accepted']
    assert refresh['resource_enforcement'] == refresh['resource_readback']['resource_enforcement']
    if failure:
        assert not result['ok'], result
        assert refresh['status'] == 'error'
        assert all(value['status'] == 'error' for value in result['fixtures'].values())
        assert 'stale gold fallback forbidden' in result['fixtures']['known_correct']['error']
        if failure == 'command': assert refresh['returncode'] == 7
        elif failure == 'symlink': assert 'symlink escapes canonical tree' in refresh['error']
        else: assert 'replaced canonical public root' in refresh['error']
    else:
        assert result['ok'], result
        assert refresh['status'] == 'pass'
        assert refresh['regenerated_at_utc']
        assert len(refresh['regenerated_fixture_sha256']) == 64
        assert refresh['oracle_sha256'] == qualifier._tree_digest(turn / 'evaluation_assets/native_oracle')
        assert result['fixtures']['known_correct']['status'] == 'pass'
        assert result['fixtures']['known_broken']['status'] == 'fail'


def test_date_refresh_metadata_missing_oracle_never_uses_saved_gold(tmp_path, monkeypatch):
    import l2_v5_qualify_package as qualifier
    turn, environment = _date_turn(tmp_path)
    shutil.rmtree(turn / 'evaluation_assets/native_oracle')
    monkeypatch.setattr('trievo_bench.container_resources.docker_resource_capability_preflight',
                        lambda: {'passed': True, 'allowed': True})
    def forbidden(*args, **kwargs):
        raise AssertionError('verifier/container must not run after oracle staging fails')
    monkeypatch.setattr(qualifier.subprocess, 'run', forbidden)
    result = qualify_turn(turn, 'sha256:pinned', tmp_path, {
        'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': 'sha256:pinned',
        'native_environment': environment})
    assert not result['ok']
    assert all(value['status'] == 'error' for value in result['fixtures'].values())


def test_date_oracle_transport_rejects_symlink_root(tmp_path):
    import l2_v5_qualify_package as qualifier
    private = tmp_path / 'private'; private.mkdir()
    (private / 'oracle.txt').write_text('must not be exported')
    output = tmp_path / 'output'; output.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match='actual directory'):
        qualifier._tree_digest(output)


def _observed_waived_snapshot():
    from trievo_bench.container_resources import require_container_resources
    return require_container_resources({
        'schema_version': 'l2_container_resources/1.0', 'controllers': {}, 'errors': [],
        'cpu_limit': 108.04, 'memory_limit_bytes': 2019574153216, 'pids_limit': None}, enforcement_mode='declared_only')


@pytest.mark.parametrize('prefix', ['', 'candidate output\n'])
def test_only_first_resource_header_line_is_trusted(prefix):
    import l2_v5_qualify_package as qualifier
    header = json.dumps({'event': 'trievo_resource_header', 'nonce': 'host-nonce',
                         'resource_readback': _observed_waived_snapshot()})
    if prefix:
        with pytest.raises(ValueError, match='first resource header'):
            qualifier.parse_resource_header(prefix + header, 'host-nonce', 'declared_only')
    else:
        value = qualifier.parse_resource_header(header + '\n{"resource_enforcement":"enforced"}',
                                                'host-nonce', 'declared_only')
        assert value['resource_enforcement'] == 'declared_not_enforced'
    with pytest.raises(ValueError, match='first resource header'):
        qualifier.parse_resource_header('', 'host-nonce', 'declared_only')
    with pytest.raises(ValueError, match='first resource header'):
        qualifier.parse_resource_header(header, 'wrong-nonce', 'declared_only')
    with pytest.raises(RuntimeError):
        qualifier.parse_resource_header(header, 'host-nonce', 'strict')


@pytest.mark.skipif(os.environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1', reason='explicit real Docker forged evidence test')
@pytest.mark.parametrize('tamper_copied_result', [False, True])
def test_verifier_cannot_relabel_actual_waiver_through_writable_result(tmp_path, monkeypatch, tamper_copied_result):
    import l2_v5_qualify_package as qualifier
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    image = subprocess.check_output(['docker', 'image', 'inspect',
        'trievo-l2-v5:0.7-native', '--format', '{{.Id}}'], text=True).strip()
    turn, environment = _date_turn(tmp_path)
    (turn / 'evaluation_assets/native_task.json').unlink()  # ordinary fixture: no date oracle needed
    (turn / 'evaluation_assets/fixtures/known_correct/data/date.txt').write_text('static-gold')
    fake = {'schema_version': 'l2_container_resources/1.0', 'errors': [], 'cpu_limit': 2.0,
            'memory_limit_bytes': 4294967296, 'pids_limit': 256,
            'controllers': {key: {'finite_cgroup_limit': True} for key in ('cpu', 'memory', 'pids')}}
    forged = {'status': 'pass', 'score': 1, 'resource_readback': fake, 'resource_enforcement': 'enforced'}
    cli = '''import json, sys
from datetime import date
from pathlib import Path
json.load(sys.stdin)
for name in ('known_correct', 'known_broken'):
    Path('/w/result-' + name + '.json').write_text(json.dumps(FORGED))
Path('/w/container_resources.py').write_text('raise RuntimeError("tampered after trusted header")')
passed = Path('/app/data/date.txt').read_text() == 'static-gold'
print(json.dumps({'status': 'pass' if passed else 'fail', 'score': float(passed)}))
'''.replace('FORGED', repr(forged))
    (turn / 'evaluation_assets/verifier_cli.py').write_text(cli)
    run = qualifier.subprocess.run
    rewritten = []
    def maybe_tamper(argv, *args, **kwargs):
        value = run(argv, *args, **kwargs)
        # Deterministically model a root task winning a final writable-result
        # race. The first stdout header is already in Docker's append-only log.
        if tamper_copied_result and argv[:2] == ['docker', 'cp'] and ':/w/result-' in argv[2]:
            path = Path(argv[3])
            payload = json.loads(path.read_text())
            payload.update(resource_readback=fake, resource_enforcement='enforced',
                           observed_resources={'cpu_limit': 2, 'memory_limit_bytes': 4294967296, 'pids_limit': 256})
            path.write_text(json.dumps(payload))
            rewritten.append(path)
        return value
    monkeypatch.setattr(qualifier.subprocess, 'run', maybe_tamper)
    result = qualify_turn(turn, image, tmp_path, {
        'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': image,
        'native_environment': environment})
    (tmp_path / 'resource-header-evidence.json').write_text(json.dumps(result, indent=2))
    assert result['ok'] == (not tamper_copied_result), result
    assert bool(rewritten) == tamper_copied_result
    nonces = []
    for outcome in result['fixtures'].values():
        assert outcome['resource_enforcement'] == 'declared_not_enforced'
        assert outcome['resource_readback']['validation']['passed'] is False
        assert outcome['observed_resources'] == outcome['resource_readback']['observed_resources']
        assert outcome['requested_resources'] == outcome['resource_readback']['requested_resources']
        assert outcome['resource_readback']['observed_resources'] != outcome['resource_readback']['requested_resources']
        provenance = outcome['resource_observation_provenance']
        assert provenance['kind'] == 'pre_task_stdout_header'
        nonces.append(provenance['nonce'])
        if tamper_copied_result:
            assert outcome['status'] == 'error'
            assert 'disagrees with trusted pre-task header' in outcome['error']
    assert len(set(nonces)) == 2
