from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from trievo_bench import docker_volume, l2_agent as l2_agent_module
from trievo_bench.baselines.base import BaselineReply, EvolutionResult, UsageDetails
from trievo_bench.docker_volume import DockerVolumeError, DockerVolumeMirror
from trievo_bench.l2_agent import L2AgentRunError, L2AgentRunOptions, L2AgentStreamRunner


class FakeStorageGate:
    def __init__(self) -> None:
        self.wait_calls = 0
        self.trip_reasons: list[str] = []

    def wait(self) -> None:
        self.wait_calls += 1

    def trip(self, reason: str) -> None:
        self.trip_reasons.append(reason)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _package(root: Path, *, turns: int = 1) -> Path:
    package = root / "L2_Demo_V3"
    stream = package / "streams" / "stream-a"
    workspace = stream / "agent_workspace"
    workspace.mkdir(parents=True)
    (workspace / "seed.txt").write_text("seed", encoding="utf-8")
    turn_ids = [f"turn{index}" for index in range(1, turns + 1)]
    summaries = []
    for index, turn_id in enumerate(turn_ids, start=1):
        summaries.append({
            "turn_id": turn_id,
            "turn_index": index,
            "sequence_role": "learning" if index == 1 else "application",
        })
        turn = stream / turn_id
        (turn / "assets").mkdir(parents=True)
        (turn / "assets" / "input.txt").write_text(turn_id, encoding="utf-8")
        _write_json(turn / "task.json", {
            "schema_version": "l2_taskstream/1.0",
            "stream_id": "stream-a",
            "turn_id": turn_id,
            "turn_index": index,
            "sequence_role": "learning" if index == 1 else "application",
            "agent_input": {
                "messages": [{"role": "user", "content": f"complete {turn_id}"}],
                "available_tools": ["terminal", "filesystem"],
                "assets": [],
            },
            "workspace_transition": {
                "mode": "continue_with_patch",
                "operations": [{
                    "op": "copy",
                    "source": "assets/input.txt",
                    "target": f"agent_workspace/inputs/{turn_id}.txt",
                }],
            },
            "user_simulator": {"max_generated_messages": 0},
            "evaluation": {
                "evaluation_profile": "general",
                "checks": [{
                    "check_id": "task_completion",
                    "dimension": "task_success",
                    "weight": 1,
                }],
                "success_policy": {"turn_score_threshold": 0.8},
            },
        })
    _write_json(stream / "stream.json", {
        "schema_version": "l2_taskstream/1.0",
        "stream_id": "stream-a",
        "agent_workspace": "agent_workspace",
        "ordering": {"turn_order": turn_ids},
        "execution_requirements": {
            "network": "disabled",
            "path_scope": "agent_workspace",
            "max_interactions": 3,
            "max_tool_calls_per_interaction": 20,
            "turn_timeout_seconds": 900,
            "max_output_tokens": 12000,
        },
        "turns": summaries,
    })
    return package


class Runtime:
    def __init__(self) -> None:
        self.workspace: Path | None = None
        self.turn = ""
        self.interact_calls = 0
        self.evolve_calls = 0

    def start_flow(self, context) -> None:
        self.workspace = context.workspace

    def start_turn(self, context) -> None:
        self.turn = context.turn_id

    def interact(self, _context) -> BaselineReply:
        self.interact_calls += 1
        assert self.workspace is not None
        (self.workspace / f"generated-{self.turn}.txt").write_text("done", encoding="utf-8")
        return BaselineReply(
            text=f"done {self.turn}",
            usage=UsageDetails(total_tokens=10, api_calls=1),
        )

    def save_checkpoint_state(self, path: Path) -> None:
        with zipfile.ZipFile(path, "w"):
            pass

    def evolve(self, context) -> EvolutionResult:
        self.evolve_calls += 1
        return EvolutionResult(
            events=[{"type": "evolution.completed" if context.update_enabled else "evolution.skipped"}],
        )

    def end_flow(self) -> None:
        return None

    def metadata(self):
        return {"adapter": "fake"}


def _options(package: Path, output: Path, condition: str = "evolving") -> L2AgentRunOptions:
    return L2AgentRunOptions(
        package_dir=package,
        stream_id="stream-a",
        output_root=output,
        baseline="hermes",
        baseline_overrides={
            "options": {"l2_agent_limits": {
                "max_model_calls": 24,
                "max_tool_calls": 24,
                "terminal_timeout_seconds": 120,
                "episode_timeout_seconds": 1200,
                "max_output_tokens": 16000,
                "evolution_timeout_seconds": 120,
                "evolution_max_model_calls": 4,
            }},
        },
        max_total_tokens=10000,
        verifier_image="verifier:test",
        browser_verifier_image="browser:test",
        repo_root=package.parent,
        condition=condition,
        evaluation_error_policy="stop",
    )


def _install_fake_runtime(monkeypatch) -> list[Runtime]:
    runtimes: list[Runtime] = []

    def create(*_args, **_kwargs):
        runtime = Runtime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(L2AgentStreamRunner, "_create_runtime", create)
    monkeypatch.setattr(
        L2AgentStreamRunner,
        "_verify_taskstream",
        lambda *_args, **_kwargs: {
            "category": "pass",
            "score": 1.0,
            "details": {},
            "native_result": {
                "status": "pass",
                "score": 1.0,
                "public_summary": {
                    "result_category": "pass",
                    "observable_deficiencies": [],
                },
            },
        },
    )
    monkeypatch.setattr(
        L2AgentStreamRunner,
        "_judge_taskstream",
        lambda *_args, **_kwargs: {
            "score": 1.0,
            "passed": True,
            "feedback": "public",
            "details": {"check_scores": {"task_completion": 1.0}},
        },
    )
    return runtimes


def test_docker_volume_copy_retries_storage_failures_without_reopening_agent_work(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "seed.txt").write_text("seed", encoding="utf-8")
    gate = FakeStorageGate()
    copy_attempts = 0

    def run(args, *, check=True):
        nonlocal copy_attempts
        if args[:1] == ["cp"] and args[2].endswith(":/volume"):
            copy_attempts += 1
            if copy_attempts < 3:
                raise DockerVolumeError("Docker volume command failed (cp): no space left on device")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(docker_volume, "_run_docker", run)
    monkeypatch.setattr(docker_volume, "_storage_recovery_module", lambda: None)
    mirror = DockerVolumeMirror(
        host_path=source,
        role="l2-workspace",
        volume_name="volume",
        carrier_name="carrier",
        helper_image="helper:test",
        uid=1000,
        gid=1000,
        _storage_gate=gate,
    )

    mirror.sync_from_host()

    assert copy_attempts == 3
    assert gate.wait_calls == 3
    assert gate.trip_reasons == [
        "Docker volume command failed (cp): no space left on device",
        "Docker volume command failed (cp): no space left on device",
    ]


def test_l2_volume_pull_storage_retry_does_not_repeat_interact_and_marks_reply_received(
    tmp_path,
    monkeypatch,
):
    package = _package(tmp_path)
    runtimes = _install_fake_runtime(monkeypatch)
    gate = FakeStorageGate()
    pull_attempts = 0
    mirrors: list[DockerVolumeMirror] = []

    def create(host_path, *, role, writable_uid=None, writable_gid=None, helper_image=None):
        mirror = DockerVolumeMirror(
            host_path=Path(host_path).resolve(),
            role=role,
            volume_name=f"volume-{role}",
            carrier_name=f"carrier-{role}",
            helper_image=helper_image or "helper:test",
            uid=1000 if writable_uid is None else writable_uid,
            gid=1000 if writable_gid is None else writable_gid,
            _storage_gate=gate,
        )
        mirrors.append(mirror)
        return mirror

    def run(args, *, check=True):
        nonlocal pull_attempts
        if args[:1] == ["cp"] and str(args[1]).endswith(":/volume/."):
            pull_attempts += 1
            if pull_attempts == 1:
                raise DockerVolumeError("Docker volume command failed (cp): no space left on device")
            staged = Path(args[2])
            (staged / "synced-from-volume.txt").write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv("TRIEVO_DOCKER_VOLUME_TRANSPORT", "required")
    monkeypatch.setattr(l2_agent_module, "_storage_gate_from_environment", lambda: gate)
    monkeypatch.setattr(l2_agent_module.DockerVolumeMirror, "create", create)
    monkeypatch.setattr(docker_volume, "_run_docker", run)
    monkeypatch.setattr(docker_volume, "_storage_recovery_module", lambda: None)

    report = L2AgentStreamRunner(_options(package, tmp_path / "out")).run()

    assert report["completed"] is True
    assert pull_attempts == 3
    assert runtimes[0].interact_calls == 1
    assert runtimes[0].evolve_calls == 1
    assert len(gate.trip_reasons) == 1
    checkpoint = next((tmp_path / "out").glob("_checkpoints/*/*/r*/checkpoint.json"))
    metadata = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert metadata["phase"] == "reply_received"
    assert metadata["workspace_synced"] is False
    assert metadata["resume_safe"] is False
    assert not (checkpoint.parent / "workspace.zip").exists()
    assert not (checkpoint.parent / "evidence.json").exists()
    assert all(mirror._closed for mirror in mirrors)


def test_reply_received_checkpoint_is_not_a_safe_matrix_resume_point(
    tmp_path,
    monkeypatch,
):
    from tools.run_new_package_matrix import latest_checkpoint

    package = _package(tmp_path)
    runtimes = _install_fake_runtime(monkeypatch)
    gate = FakeStorageGate()
    pull_attempts = 0

    def create(host_path, *, role, writable_uid=None, writable_gid=None, helper_image=None):
        return DockerVolumeMirror(
            host_path=Path(host_path).resolve(),
            role=role,
            volume_name=f"volume-{role}",
            carrier_name=f"carrier-{role}",
            helper_image=helper_image or "helper:test",
            uid=1000 if writable_uid is None else writable_uid,
            gid=1000 if writable_gid is None else writable_gid,
            _storage_gate=gate,
        )

    def run(args, *, check=True):
        nonlocal pull_attempts
        if args[:1] == ["cp"] and str(args[1]).endswith(":/volume/."):
            pull_attempts += 1
            raise DockerVolumeError("Docker volume command failed (cp): no space left on device")
        return subprocess.CompletedProcess(args, 0, "", "")

    output = tmp_path / "out"
    monkeypatch.setenv("TRIEVO_DOCKER_VOLUME_TRANSPORT", "required")
    monkeypatch.setattr(l2_agent_module, "_storage_gate_from_environment", lambda: gate)
    monkeypatch.setattr(l2_agent_module.DockerVolumeMirror, "create", create)
    monkeypatch.setattr(docker_volume, "_run_docker", run)
    monkeypatch.setattr(docker_volume, "_storage_recovery_module", lambda: None)

    with pytest.raises(DockerVolumeError, match="no space left on device"):
        L2AgentStreamRunner(_options(package, output)).run()

    assert pull_attempts == 10
    assert runtimes[0].interact_calls == 1
    reply_checkpoint = next(output.glob("_checkpoints/*/*/r*/checkpoint.json"))
    assert json.loads(reply_checkpoint.read_text(encoding="utf-8"))["phase"] == "reply_received"
    row = {
        "level": "l2",
        "agent": "hermes",
        "condition": "evolving",
        "flow_id": "stream-a",
        "turns": ["turn1"],
        "output": str(output),
    }
    with pytest.raises(ValueError, match="safe L2 checkpoint"):
        latest_checkpoint(row)
