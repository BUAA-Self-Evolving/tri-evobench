from __future__ import annotations

import json
import hashlib
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from trievo_bench import l2_agent as l2_agent_module
from trievo_bench.baselines.base import BaselineReply, EvolutionResult, UsageDetails
from trievo_bench.l2_agent import (
    L2AgentRunError,
    L2AgentRunOptions,
    L2AgentStreamRunner,
    _project_bounded_feedback,
    _taskstream_retryable,
    _user_simulator_output_error,
    _TaskSandboxSession,
    _runtime_profile_work_limits,
    L2AgentWorkLimits,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _task_sandbox_subprocess(command, **_kwargs):
    image = "sha256:" + "1" * 64
    if command[1:3] == ["image", "inspect"]:
        return subprocess.CompletedProcess(command, 0, image + "\n", "")
    if command[1] == "run":
        return subprocess.CompletedProcess(command, 0, "container-id\n", "")
    if command[1] == "inspect":
        return subprocess.CompletedProcess(command, 0, f"{image} none true\n", "")
    if command[1] == "rm":
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.CompletedProcess(command, 0, "", "")


def test_v5_task_sandbox_materializes_pinned_offline_container(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    image = "sha256:" + "a" * 64

    class WorkspaceVolume:
        volume_name = "trievo-l2-workspace-test"

        def mount_spec(self, target: str, *, read_only: bool = False) -> str:
            assert read_only is False
            return f"type=volume,src={self.volume_name},dst={target}"

    def run(command, **_kwargs):
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, image + "\n", "")
        if command[1] == "run":
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, f"{image} none true\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(l2_agent_module.subprocess, "run", run)
    session = _TaskSandboxSession(
        profile={
            "runtime_profile_id": "python_v1",
            "task_sandbox_image_digest": image,
            "tool_profile_id": "terminal_filesystem_v1",
            "dependency_class": "python_stdlib",
        },
        workspace=tmp_path,
        workspace_volume=WorkspaceVolume(),
    )
    evidence = session.start()
    session.close()

    run_command = next(command for command in calls if command[1] == "run")
    assert ["--network", "none"] == run_command[run_command.index("--network"):run_command.index("--network") + 2]
    assert "type=volume,src=trievo-l2-workspace-test,dst=/workspace" in run_command
    assert f"{tmp_path.resolve()}:/workspace" not in run_command
    assert evidence["image_digest"] == image
    assert evidence["network_mode"] == "none"
    assert evidence["workspace_transport"] == "docker_volume"


def test_v5_runtime_profile_can_only_tighten_turn_limits() -> None:
    limits = _runtime_profile_work_limits(
        L2AgentWorkLimits(
            max_tool_calls=20,
            terminal_timeout_seconds=90,
            episode_timeout_seconds=900,
            max_output_tokens=12000,
        ),
        {
            "turn_timeout_seconds": 300,
            "max_tool_calls": 6,
            "max_output_tokens": 4096,
        },
    )
    assert limits.episode_timeout_seconds == 300
    assert limits.terminal_timeout_seconds == 90
    assert limits.max_tool_calls == 6
    assert limits.max_output_tokens == 4096


def test_v5_task_local_seed_can_copy_directory_contents_to_workspace_root(
    tmp_path,
) -> None:
    turn_dir = tmp_path / "turn1"
    seed = turn_dir / "workspace_seed"
    seed.mkdir(parents=True)
    (seed / "input.txt").write_text("seed\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = object.__new__(L2AgentStreamRunner)
    runner._taskstream_schema_version = "l2_taskstream/1.2"

    runner._apply_taskstream_transition(
        {
            "workspace_transition": {
                "mode": "task_local_seed",
                "operations": [{
                    "op": "copy",
                    "source": "workspace_seed",
                    "target": "agent_workspace/.",
                }],
            }
        },
        turn_dir,
        workspace,
    )

    assert (workspace / "input.txt").read_text(encoding="utf-8") == "seed\n"


def _package(root: Path, *, turns: int = 2, simulator_messages: int = 0) -> Path:
    package = root / "L2_Demo_V3"
    stream = package / "streams" / "stream-a"
    (stream / "agent_workspace").mkdir(parents=True)
    (stream / "agent_workspace" / "seed.txt").write_text("seed", encoding="utf-8")
    turn_ids = [f"turn{index}" for index in range(1, turns + 1)]
    summaries = []
    for index, turn_id in enumerate(turn_ids, start=1):
        role = "learning" if index == 1 else "probe" if index % 10 == 0 else "application"
        summaries.append({
            "turn_id": turn_id,
            "turn_index": index,
            "sequence_role": role,
        })
        turn = stream / turn_id
        (turn / "assets").mkdir(parents=True)
        (turn / "assets" / "input.txt").write_text(turn_id, encoding="utf-8")
        _write_json(turn / "task.json", {
            "schema_version": "l2_taskstream/1.0",
            "stream_id": "stream-a",
            "turn_id": turn_id,
            "turn_index": index,
            "sequence_role": role,
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
            "user_simulator": {"max_generated_messages": simulator_messages},
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


class _Runtime:
    def __init__(self) -> None:
        self.workspace: Path | None = None
        self.workspace_mount: str | None = None
        self.turn = ""
        self.visible_before_interact: list[set[str]] = []
        self.evolution_contexts = []

    def start_flow(self, context) -> None:
        self.workspace = context.workspace
        self.workspace_mount = context.workspace_mount

    def start_turn(self, context) -> None:
        self.turn = context.turn_id

    def interact(self, context) -> BaselineReply:
        assert self.workspace is not None
        self.visible_before_interact.append({
            path.relative_to(self.workspace).as_posix()
            for path in self.workspace.rglob("*")
            if path.is_file()
        })
        (self.workspace / f"generated-{self.turn}.txt").write_text("done", encoding="utf-8")
        return BaselineReply(
            text=f"done {self.turn}",
            usage=UsageDetails(total_tokens=10, api_calls=1),
        )

    def save_checkpoint_state(self, path: Path) -> None:
        with zipfile.ZipFile(path, "w"):
            pass

    def evolve(self, context) -> EvolutionResult:
        self.evolution_contexts.append(context)
        return EvolutionResult(
            events=[{"type": "evolution.completed" if context.update_enabled else "evolution.skipped"}]
        )

    def end_flow(self) -> None:
        return None

    def metadata(self):
        return {"adapter": "fake"}


def _options(package: Path, output: Path, condition: str, *, error_policy: str = "stop"):
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
            }}
        },
        max_total_tokens=10000,
        verifier_image="verifier:test",
        browser_verifier_image="browser:test",
        repo_root=package.parent,
        condition=condition,
        evaluation_error_policy=error_policy,
    )


def _v5_package(root: Path, *, turns: int = 2) -> Path:
    package = _package(root, turns=turns)
    stream_path = package / "streams/stream-a/stream.json"
    stream = json.loads(stream_path.read_text(encoding="utf-8"))
    stream.update({
        "schema_version": "l2_taskstream/1.2",
        "workspace_persistence": "task_local",
        "stream_design": {
            "pool_id": "intra_benchmark_multi_capability",
            "pool_type": "same_benchmark",
            "composition_policy": "runtime-test",
        },
        "capabilities": [
            {
                "capability_id": f"cap-{index}",
                "domain_id": "software_engineering",
                "task_type_ids": ["code"],
                "source_benchmark": "fixture",
            }
            for index in range(1, 4)
        ],
        "selection_provenance": {"selector": "runtime-test", "seed": 7},
    })
    for index, summary in enumerate(stream["turns"], start=1):
        capability = f"cap-{((index - 1) % 3) + 1}"
        summary.update({
            "capability_id": capability,
            "runtime_profile_id": "python_v1",
        })
        task_path = package / f"streams/stream-a/turn{index}/task.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task.update({
            "schema_version": "l2_taskstream/1.2",
            "runtime_profile_id": "python_v1",
            "taxonomy_ref": {"capability_id": capability},
        })
        task["workspace_transition"]["mode"] = "task_local_seed"
        _write_json(task_path, task)
    _write_json(stream_path, stream)
    runtime_profiles = {
        "schema_version": "l2_runtime_profiles/1.0",
        "profiles": [{
            "runtime_profile_id": "python_v1",
            "task_sandbox_image_digest": "sha256:" + "1" * 64,
            "verifier_image_digest": "sha256:" + "2" * 64,
            "verifier_image_class": "python",
            "tool_profile_id": "terminal_filesystem_v1",
            "dependency_class": "python_stdlib",
            "network": "disabled",
            "turn_timeout_seconds": 900,
            "learner_state_mount_profile": "baseline_explicit_v1",
            "learner_state_abi": "baseline_state/1.0",
            "serialization_version": "1",
            "mount_path": "/baseline_state",
            "owner": "baseline",
            "permission": "rw",
        }],
    }
    pools = {
        "schema_version": "l2_pools/1.0",
        "pools": [{
            "pool_id": "intra_benchmark_multi_capability",
            "pool_type": "same_benchmark",
            "stream_ids": ["stream-a"],
            "composition_policy": "runtime-test",
            "license_scopes": ["permissive"],
            "allows_cross_pool_aggregation": False,
        }],
    }
    panels = {
        "schema_version": "l2_panels/1.0",
        "panels": [{
            "panel_id": "intra_primary",
            "purpose": "primary",
            "pool_id": "intra_benchmark_multi_capability",
            "stream_ids": ["stream-a"],
            "allows_cross_panel_aggregation": False,
        }],
    }
    taxonomy = {"schema_version": "l2_taxonomy/1.0", "taxonomy_version": "test/1.0"}
    documents = {
        "pool_manifest": ("pools.json", pools, "l2_pools/1.0"),
        "panel_manifest": ("panels.json", panels, "l2_panels/1.0"),
        "taxonomy_manifest": ("taxonomy.json", taxonomy, "l2_taxonomy/1.0"),
        "runtime_profiles": ("runtime_profiles.json", runtime_profiles, "l2_runtime_profiles/1.0"),
    }
    bindings = {}
    for key, (name, value, version) in documents.items():
        _write_json(package / name, value)
        bindings[key] = {
            "schema_version": version,
            "path": name,
            "sha256": hashlib.sha256((package / name).read_bytes()).hexdigest(),
        }
    _write_json(package / "manifest.json", {
        "schema_version": "l2_taskstream/1.2",
        "aggregation_profile": "l2_evolution_aggregation/1.2",
        **bindings,
    })
    return package


def _v5_options(package: Path, output: Path, condition: str) -> L2AgentRunOptions:
    options = _options(package, output, condition)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    return replace(
        options,
        pool_id="intra_benchmark_multi_capability",
        pool_manifest_sha256=manifest["pool_manifest"]["sha256"],
        package_manifest_sha256=hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest(),
        runtime_profiles_sha256=manifest["runtime_profiles"]["sha256"],
        panel_id="intra_primary",
        panel_manifest_sha256=manifest["panel_manifest"]["sha256"],
        selected_stream_ids=("stream-a",),
    )


def _install_fake_runtime(monkeypatch, *, judge_score: float = 1.0, verifier_error: bool = False):
    runtimes: list[_Runtime] = []

    def create(*_args, **_kwargs):
        runtime = _Runtime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(L2AgentStreamRunner, "_create_runtime", create)
    if verifier_error:
        monkeypatch.setattr(
            L2AgentStreamRunner,
            "_verify_taskstream",
            lambda *_args, **_kwargs: {
                "category": "error",
                "score": None,
                "details": {"type": "verifier_error", "error": "injected"},
            },
        )
    else:
        monkeypatch.setattr(
            L2AgentStreamRunner,
            "_verify_taskstream",
            lambda *_args, **_kwargs: {
                "category": "pass",
                "score": 1.0,
                "details": {"private": "not public"},
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
                "score": judge_score,
                "passed": judge_score >= 0.8,
                "feedback": "public",
                "details": {"check_scores": {"task_completion": judge_score}},
            },
        )
    return runtimes


@pytest.mark.parametrize(
    ("condition", "runtime_count", "turn2_sees_turn1", "evolution_enabled"),
    [
        ("reset", 2, False, False),
        ("frozen", 1, True, False),
        ("evolving", 1, True, True),
    ],
)
def test_taskstream_run_conditions_are_executable_and_reported(
    tmp_path,
    monkeypatch,
    condition,
    runtime_count,
    turn2_sees_turn1,
    evolution_enabled,
):
    package = _package(tmp_path)
    runtimes = _install_fake_runtime(monkeypatch)

    report = L2AgentStreamRunner(
        _options(package, tmp_path / f"out-{condition}", condition)
    ).run()

    assert report["condition"] == condition
    assert report["completed"] is True
    assert report["evaluation_complete"] is True
    assert report["run_configuration"]["effective_work_limits"]["max_tool_calls"] == 20
    assert report["run_configuration"]["effective_work_limits"]["episode_timeout_seconds"] == 900
    assert report["run_configuration"]["effective_work_limits"]["max_output_tokens"] == 12000
    assert len(runtimes) == runtime_count
    turn2_runtime = runtimes[-1]
    assert ("generated-turn1.txt" in turn2_runtime.visible_before_interact[-1]) is turn2_sees_turn1
    contexts = [context for runtime in runtimes for context in runtime.evolution_contexts]
    assert [context.update_enabled for context in contexts] == [evolution_enabled, evolution_enabled]
    assert [turn["turn_index"] for turn in report["turns"]] == [1, 2]
    assert [turn["sequence_role"] for turn in report["turns"]] == ["learning", "application"]
    assert all(turn["turn_score"] == 1.0 for turn in report["turns"])
    assert all(turn["evaluation_status"] == "passed" for turn in report["turns"])


@pytest.mark.parametrize("condition", ["frozen", "evolving"])
def test_v5_keeps_learner_process_but_rebuilds_task_workspace_each_turn(
    tmp_path, monkeypatch, condition
):
    package = _v5_package(tmp_path)
    runtimes = _install_fake_runtime(monkeypatch)

    report = L2AgentStreamRunner(
        _v5_options(package, tmp_path / f"out-v5-{condition}", condition)
    ).run()

    assert report["completed"] is True
    assert len(runtimes) == 1
    assert "generated-turn1.txt" not in runtimes[0].visible_before_interact[1]
    assert "inputs/turn1.txt" not in runtimes[0].visible_before_interact[1]
    assert "inputs/turn2.txt" in runtimes[0].visible_before_interact[1]


def test_v5_checkpoint_binds_package_pool_profile_and_learner_state(tmp_path, monkeypatch):
    package = _v5_package(tmp_path)
    _install_fake_runtime(monkeypatch)
    output = tmp_path / "out-v5-checkpoint"
    options = replace(_v5_options(package, output, "evolving"), stop_after_turn="turn1")

    L2AgentStreamRunner(options).run()

    checkpoint = next(output.glob("_checkpoints/*/*/t0001/checkpoint.json"))
    metadata = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert metadata["package_manifest_sha256"] == options.package_manifest_sha256
    assert metadata["pool_id"] == options.pool_id
    assert metadata["pool_manifest_sha256"] == options.pool_manifest_sha256
    assert metadata["runtime_profiles_sha256"] == options.runtime_profiles_sha256
    assert metadata["runtime_profile_id"] == "python_v1"
    assert metadata["learner_state_abi"] == "baseline_state/1.0"
    assert len(metadata["learner_state_digest"]) == 64


def test_v5_rejects_task_runtime_profile_not_in_bound_catalog(tmp_path, monkeypatch):
    package = _v5_package(tmp_path, turns=1)
    task_path = package / "streams/stream-a/turn1/task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["runtime_profile_id"] = "missing"
    _write_json(task_path, task)
    _install_fake_runtime(monkeypatch)

    with pytest.raises(L2AgentRunError, match="runtime profile"):
        L2AgentStreamRunner(_v5_options(package, tmp_path / "out", "evolving")).run()


def test_evaluator_error_continue_is_null_unscored_and_does_not_evolve(tmp_path, monkeypatch):
    package = _package(tmp_path)
    runtimes = _install_fake_runtime(monkeypatch, verifier_error=True)

    report = L2AgentStreamRunner(
        _options(package, tmp_path / "out", "evolving", error_policy="continue")
    ).run()

    assert report["completed"] is True
    assert report["evaluation_complete"] is False
    assert report["success"] is False
    assert [turn["turn_score"] for turn in report["turns"]] == [None, None]
    assert [turn["passed"] for turn in report["turns"]] == [None, None]
    assert all(turn["evaluation_status"] == "evaluation_error" for turn in report["turns"])
    assert all(
        not context.update_enabled
        for runtime in runtimes
        for context in runtime.evolution_contexts
    )


def test_evaluator_error_stop_records_null_turn_then_raises(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1)
    _install_fake_runtime(monkeypatch, verifier_error=True)
    output = tmp_path / "out"

    with pytest.raises(L2AgentRunError, match="evaluation_error.*injected"):
        L2AgentStreamRunner(_options(package, output, "evolving")).run()

    report_path = next(output.glob("stream-a/*/report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["turns"][0]["turn_score"] is None
    assert report["turns"][0]["evaluation_status"] == "evaluation_error"


@pytest.mark.parametrize("condition", ["reset", "frozen", "evolving"])
def test_matrix_resume_rejudges_saved_reply_without_repeating_agent_side_effects(tmp_path, monkeypatch, condition):
    from dataclasses import replace
    from tools.run_new_package_matrix import latest_checkpoint

    package = _package(tmp_path, turns=1)
    runtimes = _install_fake_runtime(monkeypatch)
    output = tmp_path / "out"
    options = _options(package, output, condition)

    def failed_judge(*_args, **_kwargs):
        raise RuntimeError("model request failed (HTTP 524)")

    monkeypatch.setattr(L2AgentStreamRunner, "_judge_taskstream", failed_judge)
    with pytest.raises(L2AgentRunError, match="HTTP 524"):
        L2AgentStreamRunner(options).run()

    row = {"level": "l2", "agent": "hermes", "condition": condition, "flow_id": "stream-a",
           "turns": ["turn1"], "output": str(output)}
    checkpoint = latest_checkpoint(row)
    assert json.loads((checkpoint / "checkpoint.json").read_text())["phase"] == "adjudicate"

    def repaired_judge(self, task, reply, *_args, **_kwargs):
        assert task["turn_id"] == "turn1"
        assert reply.text == "done turn1"
        assert (self._runtime.workspace / "generated-turn1.txt").read_text() == "done"
        return {"score": 1.0, "passed": True, "feedback": "public",
                "details": {"check_scores": {"task_completion": 1.0}}}

    monkeypatch.setattr(L2AgentStreamRunner, "_judge_taskstream", repaired_judge)
    report = L2AgentStreamRunner(replace(options, resume_checkpoint=checkpoint)).run()
    assert report["completed"] and report["evaluation_complete"]
    assert report["turns"][0]["turn_score"] == 1.0
    assert sum(len(runtime.visible_before_interact) for runtime in runtimes) == 1


def test_matrix_refuses_error_checkpoint_without_saved_adjudication_evidence(tmp_path, monkeypatch):
    from tools.run_new_package_matrix import latest_checkpoint

    package = _package(tmp_path, turns=1)
    _install_fake_runtime(monkeypatch, verifier_error=True)
    output = tmp_path / "out"
    with pytest.raises(L2AgentRunError):
        L2AgentStreamRunner(_options(package, output, "reset")).run()
    for path in output.glob("_checkpoints/*/*/p*/evidence.json"):
        path.unlink()
    row = {"level": "l2", "agent": "hermes", "condition": "reset", "flow_id": "stream-a",
           "turns": ["turn1"], "output": str(output)}
    with pytest.raises(ValueError, match="safe.*checkpoint"):
        latest_checkpoint(row)


def test_user_simulator_failure_does_not_start_another_interaction(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1, simulator_messages=2)
    runtimes = _install_fake_runtime(monkeypatch, judge_score=0.5)
    monkeypatch.setattr(
        L2AgentStreamRunner,
        "_taskstream_followup",
        lambda *_args, **_kwargs: {
            "status": "error",
            "raw_attempts": ["not-json", "still-not-json"],
            "error": "invalid simulator output",
        },
    )

    report = L2AgentStreamRunner(
        _options(package, tmp_path / "out", "evolving", error_policy="continue")
    ).run()

    assert len(runtimes[0].visible_before_interact) == 1
    turn = report["turns"][0]
    assert turn["turn_score"] is None
    assert turn["evaluation_status"] == "evaluation_error"
    trace = Path(turn["trace_file"]).read_text(encoding="utf-8").splitlines()
    interaction = json.loads(trace[0])
    assert interaction["score"] == 0.5
    assert interaction["evaluation_status"] == "failed"
    assert interaction["terminal_evaluation_error"]["turn_score"] is None


def test_user_simulator_normalizes_requirement_coverage_to_canonical_enum(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1, simulator_messages=1)
    runner = L2AgentStreamRunner(_options(package, tmp_path / "out", "evolving"))
    prompts = []

    class Client:
        def complete(self, messages, **_kwargs):
            prompts.append(messages[-1]["content"])
            return json.dumps({
                "schema_version": "l2_user_simulator_output/1.0",
                "next_user_message": "Please satisfy the missing requirement.",
                "rationale_category": "requirement_coverage",
            })

    monkeypatch.setattr(l2_agent_module, "client_from_config", lambda *_args, **_kwargs: Client())
    task = json.loads(
        (package / "streams/stream-a/turn1/task.json").read_text(encoding="utf-8")
    )
    task["user_simulator"]["simulator_prompt"] = "Latest response: {latest_agent_response}"

    result = runner._taskstream_followup(task, [], "partial", None)

    assert result["status"] == "ok"
    assert result["parsed"]["rationale_category"] == "unanswered_requirement"
    assert result["normalizations"] == [{
        "field": "rationale_category",
        "source": "requirement_coverage",
        "target": "unanswered_requirement",
    }]
    assert "unanswered_requirement" in prompts[0]
    assert "requirement_coverage" not in prompts[0]
    assert _user_simulator_output_error(result["parsed"]) is None
    assert _user_simulator_output_error({
        **result["parsed"], "rationale_category": "task_specific_category",
    }) == "User Simulator rationale_category is invalid"


def test_agent_work_limit_is_a_scored_failure_without_running_evaluator(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1, simulator_messages=2)
    _install_fake_runtime(monkeypatch)

    def limited_interact(self, _context):
        assert self.workspace is not None
        self.visible_before_interact.append(set())
        return BaselineReply(
            text="partial",
            raw={"limit_exhausted": True, "limit_reasons": ["max_tool_calls"]},
        )

    monkeypatch.setattr(_Runtime, "interact", limited_interact)
    monkeypatch.setattr(
        L2AgentStreamRunner,
        "_verify_taskstream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("evaluator must not run after a hard work limit")
        ),
    )

    report = L2AgentStreamRunner(
        _options(package, tmp_path / "out-limit", "evolving")
    ).run()

    turn = report["turns"][0]
    assert turn["turn_score"] == 0.0
    assert turn["evaluation_status"] == "failed"
    assert turn["final_category"] == "execution_limit"
    assert turn["interaction_count"] == 1


def _interact_started_checkpoint(output: Path, turn_id: str) -> Path:
    matches = []
    for path in output.glob("_checkpoints/*/*/i*/checkpoint.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("phase") == "interact_started" and data.get("turn_id") == turn_id:
            matches.append((data.get("invalidated") is True, path.parent))
    assert matches
    return sorted(matches)[0][1]


def test_taskstream_interact_started_marker_survives_process_crash(tmp_path, monkeypatch):
    from tools.run_new_package_matrix import latest_checkpoint

    package = _package(tmp_path, turns=2)
    _install_fake_runtime(monkeypatch)
    original_interact = _Runtime.interact

    def crash_on_turn2(self, context):
        if self.turn == "turn2":
            assert self.workspace is not None
            (self.workspace / "partial-turn2.txt").write_text("partial", encoding="utf-8")
            raise RuntimeError("child process vanished")
        return original_interact(self, context)

    monkeypatch.setattr(_Runtime, "interact", crash_on_turn2)
    output = tmp_path / "out-crash"

    with pytest.raises(RuntimeError, match="vanished"):
        L2AgentStreamRunner(_options(package, output, "reset")).run()

    checkpoint = _interact_started_checkpoint(output, "turn2")
    turn1_started = _interact_started_checkpoint(output, "turn1")
    assert json.loads((turn1_started / "checkpoint.json").read_text(encoding="utf-8"))["invalidated"] is True
    metadata = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))
    assert metadata["phase"] == "interact_started"
    assert metadata.get("invalidated") is not True
    assert metadata["next_turn_index"] == 1
    assert not (checkpoint / "evidence.json").exists()
    assert json.loads((checkpoint / "completed_turns.json").read_text(encoding="utf-8"))[0]["turn_id"] == "turn1"
    row = {"level": "l2", "agent": "hermes", "condition": "reset", "flow_id": "stream-a",
           "turns": ["turn1", "turn2"], "output": str(output)}
    with pytest.raises(ValueError, match="safe L2 checkpoint"):
        latest_checkpoint(row)


@pytest.mark.parametrize("condition", ["reset", "evolving"])
def test_interrupted_marker_resume_requires_exact_authorization_and_stops_after_turn(
    tmp_path,
    monkeypatch,
    condition,
):
    package = _package(tmp_path, turns=3)
    runtimes = _install_fake_runtime(monkeypatch)
    original_interact = _Runtime.interact
    crashed = False

    def crash_once_on_turn2(self, context):
        nonlocal crashed
        if self.turn == "turn2" and not crashed:
            crashed = True
            raise RuntimeError("child process vanished")
        return original_interact(self, context)

    monkeypatch.setattr(_Runtime, "interact", crash_once_on_turn2)
    output = tmp_path / "out-retry"
    options = _options(package, output, condition)
    with pytest.raises(RuntimeError, match="vanished"):
        L2AgentStreamRunner(options).run()
    checkpoint = _interact_started_checkpoint(output, "turn2")
    metadata = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))

    with pytest.raises(L2AgentRunError, match="explicit retry authorization"):
        L2AgentStreamRunner(replace(options, resume_checkpoint=checkpoint)).run()
    authorization = tmp_path / "retry-authorization.json"
    _write_json(authorization, {
        "status": "consumed",
        "failed_run_id": metadata["run_id"],
        "turn_id": "turn2",
        "checkpoint": str(checkpoint),
        "job": f"l2/{condition}/hermes/s01",
        "stream_id": "stream-a",
        "baseline": "hermes",
        "condition": condition,
        "source_run_dir": str(output),
    })
    with pytest.raises(L2AgentRunError, match="turn mismatch"):
        L2AgentStreamRunner(
            replace(
                options,
                resume_checkpoint=checkpoint,
                retry_interrupted_run_id=metadata["run_id"],
                stop_after_turn="turn3",
                retry_authorization=authorization,
                retry_job_id=f"l2/{condition}/hermes/s01",
            )
        ).run()

    report = L2AgentStreamRunner(
        replace(
            options,
            resume_checkpoint=checkpoint,
            retry_interrupted_run_id=metadata["run_id"],
            stop_after_turn="turn2",
            retry_authorization=authorization,
            retry_job_id=f"l2/{condition}/hermes/s01",
        )
    ).run()

    assert report["stopped_after_turn"] == "turn2"
    assert [turn["turn_id"] for turn in report["turns"]] == ["turn1", "turn2"]
    assert all(turn["turn_score"] == 1.0 for turn in report["turns"])
    assert sum(len(runtime.visible_before_interact) for runtime in runtimes) == 2
    with pytest.raises(L2AgentRunError, match="already claimed"):
        L2AgentStreamRunner(
            replace(
                options,
                resume_checkpoint=checkpoint,
                retry_interrupted_run_id=metadata["run_id"],
                stop_after_turn="turn2",
                retry_authorization=authorization,
                retry_job_id=f"l2/{condition}/hermes/s01",
            )
        ).run()
    copied_authorization = tmp_path / "retry-authorization-copy.json"
    copied_authorization.write_text(authorization.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(L2AgentRunError, match="already claimed"):
        L2AgentStreamRunner(
            replace(
                options,
                resume_checkpoint=checkpoint,
                retry_interrupted_run_id=metadata["run_id"],
                stop_after_turn="turn2",
                retry_authorization=copied_authorization,
                retry_job_id=f"l2/{condition}/hermes/s01",
            )
        ).run()


def test_interrupted_marker_retry_reuses_prior_reply_and_only_calls_later_interaction(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1, simulator_messages=2)
    _install_fake_runtime(monkeypatch, judge_score=0.5)
    output = tmp_path / "out-second-interaction"
    options = _options(package, output, "reset")

    def crash_on_second_interaction(self, context):
        if context.interaction_index == 2:
            raise RuntimeError("child process vanished")
        return BaselineReply(text="try again", usage=UsageDetails(total_tokens=10, api_calls=1))

    monkeypatch.setattr(_Runtime, "interact", crash_on_second_interaction)
    monkeypatch.setattr(
        L2AgentStreamRunner,
        "_taskstream_followup",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "raw_attempts": [],
            "parsed": {
                "schema_version": "l2_user_simulator_output/1.0",
                "next_user_message": "try again",
                "rationale_category": "incorrect_result",
            },
        },
    )
    with pytest.raises(RuntimeError, match="vanished"):
        L2AgentStreamRunner(options).run()
    interrupted = _interact_started_checkpoint(output, "turn1")
    metadata = json.loads((interrupted / "checkpoint.json").read_text(encoding="utf-8"))
    assert metadata["interaction_index"] == 2
    pending = interrupted.parent / "p0000-01"
    assert json.loads((pending / "evidence.json").read_text(encoding="utf-8"))["reply"]["text"] == "try again"
    authorization = tmp_path / "retry-second.json"
    _write_json(authorization, {
        "status": "consumed",
        "failed_run_id": metadata["run_id"],
        "turn_id": "turn1",
        "checkpoint": str(pending),
        "interrupted_checkpoint": str(interrupted),
        "retry_interaction_index": 2,
        "job": "l2/reset/hermes/s01",
        "stream_id": "stream-a",
        "baseline": "hermes",
        "condition": "reset",
        "source_run_dir": str(output),
    })
    called_interactions = []

    def finish_second(self, context):
        called_interactions.append(context.interaction_index)
        return BaselineReply(text="fixed", usage=UsageDetails(total_tokens=10, api_calls=1))

    def judge_saved_then_fixed(_self, _task, reply, *_args, **_kwargs):
        score = 1.0 if reply.text == "fixed" else 0.5
        return {"score": score, "passed": score >= 0.8, "feedback": "public",
                "details": {"check_scores": {"task_completion": score}}}

    monkeypatch.setattr(_Runtime, "interact", finish_second)
    monkeypatch.setattr(L2AgentStreamRunner, "_judge_taskstream", judge_saved_then_fixed)
    report = L2AgentStreamRunner(
        replace(
            options,
            resume_checkpoint=pending,
            retry_interrupted_run_id=metadata["run_id"],
            stop_after_turn="turn1",
            retry_authorization=authorization,
            retry_job_id="l2/reset/hermes/s01",
        )
    ).run()

    assert called_interactions == [2]
    assert report["stopped_after_turn"] == "turn1"
    assert report["turns"][0]["interaction_count"] == 2
    trace = [
        json.loads(line)
        for line in Path(report["turns"][0]["trace_file"]).read_text(encoding="utf-8").splitlines()
    ]
    interactions = [item for item in trace if item.get("interaction_index")]
    assert [item["interaction_index"] for item in interactions] == [1, 2]
    assert interactions[0]["response"] == "try again"
    assert report["turns"][0]["turn_score"] == 1.0


def test_general_judge_only_failure_retries_but_auto_research_valid_low_does_not():
    general = {
        "evaluation": {
            "evaluation_profile": "general",
            "success_policy": {"turn_score_threshold": 0.8},
        }
    }
    verification = {
        "category": "pass",
        "native_result": {"status": "pass"},
        "judge": {"score": 0.5},
    }
    assert _taskstream_retryable(general, verification)

    auto = {
        "evaluation": {
            "evaluation_profile": "auto_research",
            "success_policy": {"turn_score_threshold": 1.0},
        }
    }
    assert not _taskstream_retryable(auto, verification)


def test_bounded_feedback_uses_public_summary_and_failed_dimensions_only():
    task = {
        "evaluation": {
            "checks": [{
                "check_id": "evidence",
                "dimension": "execution_integrity",
            }]
        }
    }
    verification = {
        "details": {"private": "secret"},
        "native_result": {
            "private_diagnostics": {"gold": "secret"},
            "public_summary": {
                "result_category": "pass",
                "observable_deficiencies": [],
            },
        },
        "judge": {
            "details": {
                "check_scores": {"evidence": 0.5},
                "rationale": "private rationale",
            }
        },
    }

    feedback = _project_bounded_feedback(task, verification).to_mapping()

    assert feedback["failed_dimensions"] == ["execution_integrity"]
    assert feedback["public_result_category"] == "fail"
    encoded = json.dumps(feedback)
    assert "secret" not in encoded
    assert "private rationale" not in encoded


def test_taskstream_can_select_daemon_owned_volume_transport(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1)
    runtimes = _install_fake_runtime(monkeypatch)
    mirrors = []

    class Mirror:
        def __init__(self, role):
            self.volume_name = f"volume-{role}"
            self.closed = False

        def mount_spec(self, target, *, read_only=False):
            return (
                f"type=volume,src={self.volume_name},dst={target}"
                f"{',readonly' if read_only else ''}"
            )

        def sync_from_host(self):
            return None

        def sync_to_host(self):
            return None

        def close(self):
            self.closed = True

    def create(_path, *, role, **_kwargs):
        mirror = Mirror(role)
        mirrors.append(mirror)
        return mirror

    monkeypatch.setenv("TRIEVO_DOCKER_VOLUME_TRANSPORT", "required")
    monkeypatch.setattr(
        "trievo_bench.l2_agent.DockerVolumeMirror.create",
        create,
    )

    report = L2AgentStreamRunner(
        _options(package, tmp_path / "out-volume", "evolving")
    ).run()

    assert report["run_configuration"]["docker_volume_transport"] is True
    assert runtimes[0].workspace_mount == "volume-l2-workspace"
    assert [mirror.volume_name for mirror in mirrors] == [
        "volume-l2-workspace",
        "volume-l2-package",
    ]
    assert all(mirror.closed for mirror in mirrors)


def test_v5_taskstream_forces_daemon_owned_volume_transport(tmp_path, monkeypatch):
    package = _v5_package(tmp_path, turns=1)
    runtimes = _install_fake_runtime(monkeypatch)
    mirrors = []

    class Mirror:
        def __init__(self, role):
            self.volume_name = f"volume-{role}"
            self.closed = False

        def mount_spec(self, target, *, read_only=False):
            return (
                f"type=volume,src={self.volume_name},dst={target}"
                f"{',readonly' if read_only else ''}"
            )

        def sync_from_host(self):
            return None

        def sync_to_host(self):
            return None

        def close(self):
            self.closed = True

    def create(_path, *, role, **_kwargs):
        mirror = Mirror(role)
        mirrors.append(mirror)
        return mirror

    monkeypatch.delenv("TRIEVO_DOCKER_VOLUME_TRANSPORT", raising=False)
    monkeypatch.setattr(
        "trievo_bench.l2_agent.DockerVolumeMirror.create",
        create,
    )

    report = L2AgentStreamRunner(
        _v5_options(package, tmp_path / "out-v5-forced-volume", "evolving")
    ).run()

    assert report["run_configuration"]["docker_volume_transport"] is True
    assert runtimes[0].workspace_mount == "volume-l2-workspace"
    assert [mirror.volume_name for mirror in mirrors] == [
        "volume-l2-workspace",
        "volume-l2-package",
    ]
    assert all(mirror.closed for mirror in mirrors)


def test_taskstream_closes_volumes_when_runtime_start_fails(tmp_path, monkeypatch):
    package = _package(tmp_path, turns=1)
    mirrors = []

    class Mirror:
        def __init__(self, role):
            self.volume_name = f"volume-{role}"
            self.closed = False

        def sync_from_host(self):
            return None

        def sync_to_host(self):
            return None

        def close(self):
            self.closed = True

    def create(_path, *, role, **_kwargs):
        mirror = Mirror(role)
        mirrors.append(mirror)
        return mirror

    class FailingRuntime(_Runtime):
        def start_flow(self, _context) -> None:
            raise RuntimeError("injected runtime start failure")

    monkeypatch.setenv("TRIEVO_DOCKER_VOLUME_TRANSPORT", "required")
    monkeypatch.setattr(
        "trievo_bench.l2_agent.DockerVolumeMirror.create",
        create,
    )
    monkeypatch.setattr(
        L2AgentStreamRunner,
        "_create_runtime",
        lambda *_args, **_kwargs: FailingRuntime(),
    )

    with pytest.raises(RuntimeError, match="injected runtime start failure"):
        L2AgentStreamRunner(
            _options(package, tmp_path / "out-volume-failure", "evolving")
        ).run()

    assert len(mirrors) == 2
    assert all(mirror.closed for mirror in mirrors)


def test_native_verifier_uses_named_volumes_without_proxy_environment(
    tmp_path,
    monkeypatch,
):
    package = _package(tmp_path, turns=1)
    turn_dir = package / "streams" / "stream-a" / "turn1"
    verifier = turn_dir / "evaluation" / "native" / "verifier.py"
    verifier.parent.mkdir(parents=True)
    verifier.write_text("# test verifier\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = {
        "evaluation": {
            "submission_contract": {"source_binding": "submission"},
            "native_verifier": {
                "verifier_id": "native-test",
                "entrypoint": "evaluation/native/verifier.py",
                "timeout_seconds": 30,
                "input_bindings": [{
                    "name": "submission",
                    "source": "interaction_record",
                    "path": "final_response",
                }],
            },
        },
    }
    captured = {}

    class Mirror:
        def __init__(self, name):
            self.volume_name = name

        def mount_spec(self, target, *, read_only=False):
            return (
                f"type=volume,src={self.volume_name},dst={target}"
                f"{',readonly' if read_only else ''}"
            )

    def run(command, **kwargs):
        captured.setdefault("calls", []).append((command, kwargs))
        result = {
            "schema_version": "l2_native_verifier_result/1.0",
            "verifier_id": "native-test",
            "status": "pass",
            "score": 1.0,
            "error": None,
            "public_summary": {
                "result_category": "pass",
                "observable_deficiencies": [],
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    runner = L2AgentStreamRunner(
        _options(package, tmp_path / "out-verifier", "evolving")
    )
    runner._package_volume = Mirror("package-volume")
    runner._workspace_volume = Mirror("workspace-volume")
    monkeypatch.setattr(l2_agent_module, "_docker_executable", lambda: "docker")
    monkeypatch.setattr(l2_agent_module.subprocess, "run", run)

    result = runner._verify_taskstream(
        task=task,
        turn_dir=turn_dir,
        workspace=workspace,
        reply=BaselineReply(text="answer"),
    )

    command, verifier_kwargs = captured["calls"][0]
    assert result["category"] == "pass"
    assert verifier_kwargs["input"]
    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"
    assert "type=volume,src=package-volume,dst=/opt/l2_demo,readonly" in command
    assert "type=volume,src=workspace-volume,dst=/workspace" in command
    for name in l2_agent_module._VERIFIER_PROXY_ENV_NAMES:
        index = command.index(f"{name}=")
        assert command[index - 1] == "-e"
