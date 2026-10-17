"""Actual resource accounting: ancestor limits, namespaces and fail-closed gates."""
import ast
import json
from pathlib import Path
import subprocess

import pytest

from trievo_bench import container_resources as resources


@pytest.fixture(autouse=True)
def strict_resource_mode_by_default(monkeypatch):
    monkeypatch.delenv("TRIEVO_RESOURCE_ENFORCEMENT", raising=False)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def v2(tmp_path, *, nested=True, root='/'):
    proc, fs = tmp_path / 'proc', tmp_path / 'fs'
    member = '/parent/leaf' if nested else '/'
    _write(proc / 'self/cgroup', '0::' + member + '\n')
    _write(proc / 'self/mountinfo', '1 0 0:1 ' + root + ' /sys/fs/cgroup rw - cgroup2 cgroup rw\n')
    mount = fs / 'sys/fs/cgroup'
    for suffix, cpu, memory, pids in [('', 'max 100000', 'max', 'max'),
                                     ('parent', '200000 100000', '4294967296', '256'),
                                     ('parent/leaf', '400000 100000', '8589934592', '512')]:
        directory = mount / suffix
        for name, value in [('cpu.max', cpu), ('memory.max', memory), ('pids.max', pids)]:
            _write(directory / name, value)
    return proc, fs, mount


def test_v2_ancestor_intersection_positive_then_deliberate_quota_failure(tmp_path):
    proc, fs, mount = v2(tmp_path)
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert resources.validate_container_resources(snapshot)['passed']
    assert resources.require_container_resources(snapshot)['cpu_limit'] == 2.0
    # Loosen the controlling ancestor; requested Docker-like child metadata
    # cannot rescue actual CPU4/memory8GiB/pids512 behavior.
    for name, value in [('cpu.max', 'max 100000'), ('memory.max', 'max'), ('pids.max', 'max')]:
        (mount / 'parent' / name).write_text(value)
    broken = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert not resources.validate_container_resources(broken)['passed']
    with pytest.raises(resources.ContainerResourceError):
        resources.require_container_resources(broken)


def test_v1_split_controller_mounts_and_parent_mapping(tmp_path):
    proc, fs = tmp_path / 'proc', tmp_path / 'fs'
    _write(proc / 'self/cgroup', '1:cpu,cpuacct:/docker/child\n2:memory:/docker/child\n3:pids:/docker/child\n')
    lines = []
    for index, controller in enumerate(['cpu', 'memory', 'pids'], 1):
        lines.append('{} 0 0:{} /docker /sys/fs/cgroup/{} rw - cgroup cgroup rw,{}\n'.format(index, index, controller, controller))
        parent = fs / 'sys/fs/cgroup' / controller
        if controller == 'cpu':
            values = {'cpu.cfs_quota_us': '200000', 'cpu.cfs_period_us': '100000'}
            child = {'cpu.cfs_quota_us': '-1', 'cpu.cfs_period_us': '100000'}
        elif controller == 'memory':
            values = {'memory.limit_in_bytes': '4294967296', 'memory.use_hierarchy': '1'}
            child = {'memory.limit_in_bytes': str(2**63 - 4096)}
        else:
            values, child = {'pids.max': '256'}, {'pids.max': 'max'}
        for key, value in values.items(): _write(parent / key, value)
        for key, value in child.items(): _write(parent / 'child' / key, value)
    _write(proc / 'self/mountinfo', ''.join(lines))
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert resources.validate_container_resources(snapshot)['passed'], snapshot
    assert snapshot['controllers']['cpu']['resolved_process_path'].endswith('/cpu/child')


def test_private_cgroup_namespace_mount_root_maps_to_visible_root(tmp_path):
    proc, fs, mount = v2(tmp_path, nested=False, root='/docker/container-id')
    for name, value in [('cpu.max', '200000 100000'), ('memory.max', '4294967296'), ('pids.max', '256')]:
        (mount / name).write_text(value)
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert resources.validate_container_resources(snapshot)['passed'], snapshot
    assert snapshot['controllers']['cpu']['mount_root'] == '/docker/container-id'


def test_missing_unlimited_or_unreadable_controls_fail_closed(tmp_path):
    assert resources.validate_container_resources({})['passed'] is False
    proc, fs, mount = v2(tmp_path)
    (mount / 'parent' / 'memory.max').unlink()
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert not resources.validate_container_resources(snapshot)['passed']
    assert any('memory readback failed' in error for error in snapshot['errors'])
    (mount / 'parent/memory.max').write_text('max')
    for directory in [mount, mount / 'parent', mount / 'parent/leaf']:
        (directory / 'cpu.max').write_text('max 100000')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=2)
    # Affinity2 without an enforced quota is not evidence CPU2 was requested.
    assert snapshot['cpu_limit'] is None
    assert not resources.validate_container_resources(snapshot)['passed']


def test_capability_preflight_rejects_none_and_never_claims_enforcement(monkeypatch):
    def daemon(driver):
        return subprocess.CompletedProcess([], 0, json.dumps({'CgroupDriver': driver, 'CgroupVersion': '2'}), '')
    monkeypatch.setattr(resources.subprocess, 'run', lambda *a, **kw: daemon('none'))
    assert not resources.docker_resource_capability_preflight()['passed']
    monkeypatch.setattr(resources.subprocess, 'run', lambda *a, **kw: daemon('systemd'))
    result = resources.docker_resource_capability_preflight()
    assert result['passed'] and 'NOT' not in result['proof_scope']
    assert 'actual container readback required' in result['proof_scope']


def test_embedded_source_is_python39_parseable_and_runs_before_user_work():
    script = resources.container_resource_check_script()
    ast.parse(script, feature_version=(3, 9))
    ast.parse('import os\nordinary_setup = True\n' + script, feature_version=(3, 9))
    # Replace only the hardware observer to exercise successful embedded flow.
    script = script[:script.rindex('\nresource_readback =')]
    scope = {'__name__': 'embedded_resource_guard'}
    exec('import os\nordinary_setup = True\n' + script, scope)
    assert scope['ordinary_setup'] is True
    fake = {'schema_version': resources.SCHEMA, 'cpu_limit': 2.0, 'memory_limit_bytes': 4294967296, 'pids_limit': 256, 'errors': [],
            'controllers': {key: {'finite_cgroup_limit': True} for key in ['cpu', 'memory', 'pids']}}
    scope['read_container_resources'] = lambda: fake
    assert scope['require_container_resources']()['validation']['passed']
    fake['memory_limit_bytes'] *= 2
    with pytest.raises(RuntimeError):
        scope['require_container_resources']()


def test_process_affinity_cannot_fake_an_enforced_cpu_limit(tmp_path):
    proc, fs, mount = v2(tmp_path)
    for directory in [mount, mount / 'parent', mount / 'parent/leaf']:
        (directory / 'cpu.max').write_text('max 100000')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=2)
    assert not resources.validate_container_resources(snapshot)['passed']
    (mount / 'parent/leaf/cpuset.cpus.effective').write_text('3,7')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=2)
    assert snapshot['cpu_limit'] == 2
    assert resources.validate_container_resources(snapshot)['passed']


@pytest.mark.parametrize('hierarchy', [None, '0'])
def test_v1_missing_or_disabled_memory_hierarchy_fails_closed(tmp_path, hierarchy):
    proc, fs = tmp_path / 'proc', tmp_path / 'fs'
    _write(proc / 'self/cgroup', '1:memory:/parent/child\n')
    _write(proc / 'self/mountinfo', '1 0 0:1 /parent /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n')
    mount = fs / 'sys/fs/cgroup/memory'
    _write(mount / 'memory.limit_in_bytes', '4294967296')
    _write(mount / 'child/memory.limit_in_bytes', str(2**63 - 4096))
    if hierarchy is not None: _write(mount / 'memory.use_hierarchy', hierarchy)
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert any('memory readback failed' in error for error in snapshot['errors'])
    assert not resources.validate_container_resources(snapshot)['passed']
    _write(mount / 'memory.use_hierarchy', '1')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert snapshot['memory_limit_bytes'] == 4294967296


def test_only_verified_kernel_unified_root_may_omit_control_files(tmp_path):
    proc, fs, mount = v2(tmp_path)
    for name in ['cpu.max', 'memory.max', 'pids.max']: (mount / name).unlink()
    _write(mount / 'cgroup.controllers', 'cpu memory pids cpuset')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert resources.validate_container_resources(snapshot)['passed'], snapshot
    # Apparent namespace root is not the kernel hierarchy root.
    _write(mount / 'cgroup.type', 'domain')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    assert not resources.validate_container_resources(snapshot)['passed']


def test_explicit_waiver_does_not_turn_strict_validation_green(tmp_path, monkeypatch):
    proc, fs, mount = v2(tmp_path)
    (mount / 'parent/cpu.max').write_text('max 100000')
    snapshot = resources.read_container_resources(proc, fs, affinity_cpus=64)
    snapshot['resource_enforcement'] = 'declared_not_enforced'
    assert not resources.validate_container_resources(snapshot)['passed']
    assert not resources.evaluate_resource_enforcement(snapshot)['accepted']
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    decision = resources.evaluate_resource_enforcement(snapshot)
    assert decision['accepted'] and not decision['strict_passed']
    assert decision['resource_enforcement'] == 'declared_not_enforced'
    assert decision['requested_resources']['cpu_limit'] == 2
    assert decision['observed_resources']['cpu_limit'] == 4
    assert not resources.require_container_resources(snapshot)['validation']['passed']
    assert not resources.evaluate_resource_enforcement({})['accepted']
    # Host-selected mode is embedded, even after the receiving env changes.
    script = resources.container_resource_check_script()
    assert "enforcement_mode='declared_only'" in script.splitlines()[-1]
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'strict')
    assert not resources.evaluate_resource_enforcement(snapshot)['accepted']


@pytest.mark.parametrize('mode', ['waived', 'true', '', 'DECLARED_ONLY'])
def test_invalid_waiver_optin_rejected(mode, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', mode)
    with pytest.raises(resources.ContainerResourceError): resources.resource_enforcement_mode()
    with pytest.raises(resources.ContainerResourceError): resources.docker_resource_capability_preflight()


def test_waiver_never_accepts_daemon_transport_or_malformed_info(monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    monkeypatch.setattr(resources.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 0, '{}', ''))
    result = resources.docker_resource_capability_preflight()
    assert not result['allowed'] and not result['passed']
    def failed(*a, **kw): raise subprocess.CalledProcessError(1, 'docker info')
    monkeypatch.setattr(resources.subprocess, 'run', failed)
    assert not resources.docker_resource_capability_preflight()['allowed']
    monkeypatch.setattr(resources.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps({'CgroupDriver': 'none', 'CgroupVersion': '1'}), ''))
    result = resources.docker_resource_capability_preflight()
    assert result['allowed'] and not result['passed'] and not result['strict_passed']
    assert result['resource_enforcement'] == 'declared_not_enforced'


@pytest.mark.parametrize('snapshot', [{}, [], {'schema_version': resources.SCHEMA},
    {'schema_version': resources.SCHEMA, 'cpu_limit': 2, 'memory_limit_bytes': 4294967296,
     'pids_limit': 256, 'errors': [], 'controllers': {'cpu': 1}}])
def test_malformed_snapshot_rejected_even_with_waiver(snapshot):
    assert not resources.evaluate_resource_enforcement(snapshot, enforcement_mode='declared_only')['accepted']
