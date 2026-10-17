"""SRA r3 admission checks must reject infra failures masquerading as red."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import l2_v5_sra_r3 as builder  # noqa: E402


@pytest.fixture(autouse=True)
def strict_resource_mode_by_default(monkeypatch):
    monkeypatch.delenv("TRIEVO_RESOURCE_ENFORCEMENT", raising=False)


def evidence(status, failures=0, errors=0, started=2, execution_errors=0):
    return {"transport_verified": True,
            "resource_readback": {"schema_version": "l2_container_resources/1.0", "cpu_limit": 2.0, "memory_limit_bytes": 4294967296, "pids_limit": 256,
                                  "errors": [], "controllers": {key: {"finite_cgroup_limit": True}
                                      for key in ['cpu', 'memory', 'pids']}},
            "verifier": {"status": status, "score": int(status == "pass"), "details": {"result": status}},
            "tests_started": started, "tests_completed": started, "tests_successful": max(0, started - failures - errors),
            "assertion_failures": failures,
            "test_errors": errors, "execution_errors": execution_errors}


def test_red_requires_assertion_and_collected_tests():
    authority = evidence("fail")
    assert builder.diagnostic_valid(authority, evidence("fail", failures=1), red=True)
    for invalid in [evidence("fail", errors=1), evidence("fail", started=0),
                    evidence("fail", failures=1, errors=1),
                    evidence("fail", failures=1, execution_errors=1),
                    evidence("pass", failures=1)]:
        assert not builder.diagnostic_valid(authority, invalid, red=True)
    assert not builder.diagnostic_valid(evidence("pass"), evidence("fail", failures=1), red=True)
    ghost = evidence("fail", failures=1)
    ghost["transport_verified"] = False
    assert not builder.diagnostic_valid(authority, ghost, red=True)


def test_green_rejects_empty_collection_and_hidden_errors():
    assert builder.diagnostic_valid(evidence("pass"), evidence("pass"), red=False)
    assert not builder.diagnostic_valid(evidence("pass"), evidence("pass", started=0), red=False)
    assert not builder.diagnostic_valid(evidence("pass"), evidence("pass", errors=1), red=False)
    skipped = evidence("pass")
    skipped["tests_successful"] = 0
    assert not builder.diagnostic_valid(evidence("pass"), skipped, red=False)


def test_output_guard_protects_old_pool_and_releases(tmp_path):
    for path in [tmp_path / "l2v5_pool" / "sra", tmp_path / "L2_Demo_V5_r2"]:
        with pytest.raises(ValueError):
            builder.protect_output(path)
    with pytest.raises(ValueError):
        builder.protect_output(tmp_path, tmp_path)
    builder.protect_output(tmp_path / "l2v5_pool_r3" / "sra")


def test_diagnostic_instrumentation_observes_real_unittest_failure(tmp_path):
    spec = importlib.util.spec_from_file_location("sra_diag_test", TOOLS / "l2_v5_sra_diagnostics.py")
    diagnostic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostic)
    task = tmp_path / "task"
    (task / "evaluation_assets").mkdir(parents=True)
    (task / "evaluation_assets/source_verifier.py").write_text('''
import unittest
class NativeTest(unittest.TestCase):
    def test_expected_value(self):
        self.assertEqual(self.value, 42)
def verify(response, task):
    NativeTest.value = int(response)
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(NativeTest).run(result)
    return {"status": "pass" if result.wasSuccessful() else "fail", "score": int(result.wasSuccessful()),
            "details": {"result": "pass" if result.wasSuccessful() else "fail"}}
''')
    for name, response in [("gold", "42"), ("broken", "0")]:
        out = tmp_path / name
        out.mkdir()
        report = diagnostic.run(task, response, out / "result.json", shadow=True)
        assert report["tests_started"] == 1
        assert report["assertion_failures"] == (name == "broken")
        assert report["test_errors"] == 0
        assert json.loads((out / "result.json").read_text())["verifier"]["score"] == (name == "gold")


def test_red_candidates_preserve_gold_and_leave_tests_unchanged(tmp_path):
    task = tmp_path / "task"
    (task / "evaluation_assets").mkdir(parents=True)
    source = {"eval_data": {"code_prompt": "def task_func(x):\n",
                            "answer": "    return x + 42\n", "test": "native test marker"}}
    path = task / "evaluation_assets/source_item.json"
    path.write_text(json.dumps(source))
    candidates = list(builder.red_candidates(task))
    assert any("43" in body for _, body in candidates)
    assert candidates[0] == ("return_none", "def task_func(x):\n    return None\n")
    assert json.loads(path.read_text()) == source


@pytest.mark.skipif(__import__('os').environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1',
                    reason='explicit Docker cp executor preflight')
@pytest.mark.parametrize(('response', 'red'), [('42', False), ('0', True)])
@pytest.mark.parametrize('enforcement_mode', ['strict', 'declared_only'])
def test_fixture_transport_real_docker_cp(tmp_path, response, red, enforcement_mode, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', enforcement_mode)
    image = builder.legacy.image_digest('trievo-l2-v5:0.7-native')
    task = tmp_path / 'task'
    (task / 'evaluation_assets').mkdir(parents=True)
    (task / 'evaluation_assets/source_verifier.py').write_text('''
import unittest
class NativeTest(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(self.value, 42)
def verify(response, task):
    NativeTest.value = int(response)
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(NativeTest).run(result)
    return {"status": "pass" if result.wasSuccessful() else "fail", "score": int(result.wasSuccessful()),
            "details": {"result": "pass" if result.wasSuccessful() else "fail"}}
''')
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    (fixture / 'response.txt').write_text(response)
    authority = builder.fixture_run(task, fixture, image, tmp_path / 'authority', shadow=False)
    shadow = builder.fixture_run(task, fixture, image, tmp_path / 'shadow', shadow=True)
    capability = builder.docker_resource_capability_preflight()
    if not capability.get('allowed', capability['passed']):
        assert authority['resource_preflight_rejected'] and shadow['resource_preflight_rejected']
        assert not authority['transport_verified'] and not shadow['transport_verified']
        assert not (tmp_path / 'authority/result.json').exists()
        return
    assert authority['transport_verified'], authority
    assert shadow['transport_verified'], shadow
    assert builder.diagnostic_valid(authority, shadow, red=red), (authority, shadow)
    assert authority['actual_image_id'] == image and authority['network_readback'] == 'none'
    assert authority['expected_nonce'] != shadow['expected_nonce']
    resources = authority['resource_readback']
    assert resources['accepted']
    if not resources['validation']['passed']:
        assert authority['resource_enforcement'] == 'declared_not_enforced'
        assert resources['resource_enforcement'] == 'declared_not_enforced'
        assert resources['requested_resources']['cpu_limit'] == 2
        assert resources['observed_resources']['cpu_limit'] != 2
        assert resources['waiver']['mode'] == 'declared_only'
    commands = authority['transport_commands'] + shadow['transport_commands']
    assert not any('-v' in command or '--mount' in command for command in commands)
    assert not any(command[1] == 'exec' for command in commands)
    # A stale host-side green must not override a failed/no-op daemon launch.
    authority['transport_verified'] = False
    assert not builder.diagnostic_valid(authority, shadow, red=red)


def test_noop_daemon_cannot_reuse_stale_host_green(tmp_path, monkeypatch):
    import subprocess
    image = 'sha256:' + '0' * 64
    task = tmp_path / 'task'; task.mkdir()
    fixture = tmp_path / 'fixture'; fixture.mkdir()
    (fixture / 'response.txt').write_text('42')
    out = tmp_path / 'out'; out.mkdir()
    (out / 'result.json').write_text(json.dumps({'verifier': {'status': 'pass', 'score': 1},
                                              'execution_nonce': 'stale', 'shadow': False}))

    def noop(command, **kwargs):
        host = {'PidsLimit': 256, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'],
                'Memory': 4 * 1024 ** 3, 'NanoCpus': 2 * 10 ** 9}
        stdout = json.dumps({'Running': False, 'ExitCode': 0}) + '|' + image + '|none|' + json.dumps(host) if command[1] == 'inspect' else ''
        return subprocess.CompletedProcess(command, 0, stdout, '')

    monkeypatch.setattr(builder, 'docker_resource_capability_preflight', lambda **kwargs: {'passed': True, 'errors': []})
    monkeypatch.setattr(builder.subprocess, 'run', noop)
    result = builder.fixture_run(task, fixture, image, out, shadow=False)
    assert result['transport_verified'] is False
    assert 'missing first stdout resource header' in result['transport_error']
    assert 'verifier' not in result


@pytest.mark.parametrize('timeout_side', ['authority', 'shadow'])
def test_native_timeout_is_not_assertion_red(timeout_side):
    authority = evidence('fail')
    shadow = evidence('fail', failures=1)
    assert builder.diagnostic_valid(authority, shadow, red=True)
    timed_out = authority if timeout_side == 'authority' else shadow
    timed_out['verifier']['details']['result'] = 'timeout'
    assert not builder.diagnostic_valid(authority, shadow, red=True)


def test_interrupted_shadow_cannot_qualify_after_early_assertion():
    authority = evidence('fail')
    shadow = evidence('fail', failures=1, started=2)
    shadow['tests_completed'] = 1
    assert not builder.diagnostic_valid(authority, shadow, red=True)
    shadow['tests_completed'] = 2
    assert builder.diagnostic_valid(authority, shadow, red=True)


def test_native_green_requires_explicit_native_pass():
    authority = evidence('pass')
    shadow = evidence('pass')
    shadow['verifier']['details']['result'] = 'timeout'
    assert not builder.diagnostic_valid(authority, shadow, red=False)


def test_r3_think_helper_matches_upstream_whitespace_and_preserves_legacy():
    import ast
    import re
    import l2_v5_sra_strip_think as upstream
    # Extract the legacy pure helper without importing its native dependencies.
    tree = ast.parse(builder.legacy.VERIFIER)
    definitions = [node for node in tree.body if
                   isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('_THINK_') for t in node.targets)
                   or isinstance(node, ast.FunctionDef) and node.name == 'strip_think_tags']
    scope = {'re': re}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), '<legacy-strip>', 'exec'), scope)
    legacy_strip = scope['strip_think_tags']
    assert upstream._THINK_CLOSED_RE.pattern == r'<think>.*?</think>\s*'
    assert upstream.strip_think_tags('<think>reason</think>  answer') == 'answer'
    assert upstream.strip_think_tags('plain answer') == 'plain answer'
    # Whitespace inside generated Python strings affects the actual return value.
    response = 'def task_func():\n    return "prefix<think>reason</think>   suffix"\n'
    native_scope = {}; old_scope = {}
    exec(upstream.strip_think_tags(response), native_scope)
    exec(legacy_strip(response), old_scope)
    assert native_scope['task_func']() == 'prefixsuffix'
    assert old_scope['task_func']() == 'prefix   suffix'
    assert native_scope['task_func']() != old_scope['task_func']()
    # New carrier delegates to the source-exact helper; r2 source is unchanged.
    assert 'from strip_think import strip_think_tags' in builder.r3_verifier_text()
    assert '_THINK_CLOSED_RE =' not in builder.r3_verifier_text()
    assert r're.compile(r"<think>.*?</think>", re.DOTALL)' in builder.legacy.VERIFIER


def test_legacy_hostconfig_readback_without_actual_cgroups_is_rejected():
    authority = evidence('fail')
    shadow = evidence('fail', failures=1)
    del authority['resource_readback']
    assert not builder.diagnostic_valid(authority, shadow, red=True)
    authority = evidence('fail')
    shadow['resource_readback']['cpu_limit'] = 108.04
    assert not builder.diagnostic_valid(authority, shadow, red=True)


def test_waiver_does_not_authorize_missing_sra_resource_observation(monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    authority = evidence('pass'); shadow = evidence('pass')
    authority['resource_readback'] = {}
    assert not builder.diagnostic_valid(authority, shadow, red=False)


def test_qualification_records_actual_waiver_in_index_runtime_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    pool = tmp_path / 'l2v5_pool_r3/sra'; pool.mkdir(parents=True)
    task_dir = 'sr_agents__fixture'; task = pool / task_dir; task.mkdir()
    source_id = 'sr_agents:fixture'
    (pool / 'INDEX.jsonl').write_text(json.dumps({'source_id': source_id, 'task_dir': task_dir}) + '\n')
    (pool / 'ADAPTATIONS.jsonl').write_text(json.dumps({'source_id': source_id, 'adaptations': [], 'qualification': 'pending'}) + '\n')
    builder.write_json(task / 'pool_task.json', {'runtime': {}, 'qualification': {}, 'adaptations': []})
    def observation(status):
        data = evidence(status, failures=status == 'fail')
        data['resource_readback'].update(cpu_limit=108.04, memory_limit_bytes=2019574153216, pids_limit=None)
        enriched = builder.require_container_resources(data['resource_readback'])
        data.update(resource_readback=enriched, resource_enforcement=enriched['resource_enforcement'],
                    resource_observation_provenance={'kind': 'synthetic_test_observation'})
        return data
    fixtures = {name: {'authoritative': observation(status), 'diagnostic': observation(status), 'valid': True}
                for name, status in [('known_correct', 'pass'), ('known_broken', 'fail')]}
    monkeypatch.setattr(builder, 'docker_resource_capability_preflight', lambda **kwargs: {'allowed': True, 'passed': False})
    monkeypatch.setattr(builder.legacy, 'image_digest', lambda image: 'sha256:' + 'a' * 64)
    monkeypatch.setattr(builder, 'qualify_one', lambda *a: {'task_dir': task_dir, 'fixtures': fixtures, 'admitted': True})
    builder.qualify(pool, 'fixture:image', tmp_path / 'logs', 1, [])
    row = json.loads((pool / 'INDEX.jsonl').read_text())
    record = json.loads((task / 'pool_task.json').read_text())
    ledger = json.loads((pool / 'ADAPTATIONS.jsonl').read_text())
    for destination in [row, record, record['runtime'], record['qualification'], ledger]:
        assert destination['resource_enforcement'] == 'declared_not_enforced'
        assert destination['resource_readback']['cpu_limit'] == 108.04
        assert destination['resource_readback']['requested_resources']['cpu_limit'] == 2
        assert destination['resource_readback']['validation']['passed'] is False
        assert destination['resource_observation_provenance']['kind'] == 'synthetic_test_observation'
    assert any(a['kind'] == 'resource_enforcement_waiver' for a in ledger['adaptations'])
    assert any(a['kind'] == 'resource_enforcement_waiver' for a in record['adaptations'])


def header_text(*, nonce='fresh', response_sha256='hash', shadow=False, snapshot=None):
    return json.dumps({'schema_version': 'sra_resource_pre_execution/1.0',
                       'execution_nonce': nonce, 'response_sha256': response_sha256,
                       'shadow': shadow, 'resource_readback': snapshot or evidence('pass')['resource_readback'],
                       'assessment': {'accepted': True, 'resource_enforcement': 'enforced'}})


@pytest.mark.parametrize('prefix', ['', 'candidate chatter\n', '\n'])
def test_resource_header_absent_or_late_rejected(prefix):
    stdout = '' if prefix == '' else prefix + header_text()
    with pytest.raises(ValueError):
        builder.resource_header(stdout, nonce='fresh', response_sha256='hash', shadow=False,
                                enforcement_mode='declared_only')


@pytest.mark.parametrize('changed', [{'nonce': 'old'}, {'response_sha256': 'other'}, {'shadow': True}])
def test_resource_header_identity_mismatch_rejected(changed):
    with pytest.raises(ValueError):
        builder.resource_header(header_text(**changed), nonce='fresh', response_sha256='hash',
                                shadow=False, enforcement_mode='declared_only')


def test_resource_header_host_reassesses_serialized_waiver():
    snapshot = evidence('pass')['resource_readback']
    snapshot.update(cpu_limit=108.04, memory_limit_bytes=2019574153216, pids_limit=None)
    stdout = header_text(snapshot=snapshot)
    waived = builder.resource_header(stdout, nonce='fresh', response_sha256='hash', shadow=False,
                                     enforcement_mode='declared_only')
    assert waived['resource_enforcement'] == 'declared_not_enforced'
    strict = builder.resource_header(stdout, nonce='fresh', response_sha256='hash', shadow=False,
                                     enforcement_mode='strict')
    assert not strict['accepted']
    with pytest.raises(builder.ContainerResourceError):
        builder.require_container_resources(strict, enforcement_mode='strict')


@pytest.mark.skipif(__import__('os').environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1',
                    reason='explicit real Docker malicious resource-file overwrite')
def test_candidate_forged_resource_files_cannot_replace_stdout_observation(tmp_path, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    image = builder.legacy.image_digest('trievo-l2-v5:0.7-native')
    task = tmp_path / 'task'
    (task / 'evaluation_assets').mkdir(parents=True)
    (task / 'evaluation_assets/source_verifier.py').write_text('''
import atexit
import json
from pathlib import Path

def forge_files():
    for name in ['result.json', 'resource_readback.json']:
        path = Path('/out') / name
        payload = json.loads(path.read_text())
        payload['resource_readback'].update(cpu_limit=2.0, memory_limit_bytes=4294967296,
            pids_limit=256, errors=[], controllers={key: {'finite_cgroup_limit': True}
                for key in ['cpu', 'memory', 'pids']})
        payload['forgery_executed'] = True
        path.write_text(json.dumps(payload))
atexit.register(forge_files)

def verify(response, task):
    return {'status': 'pass', 'score': 1, 'details': {'result': 'pass'}}
''')
    fixture = tmp_path / 'fixture'; fixture.mkdir()
    (fixture / 'response.txt').write_text('42')
    report = builder.fixture_run(task, fixture, image, tmp_path / 'out', shadow=False)
    assert report['transport_verified'], report
    assert report['forgery_executed'] is True  # Confirm the attack actually ran.
    header = json.loads((tmp_path / 'out/stdout.log').read_text().splitlines()[0])
    actual = header['resource_readback']
    assert report['resource_readback']['cpu_limit'] == actual['cpu_limit']
    assert report['resource_readback']['memory_limit_bytes'] == actual['memory_limit_bytes']
    assert report['resource_readback']['pids_limit'] == actual['pids_limit']
    if not builder.evaluate_resource_enforcement(actual, enforcement_mode='strict')['accepted']:
        assert report['resource_enforcement'] == 'declared_not_enforced'
    assert report['resource_observation_provenance']['kind'] == 'first_stdout_pre_execution_header'
    assert json.loads((tmp_path / 'out/result.json').read_text())['resource_readback'] == report['resource_readback']


@pytest.mark.parametrize('failure_kind', ['exit', 'timeout'])
def test_start_failure_preserves_only_first_header_observation(tmp_path, monkeypatch, failure_kind):
    import hashlib
    import subprocess
    fixture = tmp_path / 'fixture'; fixture.mkdir()
    (fixture / 'response.txt').write_text('42')
    task = tmp_path / 'task'; task.mkdir()
    snapshot = evidence('pass')['resource_readback']
    snapshot.update(cpu_limit=108.04, pids_limit=None)
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    monkeypatch.setattr(builder, 'docker_resource_capability_preflight',
                        lambda **kwargs: {'passed': False, 'allowed': True})
    nonce = None
    def fail_after_header(command, **kwargs):
        nonlocal nonce
        if command[1] == 'create':
            nonce = next(arg.split('=', 1)[1] for arg in command if arg.startswith('L2_QUALIFICATION_NONCE='))
        if command[1] == 'start':
            stdout = header_text(nonce=nonce, response_sha256=hashlib.sha256(b'42').hexdigest(),
                                 snapshot=snapshot) + '\n'
            if failure_kind == 'timeout':
                raise subprocess.TimeoutExpired(command, 600, output=stdout.encode(), stderr=b'timeout')
            raise subprocess.CalledProcessError(1, command, output=stdout, stderr='verifier import failure')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(builder.subprocess, 'run', fail_after_header)
    result = builder.fixture_run(task, fixture, 'image', tmp_path / 'out', shadow=False)
    assert result['transport_verified'] is False
    assert ('CalledProcessError' if failure_kind == 'exit' else 'TimeoutExpired') in result['transport_error']
    assert result['resource_readback']['cpu_limit'] == 108.04
    assert result['resource_enforcement'] == 'declared_not_enforced'
    assert not any(cmd[1] == 'cp' and ':/out/' in cmd[2] for cmd in result['transport_commands'])
    assert 'verifier' not in result
