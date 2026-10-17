"""New tau profiles guard the actual simulator container before model calls."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from trievo_bench import container_resources as resources
from trievo_bench.tau2_live import launcher


def snapshot():
    return {"schema_version": resources.SCHEMA, "controllers": {}, "errors": [],
            "cpu_limit": 108.04, "memory_limit_bytes": 2019574153216, "pids_limit": None}


def session(tmp_path):
    assets = tmp_path / 'evaluation_assets'
    assets.mkdir()
    (assets / 'tau2_task.json').write_text('{}')
    (assets / 'tau2_meta.json').write_text(json.dumps({
        'domain': 'retail', 'task_id': 'test', 'revision': 'b7ea907', 'live_config': {}}))
    return launcher.Tau2LiveSession(profile={
        'task_sandbox_image_digest': 'sha256:pinned', 'environment_version': 'l2_environment/1.0',
        'live_session': {'kind': 'tau2_live_session', 'credential_env': []}},
        turn_dir=tmp_path, workspace_mount_spec='type=volume,src=test,dst=/workspace', timeout_seconds=10)


def test_strict_incapable_daemon_never_starts_service(tmp_path, monkeypatch):
    monkeypatch.delenv('TRIEVO_RESOURCE_ENFORCEMENT', raising=False)
    value = session(tmp_path)
    with patch.object(resources, 'docker_resource_capability_preflight', return_value={
            'passed': False, 'allowed': False, 'errors': ['no cgroup driver']}), patch.object(launcher, '_run') as run:
        with pytest.raises(launcher.Tau2LiveSessionError, match='resource enforcement unavailable'):
            value.start()
    run.assert_not_called()


@pytest.mark.parametrize('bad_nonce', [False, True])
def test_guarded_service_requires_real_snapshot_before_authorization(tmp_path, monkeypatch, bad_nonce):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    value = session(tmp_path)
    calls = []
    spec = {}
    authorized = False

    def fake(args, **kwargs):
        nonlocal authorized
        calls.append(args)
        if args[0] == 'cp' and args[2].endswith(':/private'):
            spec.update(json.loads((Path(args[1]) / 'spec.json').read_text()))
        elif args[0] == 'cp' and args[1].endswith(':/private/resources.json'):
            Path(args[2]).write_text(json.dumps({'nonce': 'stale' if bad_nonce else spec['nonce'],
                                                'resource_readback': snapshot()}))
        elif args[0] == 'cp' and args[2].endswith(':/private/authorization.json'):
            assert not bad_nonce
            authorized = True
        elif args[0] == 'cp' and args[1].endswith(':/private/ready.json'):
            assert authorized
            Path(args[2]).write_text(json.dumps({'nonce': spec['nonce']}))
        output = 'sha256:pinned true default' if args[0] == 'inspect' else ''
        return subprocess.CompletedProcess(args, 0, output, '')

    with patch.object(resources, 'docker_resource_capability_preflight', return_value={
            'passed': False, 'allowed': True, 'errors': ['no cgroup driver']}), \
            patch.object(launcher, '_run', side_effect=fake):
        if bad_nonce:
            with pytest.raises(launcher.Tau2LiveSessionError, match='nonce mismatch'):
                value.start()
            assert not authorized
            assert calls[-1][:2] == ['rm', '-f']
        else:
            evidence = value.start()
            assert evidence['resource_enforcement'] == 'declared_not_enforced'
            assert evidence['resource_readback']['validation']['passed'] is False
            assert evidence['observed_resources']['cpu_limit'] == 108.04
            assert evidence['requested_resources']['cpu_limit'] == 2
            create = calls[0]
            for argument in ('--cpus', '--memory', '--pids-limit'):
                assert argument in create
    assert spec['mode'] == 'declared_only'
    assert not any(args[0] == 'exec' for args in calls)


@pytest.mark.parametrize('mode,authorize', [('strict', False), ('declared_only', False), ('declared_only', True)])
def test_real_bootstrap_cannot_run_task_before_resource_authorization(tmp_path, mode, authorize, request):
    private = tmp_path / 'private'; private.mkdir()
    output = tmp_path / 'out'; output.mkdir()
    workspace = Path(tempfile.mkdtemp(prefix='tauguard-', dir='/tmp'))
    request.addfinalizer(lambda: shutil.rmtree(workspace, ignore_errors=True))
    task_marker = tmp_path / 'task-started'
    guard = Path(resources.__file__).read_text()
    guard += '\ndef read_container_resources():\n    return ' + repr(snapshot()) + '\n'
    (private / 'resource_guard.py').write_text(guard)
    command = [sys.executable, '-c',
        "import socket,time; from pathlib import Path; Path(" + repr(str(task_marker)) + ").touch(); "
        "s=socket.socket(socket.AF_UNIX); s.bind(" + repr(str(workspace / 'session.sock')) + "); time.sleep(.3)"]
    (private / 'spec.json').write_text(json.dumps({
        'nonce': 'current', 'mode': mode, 'command': command, 'ready_timeout': 1}))
    if authorize:
        (private / 'authorization.json').write_text('{"nonce":"current"}')
    bootstrap = launcher._GUARDED_SERVICE.replace('/private', str(private)).replace('/out', str(output))
    bootstrap = bootstrap.replace('/workspace/.tau2', str(workspace))
    (private / 'bootstrap.py').write_text(bootstrap)
    result = subprocess.run([sys.executable, str(private / 'bootstrap.py')], capture_output=True, text=True, timeout=5)
    assert task_marker.exists() == (mode == 'declared_only' and authorize), result.stderr
    assert result.returncode == (0 if authorize else 1)
    if mode == 'declared_only':
        readback = json.loads((private / 'resources.json').read_text())['resource_readback']
        assert readback['resource_enforcement'] == 'declared_not_enforced'
        assert 'declared_not_enforced' in (output / 'service.log').read_text()
    else:
        assert not (private / 'resources.json').exists()


def test_noop_copy_cannot_authorize_simulator(tmp_path, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    value = session(tmp_path)
    calls = []
    def noop(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, 'false' if args[0] == 'inspect' else '', '')
    with patch.object(resources, 'docker_resource_capability_preflight', return_value={
            'passed': False, 'allowed': True, 'errors': ['no cgroup driver']}), \
            patch.object(launcher, '_run', side_effect=noop):
        with pytest.raises(launcher.Tau2LiveSessionError, match='exited before resources'):
            value.start()
    assert not any(args[0] == 'cp' and args[2].endswith(':/private/authorization.json') for args in calls)
    assert calls[-1][:2] == ['rm', '-f']


@pytest.mark.skipif(__import__('os').environ.get('TRIEVO_TEST_TAU2_DOCKER') != '1',
                    reason='explicit existing-image Docker guard-only service test')
def test_actual_existing_container_service_guard_without_models(tmp_path, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    image = subprocess.check_output(['docker', 'image', 'inspect', 'trievo-l2-v5:0.7-native',
                                     '--format', '{{.Id}}'], text=True).strip()
    # Exercise the real startup authorization/marker transport. Only the final
    # simulator command is replaced with a local Unix socket, never a model.
    fake_service = ("import socket,time; from pathlib import Path; "
                    "Path('/workspace/.tau2').mkdir(parents=True,exist_ok=True); "
                    "s=socket.socket(socket.AF_UNIX); s.bind('/workspace/.tau2/session.sock'); time.sleep(30)")
    replacement = '    spec["command"] = ["python", "-c", ' + repr(fake_service) + ']\n'
    monkeypatch.setattr(launcher, '_GUARDED_SERVICE', launcher._GUARDED_SERVICE.replace(
        '    process = subprocess.Popen(', replacement + '    process = subprocess.Popen('))
    monkeypatch.setattr(launcher, 'SERVICE_PYTHON', 'python')
    value = session(tmp_path)
    value.image = image
    volume = 'trievo-tau2-guard-test-' + __import__('uuid').uuid4().hex
    subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
    value.workspace_mount_spec = 'type=volume,src=' + volume + ',dst=/workspace'
    try:
        evidence = value.start()
        assert evidence['resource_enforcement'] == 'declared_not_enforced'
        assert evidence['resource_readback']['validation']['passed'] is False
        assert evidence['observed_resources'] != evidence['requested_resources']
        assert 'declared_not_enforced' in value._service_log()
        (tmp_path / 'service-resource-evidence.json').write_text(json.dumps(evidence, indent=2))
    finally:
        value.close()
        subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)
