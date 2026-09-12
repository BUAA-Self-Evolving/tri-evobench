import json
import sys
from types import SimpleNamespace

import pytest

from trievo_bench import storage_recovery as module


@pytest.mark.skipif(sys.platform != "linux", reason="storage gate samples Linux statvfs mounts")
def test_low_space_latches_then_recovers_after_stability_across_instances(tmp_path, monkeypatch):
    clock = [100.0]
    free = [40]
    probe_calls = []
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(module.os, 'statvfs', lambda p: SimpleNamespace(
        f_bavail=free[0] * 1024**3, f_frsize=1, f_blocks=100 * 1024**3,
        f_favail=50000, f_files=100000))
    def gate():
        return module.StorageGate(tmp_path / 'state', tmp_path, probe=lambda: probe_calls.append(1))
    assert gate().check()['healthy']
    free[0] = 19; clock[0] += 3
    assert not gate().check()['healthy']
    free[0] = 23; clock[0] += 35  # above stop floor, below resume floor
    assert not gate().check()['healthy']
    free[0] = 30; clock[0] += 3
    assert not gate().check()['healthy']
    clock[0] += 31
    assert gate().check()['healthy']
    assert len(probe_calls) == 2
    gate().trip('ENOSPC from a second worker')
    assert not gate().check()['healthy']
    assert json.loads((tmp_path / 'state/health.json').read_text())['healthy'] is False


@pytest.mark.skipif(sys.platform != "linux", reason="storage gate samples Linux statvfs mounts")
def test_bad_overlay_probe_does_not_release_pending_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(module.os, 'statvfs', lambda p: SimpleNamespace(
        f_bavail=80*1024**3, f_frsize=1, f_blocks=100*1024**3, f_favail=50000, f_files=100000))
    def broken():
        raise module.StorageUnavailable('Error setting up pivot dir')
    gate = module.StorageGate(tmp_path / 'state', tmp_path, probe=broken)
    state = gate.check()
    assert not state['healthy']
    assert 'pivot' in state['reason']


def test_missing_mount_is_not_sampled_as_its_parent(tmp_path):
    gate = module.StorageGate(tmp_path / 'state', tmp_path / 'missing', probe=lambda: None)
    assert not gate.check()['healthy']


def test_off_by_default_and_classifier(monkeypatch):
    monkeypatch.delenv('TRIEVO_STORAGE_GUARD', raising=False)
    assert module.from_environment() is None
    assert module.is_storage_error('write /volume: no space left on device')
    assert module.is_storage_error('Error setting up pivot dir')
    assert not module.is_storage_error('OpenAI-compatible model returned empty content')
    assert not module.is_storage_error('HTTP 401')
