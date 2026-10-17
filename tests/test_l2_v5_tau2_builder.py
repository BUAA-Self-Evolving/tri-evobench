"""Tau2 builder isolation and error semantics without live API calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "tau2_builder", Path(__file__).resolve().parents[1] / "tools/l2_v5_tau2.py"
)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


def test_each_fixture_gets_a_fresh_container_and_only_its_own_files(tmp_path):
    tasks = []
    for name in ("task-a", "task-b"):
        task = tmp_path / name
        (task / "evaluation_assets").mkdir(parents=True)
        for fixture in ("known_correct", "known_broken"):
            path = task / "fixtures" / fixture
            path.mkdir(parents=True)
            (path / "response.txt").write_text("{}")
        tasks.append(task)
    stages = []

    def fake_run(image, mounts, command, log, timeout, **kwargs):
        stage = next(iter(mounts))
        stages.append(stage)
        fixture = command[-1]
        # This assertion turns red if the old whole-task/shared-fixture copy returns.
        assert sorted(p.name for p in (stage / "turn/fixtures").iterdir()) == [fixture]
        assert image == "sha256:pinned"
        assert not kwargs.get("network", False)
        result = {"status": "pass" if fixture == "known_correct" else "fail",
                  "score": 1.0 if fixture == "known_correct" else 0.0,
                  "raw_metric": 1.0 if fixture == "known_correct" else 0.0}
        (stage / "result.json").write_text(json.dumps({"result": result, "python": "3.12"}))
        return 0

    with patch.object(builder, "STAGING_ROOT", tmp_path / "scratch"), \
            patch.object(builder, "docker_run", side_effect=fake_run):
        results = builder.qualify_batch(tasks, {"task_image_digest": "sha256:pinned"}, "test")
    assert len(set(stages)) == 4
    assert all(not stage.exists() for stage in stages)
    assert all(builder._valid_failure(r["known_broken"]) for r in results.values())


def test_docker_failure_overrides_a_stale_pass(tmp_path):
    task = tmp_path / "task"
    (task / "evaluation_assets").mkdir(parents=True)
    for fixture in ("known_correct", "known_broken"):
        path = task / "fixtures" / fixture
        path.mkdir(parents=True)
        (path / "response.txt").write_text("{}")

    def fake_run(image, mounts, command, log, timeout, **kwargs):
        stage = next(iter(mounts))
        (stage / "result.json").write_text(json.dumps({"result": {"status": "pass", "score": 1.0}}))
        return 137

    with patch.object(builder, "STAGING_ROOT", tmp_path / "scratch"), \
            patch.object(builder, "docker_run", side_effect=fake_run):
        results = builder.qualify_batch([task], {"task_image_digest": "sha256:pinned"}, "test")
    assert results["task"]["known_correct"]["status"] == "error"
    assert not builder._valid_failure(results["task"]["known_broken"])


def test_python312_rebuild_refuses_released_pool_before_docker(tmp_path):
    args = SimpleNamespace(python312=True, base=None, tag=None)
    immutable = tmp_path / "l2v5_pool" / "tau2"
    with patch.object(builder, "POOL_ROOT", immutable), patch.object(builder.subprocess, "run") as run:
        with pytest.raises(SystemExit, match="released pool is immutable"):
            builder.cmd_build_image(args)
    run.assert_not_called()


def test_native_context_omits_inactive_gold_before_image_layers(tmp_path):
    session = tmp_path / "session"
    (session / "areal").mkdir(parents=True)
    (session / "areal/benchmark_fixes.json").write_text('{"evaluation_criteria": "private gold marker"}')
    (session / "areal/runner.py").write_text("# official carrier preserved")
    native, legacy = tmp_path / "native", tmp_path / "legacy"
    with patch.object(builder, "SESSION_PACKAGE", session):
        builder.stage_session_package(native, python312=True)
        builder.stage_session_package(legacy, python312=False)
    assert not (native / "tau2_session/areal/benchmark_fixes.json").exists()
    assert (native / "tau2_session/areal/runner.py").read_bytes() == (session / "areal/runner.py").read_bytes()
    assert (legacy / "tau2_session/areal/benchmark_fixes.json").exists()


def test_upstream_context_filters_private_payloads_before_copy(tmp_path):
    upstream = tmp_path / "upstream"
    (upstream / "src/tau2/domains/telecom/tasks").mkdir(parents=True)
    (upstream / "src/tau2/data_model").mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (upstream / name).write_text("metadata")
    (upstream / "src/tau2/domains/telecom/tasks/generator.py").write_text("private gold generator")
    (upstream / "src/tau2/domains/telecom/tasks.json").write_text('{"evaluation_criteria":"gold marker"}')
    (upstream / "src/tau2/domains/telecom/tools.py").write_text("# unchanged native tools")
    (upstream / "src/tau2/data_model/tasks.py").write_text("# runtime task schema, not definitions")
    context = tmp_path / "context"
    context.mkdir()
    with patch.object(builder, "CHECKOUT", upstream):
        builder.stage_upstream_source(context)
    source = context / "tau2-src/src/tau2"
    assert not (source / "domains/telecom/tasks").exists()
    assert not (source / "domains/telecom/tasks.json").exists()
    assert (source / "domains/telecom/tools.py").read_text() == "# unchanged native tools"
    assert (source / "data_model/tasks.py").is_file()


@pytest.mark.parametrize("stale_marker", [False, True])
def test_staged_transport_rejects_noop_or_stale_output(tmp_path, stale_marker):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "result.json").write_text('{"status":"pass","score":1.0}')
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if stale_marker and argv[:2] == ["docker", "cp"] and argv[2].endswith("/tau2-transport/result.json"):
            Path(argv[3]).write_text('{"nonce":"old-run","returncode":0}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch.object(builder.subprocess, "run", side_effect=fake_run), \
            patch.object(builder, "require_resource_capability"):
        code = builder.docker_run_staged("sha256:pinned", {stage: "/work"},
                                          ["python", "-c", "pass"], tmp_path / "run.log", 30)
    assert code == 125
    assert (stage / "result.json").read_text() == '{"status":"pass","score":1.0}'
    assert calls[-1][:3] == ["docker", "rm", "-f"]
    assert not any("exec" in call or "-v" in call for call in calls)


@pytest.mark.skipif(__import__('os').environ.get("TRIEVO_TEST_TAU2_DOCKER") != "1", reason="explicit real Docker transport test")
@pytest.mark.parametrize("resource_mode", ["strict", "declared_only"])
def test_real_staged_transport_copies_outputs_and_isolates_containers(tmp_path, monkeypatch, resource_mode):
    monkeypatch.setenv("TRIEVO_RESOURCE_ENFORCEMENT", resource_mode)
    import os
    capability = builder.docker_resource_capability_preflight()
    if not capability.get("allowed", capability["passed"]):
        stage = tmp_path / "blocked"
        stage.mkdir()
        with pytest.raises(builder.ContainerResourceError, match="resource preflight blocked"):
            builder.docker_run_staged("unused-image", {stage: "/work"}, ["python", "-c", "pass"],
                                      tmp_path / "blocked.log", 60)
        assert not list(stage.iterdir())
        assert not json.loads((tmp_path / "blocked.log.resources.json").read_text())["passed"]
        return
    image = builder.image_digest(os.environ.get("TRIEVO_TAU2_TEST_IMAGE", "trievo-l2-v5:0.7-native"))
    stage = tmp_path / "first"
    stage.mkdir()
    first = builder.docker_run_staged(image, {stage: "/work"}, ["python", "-c",
        "from pathlib import Path; Path('/tmp/tau2-private-probe').write_text('private'); "
        "Path('/work/result.json').write_text('first')"], tmp_path / "first.log", 60)
    assert first == 0
    assert (stage / "result.json").read_text() == "first"
    second_stage = tmp_path / "second"
    second_stage.mkdir()
    second = builder.docker_run_staged(image, {second_stage: "/work"}, ["python", "-c",
        "from pathlib import Path; assert not Path('/tmp/tau2-private-probe').exists(); "
        "Path('/work/result.json').write_text('second')"], tmp_path / "second.log", 60)
    assert second == 0
    assert (second_stage / "result.json").read_text() == "second"
    observed = json.loads((tmp_path / "second.log.transport.json").read_text())
    assert observed["network_mode"] == "none"
    assert observed["image_digest"] == image
    assert observed["fresh_container"] is True
    checked = builder.require_container_resources(observed["resource_readback"])
    assert observed["resource_enforcement"] == checked["resource_enforcement"]
    if not checked["validation"]["passed"]:
        assert checked["resource_enforcement"] == "declared_not_enforced"
        assert checked["requested_resources"]["cpu_limit"] == 2
        assert checked["observed_resources"] != checked["requested_resources"]
    assert [name for _, name in observed["network_interfaces"]] == ["lo"]


def test_python312_build_blocks_incapable_daemon_before_context_or_build(tmp_path):
    args = SimpleNamespace(python312=True, base=None, tag=None)
    with patch.object(builder, "POOL_ROOT", tmp_path / "r3"), \
            patch.object(builder, "STAGING_ROOT", tmp_path / "stage"), \
            patch.object(builder, "docker_resource_capability_preflight", return_value={
                "passed": False, "errors": ["no cgroup driver"]}), \
            patch.object(builder, "stage_upstream_source") as stage, \
            patch.object(builder.subprocess, "run") as run:
        with pytest.raises(builder.ContainerResourceError, match="no cgroup driver"):
            builder.cmd_build_image(args)
    stage.assert_not_called()
    run.assert_not_called()
    assert not json.loads((tmp_path / "stage/tau2-resource-capability.json").read_text())["passed"]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("enforced", [False, True])
def test_daemon_runner_checks_actual_resources_before_command(tmp_path, version, enforced):
    import subprocess
    import sys
    payload = tmp_path / "payload"
    payload.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    marker = work / "command-executed"
    snapshot = {
        "schema_version": "l2_container_resources/1.0",
        "cpu_limit": 2.0 if enforced else 108.0,
        "memory_limit_bytes": 4294967296 if enforced else 2019574153216,
        "pids_limit": 256 if enforced else None, "errors": [],
        "controllers": {name: {"version": version, "finite_cgroup_limit": enforced}
                        for name in ("cpu", "memory", "pids")},
    }
    guard = (builder.REPO_ROOT / "trievo_bench/container_resources.py").read_text()
    guard += "\ndef read_container_resources():\n    return " + repr(snapshot) + "\n"
    (payload / "resource_guard.py").write_text(guard)
    (payload / "spec.json").write_text(json.dumps({
        "nonce": "current-run", "mounts": [str(work)], "timeout": 10, "resource_enforcement_mode": "strict",
        "command": [sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()"],
    }))
    (payload / "runner.py").write_text(builder.DAEMON_RUNNER.replace("/tau2-transport", str(payload)))
    process = subprocess.run([sys.executable, str(payload / "runner.py")], capture_output=True, text=True)
    assert process.returncode == (0 if enforced else 125), process.stderr
    observed = json.loads((payload / "result.json").read_text())
    assert observed["nonce"] == "current-run"
    assert marker.exists() == enforced
    assert observed["resource_readback"]["cpu_limit"] == snapshot["cpu_limit"]
    if enforced:
        builder.require_container_resources(observed["resource_readback"])
    else:
        with pytest.raises(builder.ContainerResourceError):
            builder.require_container_resources(observed["resource_readback"])


def test_waiver_metadata_survives_pool_index_runtime_and_adaptation_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIEVO_RESOURCE_ENFORCEMENT", "declared_only")
    snapshot = {"schema_version": "l2_container_resources/1.0", "controllers": {}, "errors": [],
                "cpu_limit": 108.04, "memory_limit_bytes": 2019574153216, "pids_limit": None}
    metadata = builder.resource_metadata(snapshot)
    assert metadata["resource_enforcement"] == "declared_not_enforced"
    assert metadata["requested_resources"]["memory_limit_bytes"] == 4294967296
    image = {"task_image": "existing", "task_image_digest": "sha256:pinned", "python_target": "3.12", **metadata}
    target = tmp_path / "pool/task"
    target.mkdir(parents=True)
    row = {"domain": "retail", "task": {"id": "test"}, "in_v4": False, "in_v5": False,
           "source_id": "tau2_bench:retail:test", "source_task_id": "retail:test", "source_path": "upstream", "splits": []}
    built = builder.write_task(target, row, image, "# scoring", "# verifier")
    assert built["resource_enforcement"] == "declared_not_enforced"
    record = json.loads((target / "pool_task.json").read_text())
    record["qualification"]["fixture_build"] = {"ok": False, "error": "no live call authorized"}
    (target / "pool_task.json").write_text(json.dumps(record))
    previous = tmp_path / "previous"; previous.mkdir()
    r2 = tmp_path / "r2"; r2.mkdir()
    with patch.object(builder, "POOL_ROOT", target.parent), patch.object(builder, "image_record", return_value=image):
        builder.cmd_qualify(SimpleNamespace(batch=1))
        builder.cmd_report(SimpleNamespace(previous_pool=previous, r2_package=r2))
    record = json.loads((target / "pool_task.json").read_text())
    index = json.loads((target.parent / "INDEX.jsonl").read_text())
    ledger = json.loads((target.parent / "ADAPTATIONS.jsonl").read_text())
    for value in (record, record["runtime"], record["qualification"], index, ledger):
        assert value["resource_enforcement"] == "declared_not_enforced"
        assert value["observed_resources"]["memory_limit_bytes"] == 2019574153216
        assert value["resource_readback"]["validation"]["passed"] is False
    assert any("waiver" in note for note in ledger["adaptations"])
    # A stored waiver never authorizes later runs without host opt-in.
    monkeypatch.delenv("TRIEVO_RESOURCE_ENFORCEMENT")
    with pytest.raises(builder.ContainerResourceError):
        builder.resource_metadata(record["resource_readback"])
