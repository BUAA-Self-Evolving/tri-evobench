from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import zipfile
import sys
import time

import pytest

from tools import run_new_package_matrix as matrix
from trievo_bench.fast_checkpoint import archive_tree, FileDigests
from trievo_bench.local_resources import DEFAULT_WEIGHTS, LocalResourceGate, validate_policy
from trievo_bench.local_resources import host_under_memory_pressure
from trievo_bench.performance import timed_call


def policy():
    return {"schema": "local_resource_policy/1.0", "docker_cpus": 8, "docker_memory_gib": 9.69,
            "max_workers": 4, "tau2_workers": 2, "memory_reserve_gib": 1.25,
            "minimum_available_gib": .75, "max_cpu_utilization": .85,
            "sample_interval_seconds": 5, "weights": DEFAULT_WEIGHTS}


def healthy():
    return {"available_gib": 8, "cpu_utilization": .1, "host_memory_pressure": False}


def test_recovered_mac_warning_requires_free_memory_and_measured_low_paging():
    assert host_under_memory_pressure(2, 41, 0)  # old profiles retain warning gate
    assert host_under_memory_pressure(2, 41, None, True)
    assert not host_under_memory_pressure(2, 41, 0, True)
    assert host_under_memory_pressure(2, 20, 0, True)
    assert host_under_memory_pressure(2, 41, 8, True)
    assert host_under_memory_pressure(4, 41, 0, True)


def test_optional_calibrated_l1_class_does_not_consume_heavy_quota():
    p = policy() | {"docker_cpus": 10, "docker_memory_gib": 11.65,
                    "max_workers": 6, "l1_workers": 4,
                    "weights": {**DEFAULT_WEIGHTS, "l1": {"cpus": 1, "memory_gib": 1.75}}}
    gate = LocalResourceGate(p, lambda: healthy() | {"available_gib": 11})
    heavy = {"level": "l2", "flow_id": "intra_skilllearn_01"}
    light = {"level": "l1", "flow_id": "l1_arena_stream_1"}
    assert gate.allows(light, [heavy, heavy, light])
    assert not gate.allows(heavy, [heavy, heavy, light])
    assert not gate.allows(light, [light] * 4)
    assert not gate.allows(light, [heavy, heavy, light, light])  # CPU reservation
    legacy = LocalResourceGate(policy(), healthy)
    assert legacy.category(light) == "heavy"


def test_standard_zip_reuse_modification_and_deletion_do_not_change_old_checkpoints(tmp_path):
    source, store = tmp_path / "workspace", tmp_path / "private"
    source.mkdir()
    (source / "文字.txt").write_text("unchanged")
    (source / "binary").write_bytes(bytes(range(256)))
    (source / "binary").chmod(0o700)
    (source / "link").symlink_to(source / "binary")
    a, b, c, d = [tmp_path / name / "workspace.zip" for name in "abcd"]
    assert not archive_tree(source, a, store)["cache_hit"]
    assert archive_tree(source, b, store)["cache_hit"]
    assert a.stat().st_ino == b.stat().st_ino
    original = a.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert z.testzip() is None
        assert set(z.namelist()) == {"文字.txt", "binary"}
        assert z.read("binary") == bytes(range(256))
        assert (z.getinfo("binary").external_attr >> 16) & 0o777 == 0o700
    before = (source / "文字.txt").stat()
    (source / "文字.txt").write_text("different")
    os.utime(source / "文字.txt", ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not archive_tree(source, c, store)["cache_hit"]
    (source / "binary").unlink()
    archive_tree(source, d, store)
    with zipfile.ZipFile(c) as z:
        assert z.read("文字.txt") == b"different"
    with zipfile.ZipFile(d) as z:
        assert z.namelist() == ["文字.txt"]
    assert a.read_bytes() == b.read_bytes() == original


def test_digest_cache_detects_same_size_content_change_with_restored_mtime(tmp_path):
    path = tmp_path / "file"
    path.write_text("first")
    cache = FileDigests()
    first, before = cache.digest(path), path.stat()
    path.write_text("other")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert cache.digest(path) != first


def test_concurrent_snapshot_publish_and_standalone_restore(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "data").write_bytes(b"repeat\x00" * 100000)
    paths = [tmp_path / str(n) / "workspace.zip" for n in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda p: archive_tree(source, p, tmp_path / "store"), paths))
    assert len({p.stat().st_ino for p in paths}) == 1
    # Removing the private canonical name cannot break any standalone ZIP.
    for p in (tmp_path / "store").glob("*/*.zip"):
        p.unlink()
    for p in paths:
        with zipfile.ZipFile(p) as z:
            assert z.read("data") == (source / "data").read_bytes()


def test_mutation_during_archive_is_not_published(tmp_path, monkeypatch):
    source = tmp_path / "workspace"
    source.mkdir()
    data = source / "data"
    data.write_text("before")
    original = zipfile.ZipFile.write
    def changing(bundle, *args, **kwargs):
        result = original(bundle, *args, **kwargs)
        data.write_text("after!")
        return result
    monkeypatch.setattr(zipfile.ZipFile, "write", changing)
    with pytest.raises(RuntimeError, match="changed"):
        archive_tree(source, tmp_path / "out.zip", tmp_path / "store")
    assert not (tmp_path / "out.zip").exists()
    assert not list((tmp_path / "store").glob("*/*.zip"))


def test_agent_workspace_cannot_contain_private_store(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        archive_tree(tmp_path, tmp_path / "out.zip", tmp_path / "private")


def test_changed_snapshot_reuses_apfs_blocks_without_external_dependencies(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "dependency").write_bytes(os.urandom(2 * 1024**2))
    (source / "answer").write_text("old")
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    archive_tree(source, a, tmp_path / "store")
    (source / "answer").write_text("new")
    result = archive_tree(source, b, tmp_path / "store")
    if result["apfs_changed_bytes"] is not None:
        assert result["apfs_changed_bytes"] < 16384
    for path, answer in [(a, b"old"), (b, b"new")]:
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
            assert z.read("answer") == answer


def test_admission_uses_reservations_and_live_pressure():
    gate = LocalResourceGate(policy(), healthy)
    heavy = {"flow_id": "intra_skilllearn_01"}
    tau = {"flow_id": "intra_tau2_01"}
    sra = {"flow_id": "intra_sra_01"}
    assert gate.allows(heavy, [heavy])
    assert not gate.allows(sra, [heavy, heavy])
    assert gate.allows(tau, [heavy])
    assert not gate.allows(tau, [tau, tau])
    assert gate.allows(sra, [heavy, tau, tau])
    assert not gate.allows(sra, [sra] * 4)
    for bad in [{**healthy(), "available_gib": 1},
                {**healthy(), "cpu_utilization": .99},
                {**healthy(), "host_memory_pressure": True}]:
        assert not LocalResourceGate(policy(), lambda: bad).allows(tau, [])
    def unavailable():
        raise RuntimeError("private error must not appear in status")
    broken = LocalResourceGate(policy(), unavailable)
    assert not broken.allows(sra, [])
    assert "private error" not in json.dumps(broken.health)


def test_priority_overlaps_tau2_and_heavy_work_preserving_each_condition_order():
    gate = LocalResourceGate({**policy(), "queue_priority": {"tau2": 0, "heavy": 1, "sra": 2}}, healthy)
    rows = [{"flow_id": f, "condition": c} for f in ["sra", "skilllearn", "tau2"]
            for c in ["reset", "frozen", "evolving"]]
    ordered = gate.order(rows)
    assert [r["flow_id"] for r in ordered] == ["tau2"] * 3 + ["skilllearn"] * 3 + ["sra"] * 3
    assert [r["condition"] for r in ordered[:3]] == ["reset", "frozen", "evolving"]


def test_resource_pressure_skips_blocked_head_without_blocking_other_task_family():
    from collections import deque
    gate = LocalResourceGate(policy(), healthy)
    heavy = {"id": "heavy", "flow_id": "intra_skilllearn_01"}
    tau = {"id": "tau", "flow_id": "intra_tau2_01"}
    queue = deque([heavy, tau])
    assert matrix._pop_eligible(queue, {}, admission=lambda r: gate.allows(r, [heavy])) == heavy
    queue = deque([heavy, tau])
    assert matrix._pop_eligible(queue, {}, admission=lambda r: gate.allows(r, [heavy, tau, tau])) is None
    gate.weights = {**gate.weights, "heavy": {"cpus": 8, "memory_gib": 3}}
    queue = deque([heavy, tau])
    assert matrix._pop_eligible(queue, {}, admission=lambda r: gate.allows(r, [{"flow_id": "sra"}])) == tau


@pytest.mark.parametrize("patch", [{"docker_cpus": float("nan")}, {"max_workers": True},
                                    {"memory_reserve_gib": 20}, {"max_cpu_utilization": 2}])
def test_invalid_resource_policy_is_rejected(patch):
    with pytest.raises(ValueError):
        validate_policy({**policy(), **patch})


def test_stage_timing_records_failure_once_without_payload(tmp_path, monkeypatch):
    path = tmp_path / "timing.jsonl"
    monkeypatch.setenv("TRIEVO_TIMING_LOG", str(path))
    calls = []
    def failed(secret):
        calls.append(secret)
        raise ValueError("private response")
    with pytest.raises(ValueError):
        timed_call("agent_interaction", failed, "private prompt")
    assert len(calls) == 1
    record = json.loads(path.read_text())
    assert record["success"] is False and record["elapsed_seconds"] >= 0
    assert "private" not in path.read_text()


@pytest.mark.parametrize("flows,expected", [(["skilllearn"] * 3, 2), (["sra", "sra", "tau2", "tau2"], 4)])
def test_real_dispatch_respects_resource_budget_and_drains_every_job(tmp_path, monkeypatch, flows, expected):
    rows = [{"id": str(i), "flow_id": flow, "output": str(tmp_path / str(i))} for i, flow in enumerate(flows)]
    monkeypatch.setattr(matrix, "latest_checkpoint", lambda row: None)
    monkeypatch.setattr(matrix, "matrix", lambda rows: [])
    monkeypatch.setattr(matrix, "_has_matching_report", lambda row: (Path(row["output"]) / "done.json").is_file())
    def report(row):
        path = Path(row["output"]) / "done.json"
        return (str(path), {"ok": True}) if path.is_file() else (None, None)
    monkeypatch.setattr(matrix, "report_for", report)
    def command(repo, row, checkpoint, config):
        path = str(Path(row["output"]) / "done.json")
        code = ("import json,time,pathlib; start=time.monotonic(); time.sleep(.4); "
                f"pathlib.Path({path!r}).write_text(json.dumps([start,time.monotonic()]))")
        return [sys.executable, "-c", code]
    monkeypatch.setattr(matrix, "command", command)
    sleep = time.sleep
    monkeypatch.setattr(matrix.time, "sleep", lambda _: sleep(.02))
    snapshots = []
    original = matrix.save
    def save(path, state):
        snapshots.append(len(state.get("active_jobs", [])))
        original(path, state)
    monkeypatch.setattr(matrix, "save", save)
    gate = LocalResourceGate(policy(), healthy)
    state = {"jobs": rows, "status": "running"}
    assert matrix.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / "status.json", 4,
                               "continue", resource_gate=gate) == 0
    assert max(snapshots) == expected
    assert all(row["status"] == "completed" for row in rows)
    events = []
    for row in rows:
        a, b = json.loads((Path(row["output"]) / "done.json").read_text())
        events += [(a, 1), (b, -1)]
    current = peak = 0
    for _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    assert peak == expected
