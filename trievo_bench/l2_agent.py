"""Run materialized L2 Streams through a real BaselineRuntime.

Unlike the package's legacy text-only runner, this module gives an external
agent a persistent Stream workspace.  Private evaluation files remain mounted
only in a separate verifier container; their paths and contents are never sent
to the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .baselines.base import BaselineReply, EvolutionContext, FlowContext, InteractionContext, TurnContext, UsageDetails
from .baselines.manifest import BaselineManifest, find_manifest, load_manifest, merge_mappings
from .baselines.process import ProcessBaselineRuntime
from .evaluation_outcome import BoundedFeedback, EvaluationOutcome, EvaluationStatus
from .docker_volume import DockerVolumeMirror, docker_volume_transport_enabled
from .model import TokenBudget, client_from_config
from .run_conditions import RunCondition, parse_run_condition
from .verifier import L1LifeJudge


_VERIFIER_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)

# A package verifier may use its declared timeout internally to return a
# deterministic timeout/fail result. The Docker transport must outlive that
# deadline long enough to serialize the result and tear down Chromium.
_VERIFIER_TRANSPORT_GRACE_SECONDS = 30

USER_SIMULATOR_RATIONALE_CATEGORIES = (
    "incomplete_task",
    "incorrect_result",
    "invalid_artifact",
    "missing_evidence",
    "execution_error",
    "unanswered_requirement",
)
USER_SIMULATOR_RATIONALE_ALIASES = {
    "requirement_coverage": "unanswered_requirement",
}


def _verifier_transport_timeout(native_timeout_seconds: int) -> int:
    return native_timeout_seconds + _VERIFIER_TRANSPORT_GRACE_SECONDS


class L2AgentRunError(RuntimeError):
    """Raised when the real-agent L2 runtime cannot safely continue."""


class _TaskSandboxSession:
    """One disposable, network-disabled execution sandbox for a V5 Turn."""

    def __init__(self, *, profile: dict[str, Any], workspace: Path) -> None:
        self.profile = profile
        self.workspace = workspace.resolve()
        self.name = f"trievo-l2-task-{uuid4().hex[:12]}"
        self.image = str(profile["task_sandbox_image_digest"])
        self._started = False
        self.evidence: dict[str, Any] = {}

    def start(self) -> dict[str, Any]:
        inspect = subprocess.run(
            [_docker_executable(), "image", "inspect", self.image, "--format", "{{.Id}}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        actual_image = inspect.stdout.strip()
        if inspect.returncode != 0 or actual_image != self.image:
            raise L2AgentRunError(
                f"task sandbox image is unavailable or digest-mismatched: {self.image}"
            )
        command = [
            _docker_executable(), "run", "-d", "--name", self.name,
            "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--memory", "4g", "--cpus", "2",
            "-v", f"{self.workspace}:/workspace",
            "-w", "/workspace", self.image,
            "sh", "-lc", "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
        ]
        started = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if started.returncode != 0:
            self.close()
            raise L2AgentRunError(
                "failed to materialize task sandbox: " + started.stderr[-2000:]
            )
        self._started = True
        inspected = subprocess.run(
            [
                _docker_executable(), "inspect", self.name, "--format",
                "{{.Image}} {{.HostConfig.NetworkMode}} {{.State.Running}}",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        fields = inspected.stdout.strip().split()
        if inspected.returncode != 0 or fields != [self.image, "none", "true"]:
            self.close()
            raise L2AgentRunError("task sandbox runtime readback failed closed")
        self.evidence = {
            "runtime_profile_id": self.profile["runtime_profile_id"],
            "container_name": self.name,
            "image_digest": actual_image,
            "network_mode": "none",
            "workspace_mount": "/workspace",
            "tool_profile_id": self.profile["tool_profile_id"],
            "dependency_class": self.profile["dependency_class"],
        }
        return dict(self.evidence)

    def close(self) -> None:
        subprocess.run(
            [_docker_executable(), "rm", "-f", self.name],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self._started = False


def _restored_reply(value: dict[str, Any]) -> BaselineReply:
    return BaselineReply(**{**value, "usage": UsageDetails.from_mapping(value.get("usage"))})


@dataclass(frozen=True)
class L2AgentWorkLimits:
    """Hard bounded-autonomy contract for one L2 Episode."""

    max_model_calls: int = 10
    max_tool_calls: int = 8
    terminal_timeout_seconds: int = 90
    episode_timeout_seconds: int = 480
    max_output_tokens: int = 12000
    evolution_timeout_seconds: int = 120
    evolution_max_model_calls: int = 4


@dataclass(frozen=True)
class L2AgentRunOptions:
    package_dir: Path
    stream_id: str
    output_root: Path
    baseline: str
    baseline_overrides: dict[str, Any]
    max_total_tokens: int
    verifier_image: str
    browser_verifier_image: str
    repo_root: Path
    resume_checkpoint: Path | None = None
    condition: str | RunCondition = RunCondition.EVOLVING
    evaluation_error_policy: str = "stop"
    retry_interrupted_run_id: str | None = None
    stop_after_turn: str | None = None
    retry_authorization: Path | None = None
    retry_job_id: str | None = None
    panel_id: str | None = None
    panel_manifest_sha256: str | None = None
    selected_stream_ids: tuple[str, ...] = ()
    pool_id: str | None = None
    pool_manifest_sha256: str | None = None
    package_manifest_sha256: str | None = None
    runtime_profiles_sha256: str | None = None


class L2AgentStreamRunner:
    """Execute one L2 Stream as a sequence of BaselineRuntime Turns."""

    def __init__(self, options: L2AgentRunOptions) -> None:
        self.options = options
        self.package_dir = options.package_dir.resolve()
        self.stream_dir = self.package_dir / "streams" / options.stream_id
        self.budget = TokenBudget(options.max_total_tokens)
        self._runtime: ProcessBaselineRuntime | None = None
        self.slow_provider = options.baseline_overrides.get("options", {}).get("slow_provider", False)
        if not isinstance(self.slow_provider, bool):
            raise L2AgentRunError("options.slow_provider must be a boolean")
        self.work_limits = _work_limits(options.baseline_overrides)
        self.condition = parse_run_condition(
            options.condition,
            default=RunCondition.EVOLVING,
        )
        if options.evaluation_error_policy not in {"stop", "continue"}:
            raise L2AgentRunError(
                "evaluation_error_policy must be 'stop' or 'continue'"
            )
        self.evaluation_error_policy = options.evaluation_error_policy
        self.panel_metadata = self._validate_panel_metadata(options)
        self.v5_metadata: dict[str, Any] = {}
        self._v5_runtime_profiles: dict[str, dict[str, Any]] = {}
        self._current_v5_profile: dict[str, Any] | None = None
        self._taskstream_schema_version = "l2_taskstream/1.0"
        self.budget_profile = _budget_profile(options.baseline_overrides)
        self._applied_stream_limit_overrides: list[dict[str, Any]] = []
        if options.retry_interrupted_run_id and self.condition not in {
            RunCondition.RESET,
            RunCondition.EVOLVING,
        }:
            raise L2AgentRunError(
                "interrupted Interaction retry is only supported for reset or "
                "evolving TaskStream runs"
            )
        if options.retry_interrupted_run_id and (options.resume_checkpoint is None or not options.stop_after_turn):
            raise L2AgentRunError("interrupted Interaction retry requires a resume checkpoint and stop-after-turn")
        if options.retry_interrupted_run_id and not options.retry_job_id:
            raise L2AgentRunError("interrupted Interaction retry requires a matrix job id")
        self._workspace_volume: DockerVolumeMirror | None = None
        self._package_volume: DockerVolumeMirror | None = None
        self._storage_gate = _storage_gate_from_environment()
        self._task_sandbox: _TaskSandboxSession | None = None

    def _validate_panel_metadata(self, options: L2AgentRunOptions) -> dict[str, Any]:
        if options.panel_id is None:
            if options.panel_manifest_sha256 is not None or options.selected_stream_ids:
                raise L2AgentRunError("panel metadata must include panel_id")
            return {}
        if not options.panel_id:
            raise L2AgentRunError("panel_id must be non-empty")
        digest = options.panel_manifest_sha256
        if not isinstance(digest, str) or len(digest) != 64:
            raise L2AgentRunError("panel_manifest_sha256 must be a 64-character hex string")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise L2AgentRunError("panel_manifest_sha256 must be hexadecimal") from exc
        selected = list(options.selected_stream_ids)
        if not selected or len(set(selected)) != len(selected):
            raise L2AgentRunError("selected_stream_ids must be non-empty and unique")
        if options.stream_id not in selected:
            raise L2AgentRunError("selected_stream_ids must contain the current stream")
        return {
            "panel_id": options.panel_id,
            "panel_manifest_sha256": digest,
            "selected_stream_ids": selected,
            "allows_cross_panel_aggregation": False,
        }

    def _checkpoint_panel_metadata(self) -> dict[str, Any]:
        metadata = {**self.panel_metadata, **self.v5_metadata}
        if self._current_v5_profile is not None:
            metadata.update({
                "runtime_profile_id": self._current_v5_profile["runtime_profile_id"],
                "runtime_profile_digest": _canonical_digest(self._current_v5_profile),
                "learner_state_abi": self._current_v5_profile["learner_state_abi"],
                "learner_state_serialization_version": self._current_v5_profile["serialization_version"],
            })
        return metadata

    def _checkpoint_state_metadata(self, state_archive: Path) -> dict[str, Any]:
        return {
            **self._checkpoint_panel_metadata(),
            "learner_state_digest": hashlib.sha256(state_archive.read_bytes()).hexdigest(),
        }

    def run(self) -> dict[str, Any]:
        manifest = _load_json(self.stream_dir / "stream.json")
        # Materialized L2 releases use the self-contained TaskStream contract. Keep the
        # legacy episode runner below unchanged, but route the new contract to
        # its native-verifier carrier instead of pretending that turn*/task.json
        # is the old episode.json/evaluation.json layout.
        if manifest.get("schema_version") in {"l2_taskstream/1.0", "l2_taskstream/1.2"}:
            return self._run_taskstream(manifest)
        if self.condition is not RunCondition.EVOLVING:
            raise L2AgentRunError(
                "reset/frozen reference conditions require l2_taskstream/1.0"
            )
        if manifest.get("level") != "L2":
            raise L2AgentRunError(f"{self.options.stream_id} is not an L2 stream")
        if manifest.get("stream_id") != self.options.stream_id:
            raise L2AgentRunError("stream id does not match stream directory")
        episodes = manifest.get("episodes")
        ordering = manifest.get("ordering", {})
        if not isinstance(episodes, list) or not isinstance(ordering, dict):
            raise L2AgentRunError("stream has invalid episode metadata")
        episode_order = ordering.get("episode_order")
        if not isinstance(episode_order, list) or len(episode_order) != len(episodes):
            raise L2AgentRunError("stream episode order is invalid")

        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        run_dir = self.options.output_root.resolve() / self.options.stream_id / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        # Keep the mutable workspace outside the nested report hierarchy.  On
        # Windows, <output>/<stream>/<run>/workspace/episodes/... can exceed
        # MAX_PATH before an agent has even started.  The report still points
        # to this durable workspace, while traces stay at the requested output
        # location.
        workspace = self.options.output_root.resolve().parent / "_l2_agent_workspaces" / uuid4().hex[:12]
        initial_snapshot = manifest.get("workspace", {}).get("initial_snapshot")
        if not isinstance(initial_snapshot, str):
            raise L2AgentRunError("stream workspace.initial_snapshot is required")
        shutil.copytree(_under(self.stream_dir, initial_snapshot), workspace)

        self._runtime = self._create_runtime()
        report: dict[str, Any] = {
            "schema_version": "l2-agent-runtime-0.1",
            "run_id": run_id,
            "stream_id": self.options.stream_id,
            "baseline": self.options.baseline,
            "started_at": datetime.now(UTC).isoformat(),
            "workspace": str(workspace),
            "episodes": [],
            "success": False,
            "token_budget": asdict(self.budget),
            "work_limits": asdict(self.work_limits),
        }
        _write_json(run_dir / "report.json", report)
        trace_path = run_dir / "trace.jsonl"
        started = time.monotonic()
        previous_exhausted_failure = False
        runtime_started = False
        try:
            self._runtime.start_flow(
                FlowContext(
                    flow_id=self.options.stream_id,
                    workspace=workspace,
                    output_dir=run_dir,
                    metadata={"level": "L2", "stream_id": self.options.stream_id},
                )
            )
            runtime_started = True
            for episode_id, relative in zip(episode_order, episodes, strict=True):
                if not isinstance(episode_id, str) or not isinstance(relative, str):
                    raise L2AgentRunError("episode ids and paths must be strings")
                episode_path = _under(self.stream_dir, relative)
                result = self._run_episode(
                    episode_id=episode_id,
                    episode_path=episode_path,
                    workspace=workspace,
                    run_dir=run_dir,
                    trace_path=trace_path,
                    prepend_next_task_notice=previous_exhausted_failure,
                )
                previous_exhausted_failure = bool(result["exhausted_failure"])
                report["episodes"].append(result["report"])
                report["token_budget"] = asdict(self.budget)
                _write_json(run_dir / "report.json", report)
                if not result["runtime_usable"]:
                    report["error"] = "baseline runtime was stopped by the hard Episode time limit"
                    break
            report["success"] = (
                len(report["episodes"]) == len(episodes)
                and all(item["passed"] for item in report["episodes"])
            )
        except BaseException as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if runtime_started and self._runtime is not None:
                try:
                    self._runtime.end_flow()
                except BaseException as exc:
                    # A transport/container cleanup failure must not discard
                    # the completed per-Episode trace and report. Preserve it
                    # explicitly so callers can distinguish evaluation output
                    # from teardown health.
                    report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["wall_clock_seconds"] = round(time.monotonic() - started, 3)
            report["token_budget"] = asdict(self.budget)
            report["baseline_runtime"] = self._runtime.metadata() if self._runtime else {}
            _write_json(run_dir / "report.json", report)
        return report

    def _run_taskstream(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Execute one l2_taskstream/1.0 Stream with its package-local verifier.

        TaskStream turns carry their own transition and native-verifier
        contracts.  The agent still uses the same BaselineRuntime protocol as
        legacy L2, while the verifier receives the declared path/inline carrier
        over stdin and never receives private evaluation files in its prompt.
        """
        ordering = manifest.get("ordering", {})
        turn_order = ordering.get("turn_order") if isinstance(ordering, dict) else None
        if not isinstance(turn_order, list) or not all(isinstance(item, str) for item in turn_order):
            raise L2AgentRunError("TaskStream ordering.turn_order is invalid")
        if manifest.get("stream_id") != self.options.stream_id:
            raise L2AgentRunError("TaskStream stream id does not match stream directory")
        self._taskstream_schema_version = str(manifest.get("schema_version"))
        is_v5 = self._taskstream_schema_version == "l2_taskstream/1.2"
        if is_v5:
            self.v5_metadata, self._v5_runtime_profiles = _load_v5_runtime_binding(
                self.package_dir,
                manifest,
                self.options,
                self.panel_metadata,
            )
        execution_requirements = _execution_requirements(manifest)
        tool_catalog = _taskstream_tool_catalog(self.package_dir)
        stream_carriers = _stream_submission_carriers(self.stream_dir, turn_order)
        self.work_limits = _effective_work_limits(
            self.work_limits,
            execution_requirements,
            slow_provider=self.slow_provider,
        )
        self.work_limits = _apply_stream_limit_overrides(
            configured=self.work_limits,
            overrides=self.options.baseline_overrides,
            baseline=self.options.baseline,
            stream_id=self.options.stream_id,
            carriers=stream_carriers,
            execution_requirements=execution_requirements,
            budget_profile=self.budget_profile,
            applied=self._applied_stream_limit_overrides,
        )
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        output_root = self.options.output_root.resolve()
        run_dir = output_root / self.options.stream_id / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        agent_workspace = _under(self.stream_dir, str(manifest.get("agent_workspace", "agent_workspace")))
        if not agent_workspace.is_dir():
            raise L2AgentRunError("TaskStream agent_workspace seed is missing")
        resume_index = 0
        resume_state_archive: Path | None = None
        completed_turns: list[dict[str, Any]] = []
        resume_pending = None
        resume_interrupted = False
        workspace = Path(tempfile.mkdtemp(prefix="trievo-l2-work-"))
        if self.options.resume_checkpoint is not None:
            checkpoint = self.options.resume_checkpoint.resolve()
            metadata_path = checkpoint / "checkpoint.json"
            checkpoint_workspace = checkpoint / "workspace.zip"
            resume_state_archive = checkpoint / "baseline_state.zip"
            checkpoint_turns = checkpoint / "completed_turns.json"
            if not all(path.is_file() for path in (metadata_path, checkpoint_workspace, resume_state_archive, checkpoint_turns)):
                raise L2AgentRunError(f"invalid resume checkpoint: {checkpoint}")
            checkpoint_metadata = _load_json(metadata_path)
            if checkpoint_metadata.get("stream_id") != self.options.stream_id:
                raise L2AgentRunError("checkpoint stream identity mismatch")
            if checkpoint_metadata.get("baseline", self.options.baseline) != self.options.baseline:
                raise L2AgentRunError("checkpoint baseline identity mismatch")
            checkpoint_condition = str(
                checkpoint_metadata.get("condition", RunCondition.EVOLVING.value)
            )
            if checkpoint_condition != self.condition.value:
                raise L2AgentRunError("checkpoint run condition mismatch")
            checkpoint_identity = {**self.panel_metadata, **self.v5_metadata}
            if checkpoint_identity:
                for key, expected in checkpoint_identity.items():
                    if checkpoint_metadata.get(key) != expected:
                        raise L2AgentRunError("checkpoint package/pool/panel binding mismatch")
            checkpoint_error_policy = str(
                checkpoint_metadata.get("evaluation_error_policy", "stop")
            )
            if checkpoint_error_policy != self.evaluation_error_policy:
                raise L2AgentRunError("checkpoint evaluation error policy mismatch")
            if self.options.retry_interrupted_run_id:
                _claim_retry_authorization(
                    self.options.retry_authorization,
                    failed_run_id=self.options.retry_interrupted_run_id,
                    turn_id=str(self.options.stop_after_turn),
                    checkpoint=checkpoint,
                    job_id=str(self.options.retry_job_id),
                    stream_id=self.options.stream_id,
                    baseline=self.options.baseline,
                    condition=self.condition.value,
                )
            if checkpoint_metadata.get("phase") == "adjudicate":
                resume_pending = _load_json(checkpoint / "evidence.json")
            elif checkpoint_metadata.get("phase") == "interact_started":
                expected_run = str(checkpoint_metadata.get("run_id", ""))
                expected_turn = str(checkpoint_metadata.get("turn_id", ""))
                if int(checkpoint_metadata.get("interaction_index", -1)) != 1:
                    raise L2AgentRunError("interrupted Interaction retry only supports the first Interaction")
                if (
                    not self.options.retry_interrupted_run_id
                    or self.options.retry_interrupted_run_id != expected_run
                ):
                    raise L2AgentRunError("interrupted Interaction checkpoint requires explicit retry authorization")
                if self.options.stop_after_turn and self.options.stop_after_turn != expected_turn:
                    raise L2AgentRunError("interrupted retry stop-after-turn mismatch")
                resume_interrupted = True
            resume_index = int(checkpoint_metadata["next_turn_index"])
            loaded_turns = json.loads(checkpoint_turns.read_text(encoding="utf-8"))
            if not isinstance(loaded_turns, list) or len(loaded_turns) != resume_index:
                raise L2AgentRunError(f"invalid completed turn records: {checkpoint_turns}")
            completed_turns = loaded_turns
            if [turn.get("turn_id") for turn in completed_turns] != turn_order[:resume_index]:
                raise L2AgentRunError("checkpoint completed prefix differs from requested stream")
            prior_traces = list(output_root.glob(f"{self.options.stream_id}/*-{checkpoint.parent.name}/trace.jsonl"))
            for completed in completed_turns:
                if not Path(str(completed.get("trace_file", ""))).is_absolute() and prior_traces:
                    completed["trace_file"] = str(prior_traces[0])
            self.budget.used = int(checkpoint_metadata.get("token_budget", {}).get("used", 0))
            self.budget.calls = int(checkpoint_metadata.get("token_budget", {}).get("calls", 0))
            if resume_pending is not None or resume_interrupted or self.condition is not RunCondition.RESET:
                _restore_tree_archive(checkpoint_workspace, workspace)
            else:
                shutil.copytree(agent_workspace, workspace, dirs_exist_ok=True)
        else:
            shutil.copytree(agent_workspace, workspace, dirs_exist_ok=True)
        volume_transport = docker_volume_transport_enabled()
        report: dict[str, Any] = {
            "schema_version": "l2-agent-runtime-0.2-taskstream",
            "run_id": run_id,
            "stream_id": self.options.stream_id,
            "baseline": self.options.baseline,
            "condition": self.condition.value,
            **self.panel_metadata,
            "started_at": datetime.now(UTC).isoformat(),
            "turns": completed_turns,
            "success": False,
            "token_budget": asdict(self.budget),
            "work_limits": asdict(self.work_limits),
            "run_configuration": {
                "condition": self.condition.value,
                **self.panel_metadata,
                "budget_profile": self.budget_profile,
                "evaluation_error_policy": self.evaluation_error_policy,
                "execution_requirements": execution_requirements,
                "effective_work_limits": asdict(self.work_limits),
                "submission_carriers": list(stream_carriers),
                "stream_limit_overrides": list(self._applied_stream_limit_overrides),
                "timing_profile": "slow_provider" if self.slow_provider else "dataset",
                "dataset_wall_time_overridden": self.work_limits.episode_timeout_seconds > int(execution_requirements["turn_timeout_seconds"]),
                "docker_volume_transport": volume_transport,
            },
        }
        trace_path = run_dir / "trace.jsonl"
        started = time.monotonic()
        runtime_started = False
        stream_work_limits = self.work_limits
        try:
            if volume_transport:
                self._start_taskstream_volume_transport(workspace)
            if self.condition is not RunCondition.RESET or resume_pending is not None or resume_interrupted:
                self._start_taskstream_runtime(
                    workspace,
                    run_dir,
                    resume_state_archive=resume_state_archive,
                    runtime_output_dir=(
                        run_dir / "runtime_segments" / f"t{resume_index + 1:04d}"
                        if self.condition is RunCondition.RESET
                        else None
                    ),
                )
                runtime_started = True
            for turn_index, turn_id in enumerate(turn_order):
                if turn_index < resume_index:
                    continue
                turn_dir = _under(self.stream_dir, turn_id)
                task = _load_json(turn_dir / "task.json")
                if task.get("stream_id") != self.options.stream_id or task.get("turn_id") != turn_id:
                    raise L2AgentRunError(f"TaskStream identity mismatch for {turn_id}")
                if is_v5:
                    profile_id = task.get("runtime_profile_id")
                    profile = self._v5_runtime_profiles.get(str(profile_id))
                    if profile is None:
                        raise L2AgentRunError(f"unknown runtime profile: {profile_id!r}")
                    self._current_v5_profile = profile
                    self.work_limits = _runtime_profile_work_limits(stream_work_limits, profile)
                if resume_pending is None and not resume_interrupted:
                    if self.condition is RunCondition.RESET:
                        if runtime_started and self._runtime is not None:
                            self._runtime.end_flow()
                            runtime_started = False
                        _replace_tree(agent_workspace, workspace)
                    elif is_v5:
                        _replace_tree(agent_workspace, workspace)
                    self._apply_taskstream_transition(task, turn_dir, workspace)
                    self._sync_workspace_to_volume()
                    if self.condition is RunCondition.RESET:
                        self._start_taskstream_runtime(
                            workspace,
                            run_dir,
                            runtime_output_dir=(
                                run_dir / "runtime_segments" / f"t{turn_index + 1:04d}"
                            ),
                        )
                        runtime_started = True
                task_sandbox_evidence: dict[str, Any] = {}
                if is_v5 and isinstance(self._runtime, ProcessBaselineRuntime):
                    self._close_task_sandbox()
                    if self._current_v5_profile is None:
                        raise L2AgentRunError("V5 Turn has no active runtime profile")
                    self._task_sandbox = _TaskSandboxSession(
                        profile=self._current_v5_profile,
                        workspace=workspace,
                    )
                    task_sandbox_evidence = self._task_sandbox.start()
                sequence_role = _validate_taskstream_turn_identity(
                    manifest,
                    task,
                    turn_index=turn_index + 1,
                    turn_id=turn_id,
                )
                request = _taskstream_request(task, self.work_limits, tool_catalog=tool_catalog)
                simulator = task.get("user_simulator")
                simulator_interactions = (
                    1 + max(0, int(simulator.get("max_generated_messages", 0)))
                    if isinstance(simulator, dict)
                    else 1
                )
                max_interactions = min(
                    int(execution_requirements["max_interactions"]),
                    simulator_interactions,
                )
                if self._runtime is None:
                    raise L2AgentRunError("baseline runtime is not initialized")
                self._runtime.start_turn(
                    TurnContext(
                        turn_id,
                        request,
                        max_interactions,
                        {
                            "level": "L2",
                            "schema_version": self._taskstream_schema_version,
                            "source_format": "l2_taskstream_task_json",
                            "stream_id": self.options.stream_id,
                            "condition": self.condition.value,
                            "agent_limits": asdict(self.work_limits),
                            "submission_contract": _public_submission_contract(task),
                            "available_tools": _public_taskstream_tools(task, tool_catalog),
                            "interaction_timeout_seconds": self.work_limits.episode_timeout_seconds,
                            "slow_provider": self.slow_provider,
                            **(
                                {
                                    "runtime_profile_id": self._current_v5_profile["runtime_profile_id"],
                                    "task_sandbox_image_digest": self._current_v5_profile["task_sandbox_image_digest"],
                                    "tool_profile_id": self._current_v5_profile["tool_profile_id"],
                                    "dependency_class": self._current_v5_profile["dependency_class"],
                                    "network": "disabled",
                                    "task_sandbox": task_sandbox_evidence,
                                }
                                if self._current_v5_profile is not None
                                else {}
                            ),
                        },
                    )
                )
                interactions: list[dict[str, Any]] = list(resume_pending["interactions"]) if resume_pending else []
                history: list[dict[str, Any]] = list(resume_pending["history"]) if resume_pending else []
                current_request = resume_pending["request"] if resume_pending else request
                final_score: float | None = None
                final_category = "fail"
                terminal_outcome: EvaluationOutcome | None = None
                first_interaction = int(resume_pending["interaction_index"]) if resume_pending else 1
                for interaction_index in range(first_interaction, max_interactions + 1):
                    interaction_request = current_request
                    if not resume_pending:
                        self._wait_for_storage(
                            phase="interaction",
                            turn_id=turn_id,
                            interaction_index=interaction_index,
                        )
                    before_files = resume_pending["before_files"] if resume_pending else _workspace_digest(workspace)
                    stream_key = hashlib.sha256(self.options.stream_id.encode("utf-8")).hexdigest()[:10]
                    started_checkpoint = output_root / "_checkpoints" / stream_key / run_id[-8:] / f"i{turn_index:04d}-{interaction_index:02d}"
                    if resume_pending:
                        reply = _restored_reply(resume_pending["reply"])
                    else:
                        started_checkpoint.mkdir(parents=True, exist_ok=True)
                        _archive_tree(workspace, started_checkpoint / "workspace.zip")
                        self._runtime.save_checkpoint_state(started_checkpoint / "baseline_state.zip")
                        _write_json(started_checkpoint / "completed_turns.json", report["turns"])
                        _write_json(started_checkpoint / "checkpoint.json", {
                            "phase": "interact_started", "stream_id": self.options.stream_id,
                            "baseline": self.options.baseline, "condition": self.condition.value,
                            **self._checkpoint_state_metadata(started_checkpoint / "baseline_state.zip"),
                            "evaluation_error_policy": self.evaluation_error_policy,
                            "run_id": run_id, "turn_id": turn_id,
                            "next_turn_index": turn_index, "interaction_index": interaction_index,
                            "token_budget": asdict(self.budget),
                        })
                        reply = self._runtime.interact(
                            InteractionContext(
                                turn_id=turn_id,
                                interaction_index=interaction_index,
                                request=interaction_request,
                                history=history,
                                sandbox={
                                    "workspace": "/workspace",
                                    "files": _public_workspace_files(workspace),
                                    **({"task_runtime": task_sandbox_evidence} if task_sandbox_evidence else {}),
                                },
                                remaining_token_budget=max(0, self.budget.limit - self.budget.used),
                            )
                        )
                        self._write_reply_received_checkpoint(
                            output_root=output_root,
                            stream_key=stream_key,
                            run_id=run_id,
                            turn_id=turn_id,
                            turn_index=turn_index,
                            interaction_index=interaction_index,
                            interaction_request=interaction_request,
                            reply=reply,
                            history=history,
                            interactions=interactions,
                            before_files=before_files,
                            report=report,
                        )
                        self._sync_workspace_from_volume()
                    # Durable stage evidence survives verifier/Judge/Simulator
                    # failures. A resume can reuse this paid Agent execution.
                    stream_key = hashlib.sha256(self.options.stream_id.encode("utf-8")).hexdigest()[:10]
                    pending = output_root / "_checkpoints" / stream_key / run_id[-8:] / f"p{turn_index:04d}-{interaction_index:02d}"
                    pending.mkdir(parents=True, exist_ok=True)
                    _archive_tree(workspace, pending / "workspace.zip")
                    self._runtime.save_checkpoint_state(pending / "baseline_state.zip")
                    _write_json(pending / "evidence.json", {
                        "turn_id": turn_id, "turn_index": turn_index,
                        "interaction_index": interaction_index, "request": interaction_request,
                        "reply": asdict(reply), "history": history, "interactions": interactions,
                        "before_files": before_files, "token_budget": asdict(self.budget),
                    })
                    _write_json(pending / "completed_turns.json", report["turns"])
                    _write_json(pending / "checkpoint.json", {
                        "phase": "adjudicate", "stream_id": self.options.stream_id,
                        "baseline": self.options.baseline, "condition": self.condition.value,
                        **self._checkpoint_state_metadata(pending / "baseline_state.zip"),
                        "evaluation_error_policy": self.evaluation_error_policy,
                        "run_id": run_id, "turn_id": turn_id,
                        "next_turn_index": turn_index, "interaction_index": interaction_index,
                        "token_budget": asdict(self.budget),
                    })
                    if not resume_pending:
                        started_metadata_path = started_checkpoint / "checkpoint.json"
                        if started_metadata_path.is_file():
                            started_metadata = _load_json(started_metadata_path)
                            started_metadata["invalidated"] = True
                            started_metadata["superseded_by"] = str(pending)
                            _write_json(started_metadata_path, started_metadata)
                    resume_pending = None
                    limit_reasons = _limit_reasons(reply.raw)
                    if limit_reasons:
                        verification = _runtime_limit_failure(", ".join(limit_reasons))
                        outcome = EvaluationOutcome.scored(
                            turn_score=0.0,
                            passed=False,
                            bounded_feedback=BoundedFeedback(
                                failed_dimensions=("resource_limit",),
                                observable_deficiency=(
                                    "The task attempt exhausted its declared public work budget."
                                ),
                                public_result_category="fail",
                            ),
                            audit_details={"work_limit": verification},
                        )
                    else:
                        verification = self._verify_taskstream(
                            task=task,
                            turn_dir=turn_dir,
                            workspace=workspace,
                            reply=reply,
                        )
                        if verification["category"] == "error":
                            outcome = EvaluationOutcome.evaluation_error(
                                private_diagnostics={
                                    "stage": "native_verifier",
                                    "details": verification.get("details", {}),
                                },
                                audit_details={"native_verifier": verification},
                            )
                        else:
                            try:
                                verification["judge"] = self._judge_taskstream(
                                    task, reply, verification, interaction_index, history,
                                    before_files, _workspace_digest(workspace),
                                )
                            except Exception as exc:
                                verification["judge_error"] = {
                                    "type": type(exc).__name__,
                                    "error": str(exc),
                                }
                                outcome = EvaluationOutcome.evaluation_error(
                                    private_diagnostics={
                                        "stage": "judge",
                                        "type": type(exc).__name__,
                                        "error": str(exc),
                                    },
                                    audit_details={"native_verifier": verification},
                                )
                            else:
                                score = float(verification["judge"]["score"])
                                threshold = float(
                                    task.get("evaluation", {})
                                    .get("success_policy", {})
                                    .get("turn_score_threshold", 1.0)
                                )
                                passed = score >= threshold
                                feedback = None if passed else _project_bounded_feedback(task, verification)
                                outcome = EvaluationOutcome.scored(
                                    turn_score=score,
                                    passed=passed,
                                    bounded_feedback=feedback,
                                    private_diagnostics={
                                        "native": verification.get("details", {}),
                                        "judge": verification["judge"].get("details", {}),
                                    },
                                    audit_details={"verification": verification},
                                )
                    final_score = outcome.turn_score
                    final_category = (
                        "error"
                        if outcome.evaluation_status is EvaluationStatus.EVALUATION_ERROR
                        else "execution_limit"
                        if verification.get("category") == "execution_limit"
                        else "pass" if outcome.passed else "fail"
                    )
                    interaction_outcome = outcome
                    after_files = _workspace_digest(workspace)
                    public_feedback = outcome.bounded_feedback_mapping()
                    interaction_feedback = public_feedback
                    next_history = list(history)
                    if outcome.evaluation_status is not EvaluationStatus.EVALUATION_ERROR:
                        next_history.extend([
                            {"role": "user", "content": interaction_request},
                            {"role": "assistant", "content": reply.text},
                            {
                                "role": "judge",
                                "content": json.dumps(public_feedback, ensure_ascii=False)
                                if public_feedback is not None
                                else "Task succeeded.",
                            },
                        ])
                    simulator_audit: dict[str, Any] | None = None
                    should_retry = (
                        outcome.evaluation_status is not EvaluationStatus.EVALUATION_ERROR
                        and not bool(outcome.passed)
                        and interaction_index < max_interactions
                        and _taskstream_retryable(task, verification)
                    )
                    if should_retry:
                        simulator_audit = self._taskstream_followup(
                            task,
                            next_history,
                            reply.text,
                            public_feedback,
                        )
                        if simulator_audit["status"] == "ok":
                            current_request = str(simulator_audit["parsed"]["next_user_message"])
                        else:
                            outcome = EvaluationOutcome.evaluation_error(
                                private_diagnostics={
                                    "stage": "user_simulator",
                                    "error": str(simulator_audit.get("error", "invalid output")),
                                },
                                audit_details={
                                    "verification": verification,
                                    "user_simulator": simulator_audit,
                                },
                            )
                            final_score = None
                            final_category = "error"
                            should_retry = False
                    if final_category not in {"error", "execution_limit"}:
                        final_category = "pass" if interaction_outcome.passed else "fail"
                    record = {
                        "turn_id": turn_id,
                        "interaction_index": interaction_index,
                        "request": interaction_request,
                        "response": reply.text,
                        "tool_calls": reply.tool_calls,
                        "events": reply.events,
                        "usage": reply.usage.to_dict(),
                        "workspace_changes": _changed_workspace_files(before_files, after_files),
                        "verifier": verification,
                        "score": interaction_outcome.turn_score,
                        "passed": interaction_outcome.passed,
                        "evaluation_status": interaction_outcome.evaluation_status.value,
                        "bounded_feedback": interaction_feedback,
                    }
                    if simulator_audit is not None:
                        record["user_simulator"] = simulator_audit
                    if outcome is not interaction_outcome:
                        record["terminal_evaluation_error"] = outcome.to_mapping()
                    interactions.append(record)
                    with trace_path.open("a", encoding="utf-8") as trace:
                        trace.write(json.dumps(record, ensure_ascii=False) + "\n")
                    terminal_outcome = outcome
                    if outcome.evaluation_status is EvaluationStatus.EVALUATION_ERROR:
                        break
                    history = next_history
                    if not should_retry:
                        break
                resume_interrupted = False
                if terminal_outcome is None:
                    raise L2AgentRunError(f"TaskStream turn {turn_id} produced no terminal outcome")
                evolution_enabled = (
                    self.condition.policy.sut_evolution_enabled
                    and terminal_outcome.evaluation_status is not EvaluationStatus.EVALUATION_ERROR
                )
                self._wait_for_storage(
                    phase="evolution",
                    turn_id=turn_id,
                    interaction_index=len(interactions),
                )
                evolution = self._runtime.evolve(
                    EvolutionContext(
                        turn_id=turn_id,
                        trajectory=[_public_taskstream_trajectory(item) for item in interactions],
                        feedback=[{
                            "interaction_index": item["interaction_index"],
                            "score": item["score"],
                            "passed": item["passed"],
                            "feedback": item.get("bounded_feedback"),
                        } for item in interactions if item.get("evaluation_status") != "evaluation_error"],
                        update_enabled=evolution_enabled,
                        skip_reason=(
                            "evaluation_error"
                            if terminal_outcome.evaluation_status is EvaluationStatus.EVALUATION_ERROR
                            else f"condition:{self.condition.value}"
                        ),
                    )
                )
                self._sync_workspace_from_volume()
                evolution_record = {"record_type": "evolution", "turn_id": turn_id, "evolution": evolution.to_dict()}
                with trace_path.open("a", encoding="utf-8") as trace:
                    trace.write(json.dumps(evolution_record, ensure_ascii=False) + "\n")
                turn_passed = (
                    bool(terminal_outcome.passed) and evolution.completed
                    if terminal_outcome.passed is not None
                    else None
                )
                report["turns"].append({
                    "condition": self.condition.value,
                    "stream_id": self.options.stream_id,
                    "turn_id": turn_id,
                    "turn_index": turn_index + 1,
                    "sequence_role": sequence_role,
                    "timing_profile": "slow_provider" if self.slow_provider else "dataset",
                    "turn_score": terminal_outcome.turn_score,
                    "evaluation_status": terminal_outcome.evaluation_status.value,
                    "passed": turn_passed,
                    "final_score": final_score,
                    "final_category": final_category if evolution.completed else "evolution_limit",
                    "interaction_count": len(interactions),
                    "trace_file": str(trace_path),
                    "evolution": evolution.to_dict(),
                    "effective_work_limits": asdict(self.work_limits),
                    **({"task_sandbox": task_sandbox_evidence} if task_sandbox_evidence else {}),
                })
                report["token_budget"] = asdict(self.budget)
                _write_json(run_dir / "report.json", report)
                stream_key = hashlib.sha256(self.options.stream_id.encode("utf-8")).hexdigest()[:10]
                checkpoint = output_root / "_checkpoints" / stream_key / run_id[-8:] / f"t{turn_index + 1:04d}"
                checkpoint.mkdir(parents=True, exist_ok=True)
                _archive_tree(workspace, checkpoint / "workspace.zip")
                self._runtime.save_checkpoint_state(checkpoint / "baseline_state.zip")
                report["turns"][-1]["checkpoint"] = str(checkpoint)
                _write_json(checkpoint / "completed_turns.json", report["turns"])
                _write_json(checkpoint / "checkpoint.json", {
                    "stream_id": self.options.stream_id,
                    "baseline": self.options.baseline,
                    "condition": self.condition.value,
                    **self._checkpoint_state_metadata(checkpoint / "baseline_state.zip"),
                    "evaluation_error_policy": self.evaluation_error_policy,
                    "turn_id": turn_id,
                    "completed_turn_index": turn_index,
                    "next_turn_index": turn_index + 1,
                    "token_budget": asdict(self.budget),
                })
                _write_json(run_dir / "report.json", report)
                if (
                    terminal_outcome.evaluation_status is EvaluationStatus.EVALUATION_ERROR
                    and self.evaluation_error_policy == "stop"
                ):
                    raise L2AgentRunError(
                        f"evaluation_error in {turn_id}: "
                        f"{_evaluation_error_summary(terminal_outcome.private_diagnostics)}; "
                        f"resume from {pending} after repairing the evaluator"
                    )
                if self.options.stop_after_turn == turn_id:
                    report["stopped_after_turn"] = turn_id
                    break
            report["completed"] = len(report["turns"]) == len(turn_order)
            report["evaluation_complete"] = all(
                item.get("evaluation_status") != "evaluation_error"
                for item in report["turns"]
            )
            report["success"] = (
                report["completed"]
                and report["evaluation_complete"]
                and all(item.get("passed") is True for item in report["turns"])
            )
        except BaseException as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            self._sync_workspace_from_volume(best_effort=True)
            _archive_tree(workspace, run_dir / "error_workspace.zip")
            raise
        finally:
            self._close_task_sandbox()
            if runtime_started and self._runtime is not None:
                try:
                    self._runtime.end_flow()
                except BaseException as exc:
                    report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["wall_clock_seconds"] = round(time.monotonic() - started, 3)
            report["token_budget"] = asdict(self.budget)
            report["baseline_runtime"] = self._runtime.metadata() if self._runtime else {}
            _write_json(run_dir / "report.json", report)
            self._close_taskstream_volume_transport()
            shutil.rmtree(workspace, ignore_errors=True)
        return report

    def _close_task_sandbox(self) -> None:
        if self._task_sandbox is not None:
            self._task_sandbox.close()
            self._task_sandbox = None

    def _start_taskstream_volume_transport(self, workspace: Path) -> None:
        if self._workspace_volume is not None or self._package_volume is not None:
            raise L2AgentRunError("TaskStream Docker volume transport already started")
        try:
            self._workspace_volume = DockerVolumeMirror.create(
                workspace,
                role="l2-workspace",
            )
            self._package_volume = DockerVolumeMirror.create(
                self.package_dir,
                role="l2-package",
                writable_uid=0,
                writable_gid=0,
            )
        except BaseException:
            self._close_taskstream_volume_transport()
            raise

    def _sync_workspace_to_volume(self) -> None:
        if self._workspace_volume is not None:
            self._workspace_volume.sync_from_host()

    def _sync_workspace_from_volume(self, *, best_effort: bool = False) -> None:
        if self._workspace_volume is None:
            return
        try:
            self._workspace_volume.sync_to_host()
        except BaseException:
            if not best_effort:
                raise

    def _wait_for_storage(
        self,
        *,
        phase: str,
        turn_id: str,
        interaction_index: int | None = None,
    ) -> None:
        gate = self._storage_gate
        if gate is None:
            return
        gate.wait()

    def _write_reply_received_checkpoint(
        self,
        *,
        output_root: Path,
        stream_key: str,
        run_id: str,
        turn_id: str,
        turn_index: int,
        interaction_index: int,
        interaction_request: str,
        reply: BaselineReply,
        history: list[dict[str, Any]],
        interactions: list[dict[str, Any]],
        before_files: dict[str, Any],
        report: dict[str, Any],
    ) -> None:
        if self._runtime is None:
            raise L2AgentRunError("baseline runtime is not initialized")
        checkpoint = output_root / "_checkpoints" / stream_key / run_id[-8:] / f"r{turn_index:04d}-{interaction_index:02d}"
        checkpoint.mkdir(parents=True, exist_ok=True)
        # Persist the reply before *any* Docker state/workspace copy can wait
        # or fail. This is evidence only; the normal adjudication checkpoint
        # below still requires the synchronized workspace and state archive.
        _write_json(checkpoint / "reply_received.json", {
            "turn_id": turn_id,
            "turn_index": turn_index,
            "interaction_index": interaction_index,
            "request": interaction_request,
            "reply": asdict(reply),
            "history": history,
            "interactions": interactions,
            "before_files": before_files,
            "token_budget": asdict(self.budget),
        })
        _write_json(checkpoint / "completed_turns.json", report["turns"])
        _write_json(checkpoint / "checkpoint.json", {
            "phase": "reply_received",
            "stream_id": self.options.stream_id,
            "baseline": self.options.baseline,
            "condition": self.condition.value,
            **self._checkpoint_panel_metadata(),
            "evaluation_error_policy": self.evaluation_error_policy,
            "run_id": run_id,
            "turn_id": turn_id,
            "next_turn_index": turn_index,
            "interaction_index": interaction_index,
            "token_budget": asdict(self.budget),
            "workspace_synced": False,
            "resume_safe": False,
        })

    def _close_taskstream_volume_transport(self) -> None:
        for mirror in (self._package_volume, self._workspace_volume):
            if mirror is not None:
                mirror.close()
        self._package_volume = None
        self._workspace_volume = None

    def _apply_taskstream_transition(self, task: dict[str, Any], turn_dir: Path, workspace: Path) -> None:
        transition = task.get("workspace_transition", {})
        allowed_modes = {"continue", "continue_with_patch"}
        if self._taskstream_schema_version == "l2_taskstream/1.2":
            allowed_modes = {"task_local_seed"}
        if not isinstance(transition, dict) or transition.get("mode") not in allowed_modes:
            raise L2AgentRunError("unsupported TaskStream workspace transition")
        for operation in transition.get("operations", []):
            if not isinstance(operation, dict) or operation.get("op") != "copy":
                raise L2AgentRunError("TaskStream currently supports copy transitions only")
            source = _under(turn_dir, str(operation.get("source", "")))
            target_name = str(operation.get("target", "")).removeprefix("agent_workspace/")
            target = _under(workspace, target_name)
            if target.exists():
                raise L2AgentRunError(f"TaskStream transition target already exists: {target_name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)

    def _verify_taskstream(self, *, task: dict[str, Any], turn_dir: Path, workspace: Path, reply: BaselineReply) -> dict[str, Any]:
        evaluation = task.get("evaluation", {})
        native = evaluation.get("native_verifier", {}) if isinstance(evaluation, dict) else {}
        entrypoint = native.get("entrypoint")
        if not isinstance(entrypoint, str):
            return {"category": "error", "score": None, "details": {"type": "missing_native_verifier"}}
        contract = evaluation.get("submission_contract", {})
        source = str(contract.get("source_binding", "submission"))
        declared = next((item for item in native.get("input_bindings", []) if item.get("name") == source), None)
        if not isinstance(declared, dict):
            return {"category": "error", "score": None, "details": {"type": "missing_submission_binding"}}
        bindings: dict[str, dict[str, Any]] = {}
        for binding in native.get("input_bindings", []):
            if not isinstance(binding, dict) or not isinstance(binding.get("name"), str):
                return {"category": "error", "score": None, "details": {"type": "invalid_native_binding"}}
            binding_name = str(binding["name"])
            binding_source = binding.get("source")
            if binding_source in {"workspace", "evaluation_assets"}:
                binding_path = binding.get("path")
                if not isinstance(binding_path, str) or not binding_path:
                    return {
                        "category": "error",
                        "score": None,
                        "details": {
                            "type": "invalid_native_binding",
                            "name": binding_name,
                            "source": binding_source,
                            "reason": "path must be a non-empty string",
                        },
                    }
                bindings[binding_name] = {"kind": "path", "path": binding_path}
            elif binding_source == "interaction_record" and binding.get("path") == "final_response":
                bindings[binding_name] = {"kind": "inline", "value": reply.text}
            else:
                return {
                    "category": "error",
                    "score": None,
                    "details": {
                        "type": "unsupported_native_binding",
                        "name": binding_name,
                        "source": binding_source,
                        "path": binding.get("path"),
                    },
                }
        request = {
            "schema_version": "l2_native_verifier_request/1.0",
            "bindings": bindings,
        }
        package_root = self.options.package_dir.resolve()
        verifier = _under(turn_dir, entrypoint)
        verifier_rel = verifier.relative_to(package_root).as_posix()
        contract_path = verifier.parent / "verifier_contract.json"
        requires_browser = contract_path.is_file() and "playwright" in contract_path.read_text(encoding="utf-8").lower()
        verifier_image = self.options.browser_verifier_image if requires_browser else self.options.verifier_image
        current_v5_profile = getattr(self, "_current_v5_profile", None)
        if current_v5_profile is not None:
            declared_verifier = current_v5_profile.get("verifier_image_digest")
            if not isinstance(declared_verifier, str) or not declared_verifier.startswith("sha256:"):
                return {
                    "category": "error",
                    "score": None,
                    "details": {"type": "invalid_runtime_profile_verifier_image"},
                }
            verifier_image = declared_verifier
        package_volume = getattr(self, "_package_volume", None)
        workspace_volume = getattr(self, "_workspace_volume", None)
        package_mount = (
            package_volume.mount("/opt/l2_demo", read_only=True)
            if package_volume is not None
            else f"{package_root}:/opt/l2_demo:ro"
        )
        workspace_mount = (
            workspace_volume.mount("/workspace")
            if workspace_volume is not None
            else f"{workspace}:/workspace"
        )
        blocked_proxy_environment = [
            item
            for name in _VERIFIER_PROXY_ENV_NAMES
            for item in ("-e", f"{name}=")
        ]
        verifier_container = f"trievo-l2-verifier-{uuid4().hex[:12]}"
        command = [
            _docker_executable(), "run", "--name", verifier_container, "-i", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
            "-v", package_mount, "-v", workspace_mount,
            "-e", "L2_VERIFIER_WORKSPACE=/workspace", "-e", "PYTHONPATH=/opt/l2_demo/runtime/src", "-e", "MPLBACKEND=Agg",
            *blocked_proxy_environment,
            verifier_image, "python", f"/opt/l2_demo/{verifier_rel}",
        ]
        timeout = int(native.get("timeout_seconds", 300))
        transport_timeout = _verifier_transport_timeout(timeout)
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=transport_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "category": "error",
                "score": None,
                "details": {
                    "type": "verifier_timeout",
                    "timeout_seconds": timeout,
                    "transport_timeout_seconds": transport_timeout,
                    "stdout": str(exc.stdout or "")[-4000:],
                    "stderr": str(exc.stderr or "")[-4000:],
                },
            }
        except (OSError, ValueError) as exc:
            return {
                "category": "error",
                "score": None,
                "details": {
                    "type": "verifier_runtime_error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            }
        finally:
            # Some rootless Docker 24 installations lose attached stdout even
            # though the container log driver retained it. Keep the container
            # until logs are collected, then always remove the exact name.
            if "completed" in locals() and completed.returncode == 0 and not completed.stdout.strip():
                logs = subprocess.run(
                    [_docker_executable(), "logs", verifier_container],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
                if logs.returncode == 0:
                    completed.stdout = logs.stdout
                    completed.stderr = completed.stderr or logs.stderr
            subprocess.run(
                [_docker_executable(), "rm", "-f", verifier_container],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        if completed.returncode != 0:
            return {"category": "error", "score": None, "details": {"type": "verifier_error", "stderr": completed.stderr[-4000:], "stdout": completed.stdout[-4000:]}}
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            return {"category": "error", "score": None, "details": {"type": "invalid_verifier_output", "error": str(exc), "stdout": completed.stdout[-4000:]}}
        error = _native_result_error(native, result)
        if error:
            return {
                "category": "error",
                "score": None,
                "details": {"type": "invalid_verifier_output", "error": error},
                "native_result": result,
            }
        category = str(result["status"])
        return {
            "category": category,
            "score": None if category == "error" else float(result["score"]),
            "details": (
                {
                    "type": "native_verifier_error",
                    "error": result.get("error"),
                    "private_diagnostics": result.get("private_diagnostics", {}),
                }
                if category == "error"
                else result.get("private_diagnostics", {})
            ),
            "native_result": result,
        }

    def _judge_taskstream(self, task: dict[str, Any], reply: BaselineReply, native: dict[str, Any], interaction_index: int, history: list[dict[str, Any]], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        evaluation = task["evaluation"]
        if evaluation.get("type") == "native_verifier_only":
            score = float(native.get("score", 0.0) or 0.0)
            threshold = float(evaluation.get("success_policy", {}).get("turn_score_threshold", 1.0))
            return {
                "score": score,
                "passed": score >= threshold,
                "feedback": "Native verifier score used directly.",
                "details": {"check_scores": {"native_verifier": score}},
            }
        config = evaluation.get("judge_config", {})
        if evaluation.get("type") != "llm_as_judge" or not config.get("judge_prompt"):
            raise L2AgentRunError("TaskStream requires its declared reference judge")
        route = config.get("rubric_route", {})
        routed = {}
        for key in ("domain_rubric", "benchmark_rubric"):
            relative = route.get(key, {}).get("path")
            routed[key] = _under(self.package_dir, relative).read_text(encoding="utf-8") if relative else ""
        values = {
            **routed, "interaction_index": interaction_index,
            "evaluation_profile": evaluation.get("evaluation_profile"),
            "metric_profile": evaluation.get("metric_profile", {}),
            "native_verifier_result": native["native_result"],
            "workspace_diff": {"changed_files": _changed_workspace_files(before, after)},
            "workspace_transition_record": task.get("workspace_transition", {}),
            "execution_error": None, "resource_usage": reply.usage.to_dict(),
        }
        check = {**evaluation, "stream_id": task["stream_id"], "turn_id": task["turn_id"],
                 "turn_type": task.get("turn_type"), "turn_origin": task.get("turn_origin"),
                 "agent_input": task.get("agent_input", {}), "judge_prompt": config["judge_prompt"],
                 "prompt_values": values, "max_output_tokens": 4096,
                 "pass_threshold": evaluation.get("success_policy", {}).get("turn_score_threshold", 1.0)}
        trajectory = {"interactions": history, "tool_calls": reply.tool_calls,
                      "tool_results": [item.get("observation", item.get("result")) for item in reply.tool_calls],
                      "workspace_before": before, "workspace_after": after}
        result = L1LifeJudge(client_from_config({"thinking": "disabled"}, self.budget)).evaluate(check, reply.text, None, trajectory)
        if result.details.get("type") == "l1_life_judge_parse_error":
            raise L2AgentRunError(f"invalid reference judge output: {result.details}")
        component_values = [
            *result.details.get("check_scores", {}).values(),
            *result.details.get("source_rubric_scores", {}).values(),
        ]
        if component_values and all(value is None for value in component_values):
            raise L2AgentRunError("reference judge returned only null components")
        return {"score": result.score, "passed": result.passed, "feedback": result.feedback, "details": result.details}

    def _taskstream_followup(
        self,
        task: dict[str, Any],
        history: list[dict[str, Any]],
        response: str,
        bounded_feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        config = task.get("user_simulator", {})
        attempts: list[str] = []
        last_error = "unknown user-simulator output error"
        for attempt in range(2):
            try:
                prompt = L1LifeJudge._render(str(config.get("simulator_prompt", "")), {
                    "agent_input": task.get("agent_input", {}),
                    "interaction_transcript": history,
                    "latest_agent_response": response,
                    "bounded_evaluation_feedback": bounded_feedback,
                })
                prompt += (
                    "\n\nrationale_category must be exactly one of: "
                    + ", ".join(USER_SIMULATOR_RATIONALE_CATEGORIES)
                    + "."
                )
                if attempt:
                    prompt += (
                        "\n\n[REPAIR]\nThe prior output was invalid: "
                        + last_error
                        + "\nReturn exactly one valid l2_user_simulator_output/1.0 JSON object."
                    )
                text = client_from_config({"thinking": "disabled"}, self.budget).complete(
                    [{"role": "system", "content": "Follow the bounded public user-simulator contract. Return JSON only."},
                     {"role": "user", "content": prompt}], max_tokens=1024, json_mode=True,
                )
                attempts.append(text)
                result = L1LifeJudge._parse(text)
                normalization = None
                if isinstance(result, dict):
                    original_category = result.get("rationale_category")
                    normalized_category = USER_SIMULATOR_RATIONALE_ALIASES.get(original_category)
                    if normalized_category is not None:
                        result = {**result, "rationale_category": normalized_category}
                        normalization = {
                            "field": "rationale_category",
                            "source": original_category,
                            "target": normalized_category,
                        }
                error = _user_simulator_output_error(result)
                if error:
                    raise ValueError(error)
                audit = {"status": "ok", "raw_attempts": attempts, "parsed": result}
                if normalization is not None:
                    audit["normalizations"] = [normalization]
                return audit
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        return {"status": "error", "raw_attempts": attempts, "error": last_error}

    def _start_taskstream_runtime(
        self,
        workspace: Path,
        run_dir: Path,
        *,
        resume_state_archive: Path | None = None,
        runtime_output_dir: Path | None = None,
    ) -> None:
        output_dir = runtime_output_dir or run_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self._runtime = self._create_runtime(resume_state_archive)
        self._runtime.start_flow(
            FlowContext(
                flow_id=self.options.stream_id,
                workspace=workspace,
                output_dir=output_dir,
                metadata={
                    "level": "L2",
                    "schema_version": self._taskstream_schema_version,
                    "stream_id": self.options.stream_id,
                    "condition": self.condition.value,
                },
                workspace_mount=(
                    self._workspace_volume.volume_name
                    if self._workspace_volume is not None
                    else None
                ),
            )
        )

    def _create_runtime(self, resume_state_archive: Path | None = None) -> ProcessBaselineRuntime:
        # L2 uses the same BaselineRuntime protocol as L1.  Resolve the
        # selected adapter from its registered manifest so Hermes, SkillOpt,
        # Trace2Skill, Prime Agent, Adaptive Auto-Harness, and OpenCode can be compared
        # under one evaluator. Previously this method hard-coded Hermes.
        manifest_path = find_manifest(self.options.baseline, self.options.repo_root)
        policy = "turn_boundary" if self.condition.policy.sut_evolution_enabled else "disabled"
        overrides = merge_mappings(
            self.options.baseline_overrides,
            {
                "evolution": {
                    "policy": policy,
                    "timeout_seconds": self.work_limits.evolution_timeout_seconds,
                },
                "options": {"l2_agent_limits": asdict(self.work_limits)},
            },
        )
        manifest = load_manifest(manifest_path).with_overrides(overrides)
        options = manifest.options
        if self.options.baseline == "skillopt":
            current = int(options.get("max_completion_tokens", self.work_limits.max_output_tokens))
            manifest = manifest.with_overrides({
                "options": {"max_completion_tokens": min(current, self.work_limits.max_output_tokens)}
            })
        elif self.options.baseline == "trace2skill":
            current = int(options.get("max_turns", self.work_limits.max_tool_calls))
            manifest = manifest.with_overrides({
                "options": {
                    "max_turns": min(current, self.work_limits.max_tool_calls),
                    "max_tokens": min(
                        int(options.get("max_tokens", self.work_limits.max_output_tokens)),
                        self.work_limits.max_output_tokens,
                    ),
                }
            })
        elif self.options.baseline == "prime-agent":
            manifest = manifest.with_overrides({
                "options": {
                    "max_output_tokens": min(
                        int(options.get("max_output_tokens", self.work_limits.max_output_tokens)),
                        self.work_limits.max_output_tokens,
                    ),
                }
            })
        elif self.options.baseline == "opencode":
            manifest = manifest.with_overrides({
                "options": {
                    "max_output_tokens": min(
                        int(options.get("max_output_tokens", self.work_limits.max_output_tokens)),
                        self.work_limits.max_output_tokens,
                    ),
                    "request_timeout_seconds": min(
                        float(options.get("request_timeout_seconds", self.work_limits.episode_timeout_seconds)),
                        float(self.work_limits.episode_timeout_seconds),
                    ),
                }
            })
        elif self.options.baseline == "adaptive-auto-harness":
            manifest = manifest.with_overrides({
                "options": {
                    "max_steps": min(
                        int(options.get("max_steps", self.work_limits.max_tool_calls)),
                        self.work_limits.max_tool_calls,
                    ),
                    "max_output_tokens": min(
                        int(options.get("max_output_tokens", self.work_limits.max_output_tokens)),
                        self.work_limits.max_output_tokens,
                    ),
                    "evolver_max_tokens": min(
                        int(options.get("evolver_max_tokens", self.work_limits.max_output_tokens)),
                        self.work_limits.max_output_tokens,
                    ),
                }
            })
        return ProcessBaselineRuntime(
            manifest,
            budget=self.budget,
            repo_root=self.options.repo_root,
            resume_state_archive=resume_state_archive,
        )

    def _run_episode(
        self,
        *,
        episode_id: str,
        episode_path: Path,
        workspace: Path,
        run_dir: Path,
        trace_path: Path,
        prepend_next_task_notice: bool,
    ) -> dict[str, Any]:
        if self._runtime is None:
            raise L2AgentRunError("baseline runtime is not initialized")
        episode = _load_json(episode_path)
        if episode.get("episode_id") != episode_id:
            raise L2AgentRunError(f"episode id mismatch for {episode_id}")
        self._apply_workspace_transition(episode, episode_path.parent, workspace)
        evaluation = _load_json(_under(episode_path.parent, str(episode["evaluation_ref"])))
        policy = episode.get("interaction_policy")
        if not isinstance(policy, dict):
            raise L2AgentRunError(f"{episode_id} has no interaction policy")
        max_interactions = policy.get("max_interactions")
        if not isinstance(max_interactions, int) or not 1 <= max_interactions <= 3:
            raise L2AgentRunError(f"{episode_id} has invalid max_interactions")
        threshold = float(evaluation.get("scoring", {}).get("success_threshold", 1.0))
        request = _agent_request(episode_path.parent, episode["agent_view"], self.work_limits)
        if prepend_next_task_notice:
            request = "让我们忽略上一个任务，进行下一个任务。\n\n" + request
        self._runtime.start_turn(
            TurnContext(
                episode_id,
                request,
                max_interactions,
                {
                    "level": "L2",
                    "l2_agent_limits": asdict(self.work_limits),
                    "interaction_timeout_seconds": self.work_limits.episode_timeout_seconds,
                            "slow_provider": self.slow_provider,
                },
            )
        )

        interactions: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        all_events: list[dict[str, Any]] = []
        all_tools: list[dict[str, Any]] = []
        current_request = request
        final_score = 0.0
        final_category = "fail"
        passed = False
        runtime_usable = True
        for interaction_index in range(1, max_interactions + 1):
            before_files = _workspace_digest(workspace)
            try:
                reply = self._runtime.interact(
                    InteractionContext(
                        turn_id=episode_id,
                        interaction_index=interaction_index,
                        request=current_request,
                        history=history,
                        sandbox={"workspace": "/workspace", "files": _public_workspace_files(workspace)},
                        remaining_token_budget=max(0, self.budget.limit - self.budget.used),
                    )
                )
            except Exception as exc:
                after_files = _workspace_digest(workspace)
                final_category = "execution_limit"
                verification = _runtime_limit_failure("episode_timeout", str(exc))
                record = {
                    "turn_id": episode_id,
                    "interaction_index": interaction_index,
                    "request": current_request,
                    "response": "",
                    "tool_calls": [],
                    "events": [{"type": "work_limit.reached", "reason": "episode_timeout"}],
                    "usage": {},
                    "workspace_changes": _changed_workspace_files(before_files, after_files),
                    "submission_source": None,
                    "verifier": verification,
                    "score": 0.0,
                    "passed": False,
                }
                interactions.append(record)
                with trace_path.open("a", encoding="utf-8") as trace:
                    trace.write(json.dumps(record, ensure_ascii=False) + "\n")
                runtime_usable = False
                break
            all_events.extend(reply.events)
            all_tools.extend(reply.tool_calls)
            limit_reasons = _limit_reasons(reply.raw)
            if limit_reasons:
                verification = _runtime_limit_failure(", ".join(limit_reasons))
                submission_source = None
            else:
                raw_submission, submission_source = _submission_text(reply.text, workspace, episode["agent_view"])
                # Keep this verifier-only handoff path short.  On Windows an
                # L2 run directory already carries a long stream/run id, and
                # nesting it under benchmark_private/episodes/... can exceed
                # the host's usable path length before Docker ever sees it.
                raw_path = run_dir / f".submission-{episode_id}-{interaction_index}.txt"
                raw_path.write_text(raw_submission, encoding="utf-8")
                verification = self._verify(
                    evaluation=evaluation,
                    episode_dir=episode_path.parent,
                    workspace=workspace,
                    run_dir=run_dir,
                    raw_submission_path=raw_path,
                )
            final_score = float(verification["score"])
            final_category = str(verification["category"])
            passed = final_score >= threshold
            after_files = _workspace_digest(workspace)
            record = {
                "turn_id": episode_id,
                "interaction_index": interaction_index,
                "request": current_request,
                "response": reply.text,
                "tool_calls": reply.tool_calls,
                "events": reply.events,
                "usage": reply.usage.to_dict(),
                "workspace_changes": _changed_workspace_files(before_files, after_files),
                "submission_source": submission_source,
                "verifier": verification,
                "score": final_score,
                "passed": passed,
            }
            interactions.append(record)
            with trace_path.open("a", encoding="utf-8") as trace:
                trace.write(json.dumps(record, ensure_ascii=False) + "\n")
            history.extend([
                {"role": "user", "content": current_request},
                {"role": "assistant", "content": reply.text},
                {"role": "judge", "content": _bounded_feedback(final_category)},
            ])
            if limit_reasons:
                break
            if passed and bool(policy.get("stop_on_success", True)):
                break
            if interaction_index < max_interactions:
                current_request = _retry_request(final_category)

        evolution_completed = True
        if runtime_usable:
            evolution = self._runtime.evolve(
                EvolutionContext(
                    turn_id=episode_id,
                    trajectory=[_public_trajectory(item) for item in interactions],
                    feedback=[
                        {"interaction_index": item["interaction_index"], "score": item["score"], "passed": item["passed"], "feedback": _bounded_feedback(str(item["verifier"]["category"]))}
                        for item in interactions
                    ],
                )
            )
            evolution_data = evolution.to_dict()
            evolution_completed = evolution.completed
            evolution_record = {"record_type": "evolution", "turn_id": episode_id, "evolution": evolution_data}
            with trace_path.open("a", encoding="utf-8") as trace:
                trace.write(json.dumps(evolution_record, ensure_ascii=False) + "\n")
            if not evolution_completed:
                passed = False
                final_category = "evolution_limit"
        else:
            evolution_data = {"completed": False, "events": [{"type": "evolution.skipped", "reason": "runtime_unavailable"}]}
            evolution_completed = False
        return {
            "exhausted_failure": not passed and (
                len(interactions) == max_interactions
                or any(item["verifier"]["category"] == "execution_limit" for item in interactions)
                or not evolution_completed
            ),
            "runtime_usable": runtime_usable,
            "report": {
                "episode_id": episode_id,
                "passed": passed,
                "final_score": final_score,
                "final_category": final_category,
                "interaction_count": len(interactions),
                "trace_file": "trace.jsonl",
                "evolution": evolution_data,
            },
        }

    def _apply_workspace_transition(self, episode: dict[str, Any], episode_dir: Path, workspace: Path) -> None:
        transition = episode.get("workspace_transition", {})
        if not isinstance(transition, dict):
            raise L2AgentRunError("workspace_transition must be an object")
        mode = transition.get("mode")
        if mode == "continue":
            return
        if mode != "continue_with_patch":
            raise L2AgentRunError(f"unsupported workspace transition: {mode}")
        for item in transition.get("workspace_patch", []):
            if not isinstance(item, dict) or item.get("op") != "copy":
                raise L2AgentRunError("only copy workspace patches are supported")
            source = item.get("from")
            target = item.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                raise L2AgentRunError("workspace patch paths must be strings")
            source_path = _under(episode_dir.parents[1], source)
            target_path = _under(workspace, target.removeprefix("workspace/"))
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if source_path.is_dir():
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(source_path, target_path)
            else:
                shutil.copy2(source_path, target_path)

    def _verify(
        self,
        *,
        evaluation: dict[str, Any],
        episode_dir: Path,
        workspace: Path,
        run_dir: Path,
        raw_submission_path: Path,
    ) -> dict[str, Any]:
        verifier = evaluation.get("verifier")
        if not isinstance(verifier, dict):
            raise L2AgentRunError("private evaluation has no verifier")
        is_browser = verifier.get("type") == "package_local_browser_pytest"
        image = self.options.browser_verifier_image if is_browser else self.options.verifier_image
        episode_relative = episode_dir.relative_to(self.package_dir).as_posix()
        raw_relative = raw_submission_path.relative_to(run_dir).as_posix()
        script = _browser_verify_script(episode_relative) if is_browser else _api_verify_script(episode_relative, raw_relative)
        command = [
            _docker_executable(), "run", "--rm", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
            "-v", f"{self.package_dir}:/opt/l2_demo:ro", "-v", f"{run_dir}:/output",
            "-v", f"{workspace}:/workspace", "-e", "PYTHONPATH=/opt/l2_demo/runtime/src", "-e", "MPLBACKEND=Agg",
            image, "python", "-c", script,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=300)
        if completed.returncode != 0:
            return {"category": "error", "score": 0.0, "details": {"type": "verifier_error", "stderr": completed.stderr[-4000:], "stdout": completed.stdout[-4000:]}}
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            return {"category": "error", "score": 0.0, "details": {"type": "invalid_verifier_output", "error": str(exc), "stdout": completed.stdout[-4000:]}}
        if not isinstance(result, dict):
            return {"category": "error", "score": 0.0, "details": {"type": "invalid_verifier_output"}}
        return {"category": str(result.get("category", "error")), "score": float(result.get("score", 0.0)), "details": result.get("details", {})}


def run_l2_agent_stream(options: L2AgentRunOptions) -> dict[str, Any]:
    return L2AgentStreamRunner(options).run()


def load_baseline_overrides(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    from .baselines.manifest import load_mapping
    return load_mapping(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise L2AgentRunError(f"invalid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise L2AgentRunError(f"JSON root must be an object: {path}")
    return raw


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_digest(value: str | None, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise L2AgentRunError(f"{field} must be a 64-character SHA-256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise L2AgentRunError(f"{field} must be hexadecimal") from exc
    return value


def _load_v5_runtime_binding(
    package_dir: Path,
    stream: dict[str, Any],
    options: L2AgentRunOptions,
    panel_metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = package_dir / "manifest.json"
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != "l2_taskstream/1.2":
        raise L2AgentRunError("1.2 stream requires a 1.2 package manifest")
    package_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if _require_digest(options.package_manifest_sha256, field="package_manifest_sha256") != package_digest:
        raise L2AgentRunError("package manifest SHA-256 does not match manifest.json")
    if not isinstance(options.pool_id, str) or not options.pool_id:
        raise L2AgentRunError("1.2 runtime requires an explicit pool_id")

    catalog_specs = {
        "pool_manifest": ("pools.json", options.pool_manifest_sha256),
        "panel_manifest": ("panels.json", options.panel_manifest_sha256),
        "runtime_profiles": ("runtime_profiles.json", options.runtime_profiles_sha256),
    }
    catalogs: dict[str, dict[str, Any]] = {}
    catalog_digests: dict[str, str] = {}
    for key, (default_path, supplied_digest) in catalog_specs.items():
        binding = manifest.get(key)
        if not isinstance(binding, dict):
            raise L2AgentRunError(f"1.2 manifest is missing {key}")
        path = _under(package_dir, str(binding.get("path", default_path)))
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if binding.get("sha256") != actual:
            raise L2AgentRunError(f"{key} binding does not match package contents")
        if _require_digest(supplied_digest, field=f"{key}_sha256") != actual:
            raise L2AgentRunError(f"runtime {key} SHA-256 does not match package")
        catalogs[key] = _load_json(path)
        catalog_digests[key] = actual

    design = stream.get("stream_design")
    if not isinstance(design, dict) or design.get("pool_id") != options.pool_id:
        raise L2AgentRunError("runtime pool_id does not match Stream design")
    pools = catalogs["pool_manifest"].get("pools")
    pool = next(
        (item for item in pools if isinstance(item, dict) and item.get("pool_id") == options.pool_id),
        None,
    ) if isinstance(pools, list) else None
    if pool is None or options.stream_id not in pool.get("stream_ids", []):
        raise L2AgentRunError("Stream is not selected by the requested pool")
    if not panel_metadata:
        raise L2AgentRunError("1.2 runtime requires explicit panel metadata")
    panels = catalogs["panel_manifest"].get("panels")
    panel = next(
        (item for item in panels if isinstance(item, dict) and item.get("panel_id") == options.panel_id),
        None,
    ) if isinstance(panels, list) else None
    if panel is None or panel.get("pool_id") != options.pool_id:
        raise L2AgentRunError("runtime panel does not belong to the requested pool")
    if panel.get("stream_ids") != list(options.selected_stream_ids):
        raise L2AgentRunError("runtime panel Stream selection does not match panels.json")

    raw_profiles = catalogs["runtime_profiles"].get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise L2AgentRunError("runtime_profiles.json must declare profiles")
    profiles = {
        str(item.get("runtime_profile_id")): item
        for item in raw_profiles
        if isinstance(item, dict) and isinstance(item.get("runtime_profile_id"), str)
    }
    referenced_ids = {
        str(item.get("runtime_profile_id"))
        for item in stream.get("turns", [])
        if isinstance(item, dict)
    }
    missing = sorted(referenced_ids - profiles.keys())
    if missing:
        raise L2AgentRunError(f"unknown runtime profile(s): {missing}")
    abi_fields = (
        "learner_state_abi",
        "serialization_version",
        "mount_path",
        "owner",
        "permission",
    )
    abi_values = {tuple(profiles[profile_id].get(field) for field in abi_fields) for profile_id in referenced_ids}
    if len(abi_values) != 1 or any(not value for value in next(iter(abi_values), ())):
        raise L2AgentRunError("runtime profiles have incompatible learner-state ABI")
    for profile_id in referenced_ids:
        profile = profiles[profile_id]
        if profile.get("network") != "disabled":
            raise L2AgentRunError(f"runtime profile {profile_id} must disable network")
        image = profile.get("task_sandbox_image_digest")
        if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
            raise L2AgentRunError(f"runtime profile {profile_id} image digest is not pinned")
        verifier_image = profile.get("verifier_image_digest")
        if (
            not isinstance(verifier_image, str)
            or not verifier_image.startswith("sha256:")
            or len(verifier_image) != 71
        ):
            raise L2AgentRunError(
                f"runtime profile {profile_id} verifier image digest is not pinned"
            )

    return ({
        "package_manifest_sha256": package_digest,
        "pool_id": options.pool_id,
        "pool_manifest_sha256": catalog_digests["pool_manifest"],
        "runtime_profiles_sha256": catalog_digests["runtime_profiles"],
        "runtime_profile_set_digest": _canonical_digest(
            [profiles[profile_id] for profile_id in sorted(referenced_ids)]
        ),
        "learner_state_abi_set_digest": _canonical_digest(sorted(abi_values)),
        "allows_cross_pool_aggregation": False,
    }, profiles)


def _under(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise L2AgentRunError(f"path escapes allowed root: {relative}")
    return candidate


def _archive_tree(source: Path, archive: Path) -> None:
    """Archive a workspace without materializing deep checkpoint paths."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in source.rglob("*"):
            if path.is_file() and not path.is_symlink():
                bundle.write(path, arcname=path.relative_to(source).as_posix())


def _restore_tree_archive(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise L2AgentRunError(f"unsafe workspace checkpoint entry: {name}")
            destination = target.joinpath(*relative.parts)
            if name.endswith("/"):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(name))


def _agent_request(episode_dir: Path, agent_view: Any, limits: L2AgentWorkLimits) -> str:
    if not isinstance(agent_view, dict):
        raise L2AgentRunError("episode agent_view must be an object")
    request = agent_view.get("request")
    if not isinstance(request, str) or not request.strip():
        raise L2AgentRunError("episode agent_view.request must be non-empty")
    parts = [
        "You are a real task agent with terminal tools and a persistent /workspace directory.",
        "Complete the task by inspecting and editing only /workspace. Do not seek or infer private evaluator files.",
        (
            "This Episode has a hard work budget enforced by the evaluator: at most "
            f"{limits.max_model_calls} model calls, {limits.max_tool_calls} tool calls, "
            f"{limits.max_output_tokens} output tokens, "
            f"{limits.terminal_timeout_seconds} seconds for each terminal command, and "
            f"{limits.episode_timeout_seconds} seconds total. Reaching any limit immediately "
            "ends this Episode and records it as a failure. Do not run exhaustive parameter "
            "grids or background jobs; first create a simple valid deliverable, then perform "
            "at most one modest validation."
        ),
        "In your final response return exactly the requested deliverable, because the evaluator consumes that response; also leave any requested files in /workspace.",
        "\nTASK:\n" + request,
    ]
    request_file = agent_view.get("request_file")
    if isinstance(request_file, str):
        sidecar = _public_sidecar(episode_dir, request_file)
        parts.append(f"\n<AGENT_VISIBLE_REQUEST path={json.dumps(request_file)}>\n{sidecar.read_text(encoding='utf-8')}\n</AGENT_VISIBLE_REQUEST>")
    tools = agent_view.get("available_tools")
    if tools is not None:
        parts.append("\n<AVAILABLE_TOOLS>\n" + json.dumps(tools, ensure_ascii=False, indent=2) + "\n</AVAILABLE_TOOLS>")
    assets = agent_view.get("assets", [])
    if not isinstance(assets, list) or not all(isinstance(item, str) for item in assets):
        raise L2AgentRunError("agent_view.assets must be a list of strings")
    for asset in assets:
        if asset == "assets" or asset.startswith("assets/"):
            sidecar = _public_sidecar(episode_dir, asset)
            parts.append(f"\n<AGENT_VISIBLE_ASSET path={json.dumps(asset)}>\n{sidecar.read_text(encoding='utf-8')}\n</AGENT_VISIBLE_ASSET>")
        else:
            parts.append(f"\n<WORKSPACE_ASSET path={json.dumps(asset)} />")
    return "\n".join(parts)


def _taskstream_request(
    task: dict[str, Any],
    limits: L2AgentWorkLimits | None = None,
    *,
    tool_catalog: dict[str, Any] | None = None,
) -> str:
    agent_input = task.get("agent_input")
    if not isinstance(agent_input, dict) or not isinstance(agent_input.get("messages"), list):
        raise L2AgentRunError(f"{task.get('turn_id', '<unknown>')} agent_input.messages is required")
    messages = agent_input["messages"]
    transcript = "\n\n".join(
        f"{str(item.get('role', 'user')).upper()}:\n{str(item.get('content', ''))}"
        for item in messages if isinstance(item, dict)
    )
    if not transcript.strip():
        raise L2AgentRunError(f"{task.get('turn_id', '<unknown>')} has an empty public transcript")
    tools = _public_taskstream_tools(task, tool_catalog or {})
    assets = agent_input.get("assets", [])
    carrier_instruction = _public_submission_instruction(task)
    return "\n".join([
        "You are a real task agent with terminal tools and a persistent /workspace directory.",
        "Complete the public task by inspecting and editing only /workspace. Do not seek private evaluation assets.",
        "The current TaskStream turn is self-contained; prior public messages are included below in order.",
        (f"Per-interaction resource ceiling: {limits.max_model_calls} model calls, {limits.max_tool_calls} tool calls, "
         f"{limits.max_output_tokens} output tokens, {limits.terminal_timeout_seconds} seconds per terminal command, "
         f"{limits.episode_timeout_seconds} seconds total. "
         "Finish the required artifact before the tool budget is exhausted." if limits else ""),
        "\nPUBLIC TRANSCRIPT:\n" + transcript,
        "\nAVAILABLE TOOLS:\n" + json.dumps(tools, ensure_ascii=False, indent=2),
        "\nDECLARED WORKSPACE ASSETS:\n" + json.dumps(assets, ensure_ascii=False, indent=2),
        carrier_instruction,
    ])


def _public_submission_instruction(task: dict[str, Any]) -> str:
    """Project only the public output carrier, never private verifier data."""

    contract = _public_submission_contract(task)
    mode = contract.get("mode")
    payload_kind = contract.get("payload_kind")
    if mode == "workspace_artifact":
        paths = contract.get("required_paths", [])
        suffix = f" Required verifier-visible path(s): {', '.join(paths)}." if paths else ""
        return (
            "SUBMISSION CONTRACT: create the requested artifact in /workspace using actual tool calls; "
            "a prose claim or final-response-only substitute is not a submission."
            + suffix
        )
    if mode == "final_response" and payload_kind == "python_source":
        return (
            "SUBMISSION CONTRACT: your final response itself must be only the complete Python source code. "
            "Do not return a summary or file link, and do not rely on a Workspace file as the submission."
        )
    if mode == "final_response" and payload_kind == "json_tool_trajectory":
        return (
            "SUBMISSION CONTRACT: your final response itself must be one strict JSON object with exactly "
            "the keys schema_version, assistant_message, and tool_calls. schema_version must be "
            "tau2-telecom-trajectory-0.1. Every tool_calls item must contain exactly requestor, name, and "
            "arguments; requestor is user or assistant, and name must come from AVAILABLE TOOLS. Do not "
            "emit user_tool_calls, assistant_tool_calls, nested chat messages, tool results, extra keys, "
            "Markdown fences, Thought/Action protocol text, or prose outside the JSON object."
        )
    return "Return the requested deliverable in your final response and leave required files in /workspace."


def _taskstream_tool_catalog(package_dir: Path) -> dict[str, Any]:
    path = package_dir / "tools.json"
    # Synthetic/unit packages created before Contract V1 may omit the catalog;
    # retain name-only tools for backward-compatible smoke execution.
    if not path.is_file():
        return {}
    raw = _load_json(path)
    if raw.get("schema_version") != "l2_tools/1.0" or not isinstance(raw.get("tools"), dict):
        raise L2AgentRunError(f"invalid TaskStream tool catalog: {path}")
    return dict(raw["tools"])


def _public_taskstream_tools(task: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    agent_input = task.get("agent_input", {})
    names = agent_input.get("available_tools", []) if isinstance(agent_input, dict) else []
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise L2AgentRunError("agent_input.available_tools must be a list of tool names")
    selected: list[dict[str, Any]] = []
    for name in names:
        definition = catalog.get(name)
        if catalog and not isinstance(definition, dict):
            raise L2AgentRunError(f"TaskStream tool {name!r} is missing from tools.json")
        selected.append({"name": name, **(definition if isinstance(definition, dict) else {})})
    return selected


def _public_taskstream_trajectory(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "interaction_index": item.get("interaction_index"),
        "request": item.get("request"),
        "response": item.get("response"),
        "tool_calls": item.get("tool_calls", []),
        "events": item.get("events", []),
        "workspace_changes": item.get("workspace_changes", []),
        "score": item.get("score"),
        "passed": item.get("passed"),
    }


def _taskstream_retryable(task: dict[str, Any], verification: dict[str, Any]) -> bool:
    native_status = verification.get("native_result", {}).get("status")
    if native_status == "error" or verification.get("category") == "error":
        return False
    evaluation = task.get("evaluation", {})
    profile = evaluation.get("evaluation_profile", "general") if isinstance(evaluation, dict) else "general"
    if profile == "auto_research":
        # A native pass means the artifact was valid and measurable. Its metric
        # may still be low, but Contract V1 terminates that Interaction.
        return native_status == "fail"
    threshold = float(evaluation.get("success_policy", {}).get("turn_score_threshold", 1.0))
    judge_score = verification.get("judge", {}).get("score")
    return native_status in {"fail", "partial"} or (
        isinstance(judge_score, (int, float))
        and not isinstance(judge_score, bool)
        and float(judge_score) < threshold
    )


def _public_sidecar(episode_dir: Path, relative: str) -> Path:
    candidate = _under(episode_dir, relative)
    public_root = (episode_dir / "assets").resolve()
    if public_root not in candidate.parents or not candidate.is_file():
        raise L2AgentRunError(f"invalid public sidecar: {relative}")
    return candidate


def _submission_text(reply: str, workspace: Path, agent_view: Any) -> tuple[str, str]:
    request = str(agent_view.get("request", "")) if isinstance(agent_view, dict) else ""
    marker = "Return one complete Python script, "
    if marker in request:
        filename = request.split(marker, 1)[1].split(",", 1)[0].strip()
        candidate = _under(workspace, filename)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8"), f"workspace:{filename}"
    return reply, "final_response"


def _api_verify_script(episode_relative: str, raw_relative: str) -> str:
    return (
        "import json; from pathlib import Path; "
        "from l2_runtime.runner import _load_verifier, _verify_with_workspace; "
        f"ep=Path('/opt/l2_demo/{episode_relative}'); ev=json.loads((ep/'evaluation.json').read_text()); "
        f"raw=Path('/output/{raw_relative}').read_text(); "
        "verifier=_load_verifier(ev['verifier']['entrypoint'], allow_unsafe_execution=True); "
        "result=_verify_with_workspace(verifier, raw, ep, Path('/workspace')); "
        "print(json.dumps({'category': result.category, 'score': result.score, 'details': result.details}, ensure_ascii=False))"
    )


def _browser_verify_script(episode_relative: str) -> str:
    return (
        "import importlib.util, json; from pathlib import Path; "
        f"ep=Path('/opt/l2_demo/{episode_relative}'); source=ep/'evaluation_assets'/'verifier.py'; "
        "spec=importlib.util.spec_from_file_location('l2_browser_verifier', source); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "result=module.FileArtifactVerifier(allow_unsafe_execution=True).verify(Path('/workspace/output'), ep); "
        "print(json.dumps({'category': result.category, 'score': result.score, 'details': result.details}, ensure_ascii=False))"
    )


def _workspace_digest(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if not path.is_file() or path.is_symlink() or ".l2_state" in path.parts:
            continue
        relative = path.relative_to(workspace).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _public_workspace_files(workspace: Path) -> list[dict[str, Any]]:
    return [{"path": path, "sha256": digest} for path, digest in _workspace_digest(workspace).items()]


def _changed_workspace_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _bounded_feedback(category: str) -> str:
    return f"The previous submission was scored as {category}. Continue using only the public task requirements."


def _retry_request(category: str) -> str:
    return "The previous submission did not pass (" + category + "). Please revise the task deliverable using the workspace and return the complete required final format."


def _work_limits(overrides: dict[str, Any]) -> L2AgentWorkLimits:
    raw_options = overrides.get("options", {}) if isinstance(overrides, dict) else {}
    raw = raw_options.get("l2_agent_limits", {}) if isinstance(raw_options, dict) else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise L2AgentRunError("options.l2_agent_limits must be an object")
    defaults = asdict(L2AgentWorkLimits())
    values: dict[str, int] = {}
    ranges = {
        "max_model_calls": (2, 100),
        "max_tool_calls": (1, 100),
        "terminal_timeout_seconds": (1, 600),
        "episode_timeout_seconds": (30, 86400 if raw_options.get("slow_provider") is True else 3600),
        "max_output_tokens": (1, 1_000_000),
        "evolution_timeout_seconds": (10, 86400 if raw_options.get("slow_provider") is True else 900),
        "evolution_max_model_calls": (1, 16),
    }
    for name, (minimum, maximum) in ranges.items():
        value = raw.get(name, defaults[name])
        if isinstance(value, bool):
            raise L2AgentRunError(f"options.l2_agent_limits.{name} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise L2AgentRunError(f"options.l2_agent_limits.{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise L2AgentRunError(
                f"options.l2_agent_limits.{name} must be between {minimum} and {maximum}"
            )
        values[name] = parsed
    if values["episode_timeout_seconds"] <= values["terminal_timeout_seconds"]:
        raise L2AgentRunError("Episode time limit must exceed the terminal-command time limit")
    return L2AgentWorkLimits(**values)


def _budget_profile(overrides: dict[str, Any]) -> str:
    raw_options = overrides.get("options", {}) if isinstance(overrides, dict) else {}
    profile = raw_options.get("budget_profile", "formal") if isinstance(raw_options, dict) else "formal"
    if profile not in {"formal", "qualification"}:
        raise L2AgentRunError("options.budget_profile must be 'formal' or 'qualification'")
    return str(profile)


def _execution_requirements(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("execution_requirements")
    if not isinstance(raw, dict):
        raise L2AgentRunError("TaskStream execution_requirements is required")
    if raw.get("network") != "disabled":
        raise L2AgentRunError("TaskStream execution_requirements.network must be disabled")
    if raw.get("path_scope") != "agent_workspace":
        raise L2AgentRunError("TaskStream execution_requirements.path_scope must be agent_workspace")
    result: dict[str, Any] = {
        "network": "disabled",
        "path_scope": "agent_workspace",
    }
    for name in (
        "max_interactions",
        "max_tool_calls_per_interaction",
        "turn_timeout_seconds",
        "max_output_tokens",
    ):
        value = raw.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise L2AgentRunError(f"execution_requirements.{name} must be a positive integer")
        result[name] = value
    if result["max_interactions"] > 3:
        raise L2AgentRunError("execution_requirements.max_interactions must not exceed 3")
    return result


def _public_submission_contract(task: dict[str, Any]) -> dict[str, Any]:
    evaluation = task.get("evaluation", {})
    if not isinstance(evaluation, dict):
        return {}
    contract = evaluation.get("submission_contract", {})
    native = evaluation.get("native_verifier", {})
    if not isinstance(contract, dict) or not isinstance(native, dict):
        return {}
    result: dict[str, Any] = {}
    mode = contract.get("mode")
    if isinstance(mode, str) and mode:
        result["mode"] = mode
    payload_kind = contract.get("payload_kind")
    if isinstance(payload_kind, str) and payload_kind:
        result["payload_kind"] = payload_kind
    source = str(contract.get("source_binding", "submission"))
    if source:
        result["source_binding"] = source
    bindings = native.get("input_bindings", [])
    declared = next(
        (
            item for item in bindings
            if isinstance(item, dict) and item.get("name") == source
        ),
        None,
    )
    if not isinstance(declared, dict) or declared.get("source") != "workspace":
        return result
    raw_path = declared.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return result
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts:
        return result
    if path.parts and path.parts[0] == "agent_workspace":
        path = PurePosixPath(*path.parts[1:])
    display_path = "/workspace" + (f"/{path.as_posix()}" if path.as_posix() != "." else "")
    result["required_paths"] = [display_path]
    return result


def _stream_submission_carriers(stream_dir: Path, turn_order: list[str]) -> tuple[str, ...]:
    carriers: set[str] = set()
    for turn_id in turn_order:
        task = _load_json(_under(stream_dir, f"{turn_id}/task.json"))
        evaluation = task.get("evaluation", {})
        contract = evaluation.get("submission_contract", {}) if isinstance(evaluation, dict) else {}
        mode = contract.get("mode")
        if isinstance(mode, str) and mode:
            carriers.add(mode)
            continue
        source = str(contract.get("source_binding", "submission"))
        native = evaluation.get("native_verifier", {}) if isinstance(evaluation, dict) else {}
        bindings = native.get("input_bindings", []) if isinstance(native, dict) else []
        declared = next((item for item in bindings if isinstance(item, dict) and item.get("name") == source), None)
        binding_source = declared.get("source") if isinstance(declared, dict) else None
        if binding_source == "interaction_record":
            carriers.add("final_response")
        elif binding_source == "workspace":
            carriers.add("workspace_artifact")
        else:
            carriers.add("unknown")
    return tuple(sorted(carriers))


def _apply_stream_limit_overrides(
    *,
    configured: L2AgentWorkLimits,
    overrides: dict[str, Any],
    baseline: str,
    stream_id: str,
    carriers: tuple[str, ...],
    execution_requirements: dict[str, Any],
    budget_profile: str,
    applied: list[dict[str, Any]],
) -> L2AgentWorkLimits:
    raw_options = overrides.get("options", {}) if isinstance(overrides, dict) else {}
    raw = raw_options.get("l2_stream_limit_overrides", []) if isinstance(raw_options, dict) else []
    if raw in (None, []):
        return configured
    if not isinstance(raw, list):
        raise L2AgentRunError("options.l2_stream_limit_overrides must be a list")
    values = asdict(configured)
    allowed_fields = set(values)
    formal_caps = {
        "max_tool_calls": int(execution_requirements["max_tool_calls_per_interaction"]),
        "max_output_tokens": int(execution_requirements["max_output_tokens"]),
        "episode_timeout_seconds": int(execution_requirements["turn_timeout_seconds"]),
    }
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise L2AgentRunError("each l2_stream_limit_overrides entry must be an object")
        if item.get("stream_id") not in {stream_id, "*"}:
            continue
        if item.get("baseline", baseline) not in {baseline, "*"}:
            continue
        carrier = item.get("carrier", "*")
        if carrier != "*" and carrier not in carriers:
            continue
        limits = item.get("limits")
        if not isinstance(limits, dict) or not limits:
            raise L2AgentRunError("l2_stream_limit_overrides.limits must be a non-empty object")
        normalized: dict[str, int] = {}
        for name, value in limits.items():
            if name not in allowed_fields:
                raise L2AgentRunError(f"unsupported stream limit override: {name}")
            if isinstance(value, bool):
                raise L2AgentRunError(f"stream limit override {name} must be an integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise L2AgentRunError(f"stream limit override {name} must be an integer") from exc
            if parsed < 1:
                raise L2AgentRunError(f"stream limit override {name} must be positive")
            if (
                budget_profile == "formal"
                and name in formal_caps
                and parsed > formal_caps[name]
            ):
                raise L2AgentRunError(
                    f"formal stream limit override for {stream_id}/{carrier} exceeds execution_requirements.{name}"
                )
            normalized[name] = parsed
        values.update(normalized)
        applied.append({
            "index": index,
            "stream_id": item.get("stream_id"),
            "baseline": item.get("baseline", baseline),
            "carrier": carrier,
            "limits": normalized,
            "budget_profile": budget_profile,
        })
    if values["episode_timeout_seconds"] <= values["terminal_timeout_seconds"]:
        raise L2AgentRunError("Episode time limit must exceed the terminal-command time limit")
    return L2AgentWorkLimits(**values)


def _effective_work_limits(
    configured: L2AgentWorkLimits,
    requirements: dict[str, Any],
    *,
    slow_provider: bool = False,
) -> L2AgentWorkLimits:
    episode_timeout = configured.episode_timeout_seconds if slow_provider else min(
        configured.episode_timeout_seconds,
        int(requirements["turn_timeout_seconds"]),
    )
    if episode_timeout < 2:
        raise L2AgentRunError("turn_timeout_seconds must allow a positive terminal timeout")
    return replace(
        configured,
        max_tool_calls=min(
            configured.max_tool_calls,
            int(requirements["max_tool_calls_per_interaction"]),
        ),
        terminal_timeout_seconds=min(
            configured.terminal_timeout_seconds,
            episode_timeout - 1,
        ),
        episode_timeout_seconds=episode_timeout,
        max_output_tokens=min(
            configured.max_output_tokens,
            int(requirements["max_output_tokens"]),
        ),
    )


def _runtime_profile_work_limits(
    stream_limits: L2AgentWorkLimits,
    profile: dict[str, Any],
) -> L2AgentWorkLimits:
    """Apply a Turn profile without allowing it to loosen Stream limits."""
    timeout = int(profile.get("turn_timeout_seconds", stream_limits.episode_timeout_seconds))
    tool_calls = int(profile.get("max_tool_calls", stream_limits.max_tool_calls))
    output_tokens = int(profile.get("max_output_tokens", stream_limits.max_output_tokens))
    if timeout < 2 or tool_calls < 1 or output_tokens < 1:
        raise L2AgentRunError("runtime profile limits must be positive")
    episode_timeout = min(stream_limits.episode_timeout_seconds, timeout)
    return replace(
        stream_limits,
        episode_timeout_seconds=episode_timeout,
        terminal_timeout_seconds=min(
            stream_limits.terminal_timeout_seconds,
            episode_timeout - 1,
        ),
        max_tool_calls=min(stream_limits.max_tool_calls, tool_calls),
        max_output_tokens=min(stream_limits.max_output_tokens, output_tokens),
    )


def _validate_taskstream_turn_identity(
    manifest: dict[str, Any],
    task: dict[str, Any],
    *,
    turn_index: int,
    turn_id: str,
) -> str:
    summaries = manifest.get("turns")
    if not isinstance(summaries, list) or len(summaries) != len(
        manifest.get("ordering", {}).get("turn_order", [])
    ):
        raise L2AgentRunError("TaskStream turns must match ordering.turn_order")
    summary = summaries[turn_index - 1]
    if not isinstance(summary, dict):
        raise L2AgentRunError(f"TaskStream turn summary {turn_index} is invalid")
    sequence_role = task.get("sequence_role")
    if sequence_role not in {"learning", "application", "probe", "replay", "noise"}:
        raise L2AgentRunError(f"TaskStream {turn_id} has an invalid sequence_role")
    identity = (turn_id, turn_index, sequence_role)
    task_identity = (task.get("turn_id"), task.get("turn_index"), task.get("sequence_role"))
    summary_identity = (
        summary.get("turn_id"),
        summary.get("turn_index"),
        summary.get("sequence_role"),
    )
    if task_identity != identity or summary_identity != identity:
        raise L2AgentRunError(
            f"TaskStream identity/sequence_role mismatch for {turn_id}"
        )
    return str(sequence_role)


def _replace_tree(seed: Path, target: Path) -> None:
    """Rebuild one already-isolated runtime Workspace from its frozen seed."""

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(seed, target, dirs_exist_ok=True)


def _native_result_error(native: dict[str, Any], result: Any) -> str | None:
    if not isinstance(result, dict):
        return "native verifier result must be an object"
    if result.get("schema_version") != "l2_native_verifier_result/1.0":
        return "unexpected native verifier result schema_version"
    if result.get("verifier_id") != native.get("verifier_id"):
        return "native verifier result identity mismatch"
    status = result.get("status")
    if status not in {"pass", "fail", "partial", "error"}:
        return "native verifier result has an invalid status"
    score = result.get("score")
    if status == "error":
        if score is not None or not isinstance(result.get("error"), str) or not result["error"]:
            return "native verifier error must have score=null and a non-empty error"
    elif (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not 0.0 <= float(score) <= 1.0
        or result.get("error") is not None
    ):
        return "scored native verifier result must have score in [0,1] and error=null"
    public_summary = result.get("public_summary")
    if not isinstance(public_summary, dict) or not isinstance(
        public_summary.get("observable_deficiencies"), list
    ):
        return "native verifier result must include public_summary"
    return None


def _project_bounded_feedback(
    task: dict[str, Any],
    verification: dict[str, Any],
) -> BoundedFeedback:
    evaluation = task.get("evaluation", {})
    checks = evaluation.get("checks", []) if isinstance(evaluation, dict) else []
    scores = verification.get("judge", {}).get("details", {}).get("check_scores", {})
    failed_dimensions: list[str] = []
    if isinstance(scores, dict):
        for check in checks:
            if not isinstance(check, dict):
                continue
            score = scores.get(str(check.get("check_id")))
            if score is not None and float(score) < 1.0:
                failed_dimensions.append(
                    str(check.get("dimension") or check.get("check_id") or "task_success")
                )
    native = verification.get("native_result", {})
    public_summary = native.get("public_summary", {}) if isinstance(native, dict) else {}
    deficiencies = public_summary.get("observable_deficiencies", []) if isinstance(public_summary, dict) else []
    if not failed_dimensions:
        failed_dimensions = ["task_success"]
    observable = next(
        (str(item) for item in deficiencies if isinstance(item, str) and item.strip()),
        "The observable result did not satisfy all declared public requirements.",
    )
    category = public_summary.get("result_category") if isinstance(public_summary, dict) else None
    if category not in {"fail", "partial"}:
        category = "fail"
    return BoundedFeedback(
        failed_dimensions=tuple(dict.fromkeys(failed_dimensions)),
        observable_deficiency=observable,
        public_result_category=str(category),
    )


def _user_simulator_output_error(result: Any) -> str | None:
    if not isinstance(result, dict):
        return "User Simulator output must be an object"
    allowed = {"schema_version", "next_user_message", "rationale_category"}
    if set(result) != allowed:
        return "User Simulator output has missing or unknown fields"
    if result.get("schema_version") != "l2_user_simulator_output/1.0":
        return "User Simulator output has an invalid schema_version"
    message = result.get("next_user_message")
    if not isinstance(message, str) or not message.strip():
        return "User Simulator next_user_message must be non-empty"
    if result.get("rationale_category") not in USER_SIMULATOR_RATIONALE_CATEGORIES:
        return "User Simulator rationale_category is invalid"
    return None


def _evaluation_error_summary(details: Any) -> str:
    if not isinstance(details, dict):
        return "evaluator failure"
    if isinstance(details.get("error"), str) and details["error"]:
        return str(details["error"])
    nested = details.get("details")
    if isinstance(nested, dict):
        if isinstance(nested.get("error"), str) and nested["error"]:
            return str(nested["error"])
        if isinstance(nested.get("type"), str) and nested["type"]:
            return str(nested["type"])
    if isinstance(details.get("stage"), str):
        return str(details["stage"])
    return "evaluator failure"


def _limit_reasons(raw: Any) -> list[str]:
    if not isinstance(raw, dict) or not raw.get("limit_exhausted"):
        return []
    values = raw.get("limit_reasons", [])
    return [str(item) for item in values] if isinstance(values, list) else ["unknown_work_limit"]


def _runtime_limit_failure(reason: str, detail: str | None = None) -> dict[str, Any]:
    details: dict[str, Any] = {"type": "work_limit", "reason": reason}
    if detail:
        details["detail"] = detail
    return {"category": "execution_limit", "score": 0.0, "details": details}


def _claim_retry_authorization(
    path: Path | None,
    *,
    failed_run_id: str,
    turn_id: str,
    checkpoint: Path,
    job_id: str,
    stream_id: str,
    baseline: str,
    condition: str,
) -> None:
    if path is None:
        raise L2AgentRunError("interrupted retry requires a consumed authorization ledger")
    auth_path = path.resolve()
    try:
        data = _load_json(auth_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise L2AgentRunError("invalid interrupted retry authorization ledger") from exc
    if data.get("status") != "consumed":
        raise L2AgentRunError("interrupted retry authorization ledger was not consumed")
    if str(data.get("failed_run_id", "")) != failed_run_id:
        raise L2AgentRunError("interrupted retry authorization run mismatch")
    if str(data.get("turn_id", "")) != turn_id:
        raise L2AgentRunError("interrupted retry authorization turn mismatch")
    if str(data.get("job", "")) != job_id:
        raise L2AgentRunError("interrupted retry authorization job mismatch")
    if str(data.get("stream_id", "")) != stream_id:
        raise L2AgentRunError("interrupted retry authorization stream mismatch")
    if str(data.get("baseline", "")) != baseline:
        raise L2AgentRunError("interrupted retry authorization baseline mismatch")
    if str(data.get("condition", "")) != condition:
        raise L2AgentRunError("interrupted retry authorization condition mismatch")
    if Path(str(data.get("checkpoint", ""))).resolve() != checkpoint.resolve():
        raise L2AgentRunError("interrupted retry authorization checkpoint mismatch")
    has_marker_binding = "interrupted_checkpoint" in data or "retry_interaction_index" in data
    retry_interaction_index = int(data.get("retry_interaction_index", 1))
    interrupted_checkpoint = Path(str(data.get("interrupted_checkpoint", checkpoint))).resolve()
    if has_marker_binding:
        if retry_interaction_index < 1:
            raise L2AgentRunError("interrupted retry authorization interaction mismatch")
        resume_metadata = _load_json(checkpoint / "checkpoint.json")
        interrupted_metadata = _load_json(interrupted_checkpoint / "checkpoint.json")
        if not (
            interrupted_metadata.get("phase") == "interact_started"
            and interrupted_metadata.get("run_id") == failed_run_id
            and interrupted_metadata.get("turn_id") == turn_id
            and int(interrupted_metadata.get("interaction_index", -1)) == retry_interaction_index
            and interrupted_metadata.get("stream_id") == stream_id
            and interrupted_metadata.get("baseline", baseline) == baseline
            and interrupted_metadata.get("condition", condition) == condition
        ):
            raise L2AgentRunError("interrupted retry marker mismatch")
    if has_marker_binding and retry_interaction_index > 1:
        evidence_path = checkpoint / "evidence.json"
        if not evidence_path.is_file():
            raise L2AgentRunError("later interrupted retry lacks saved reply evidence")
        evidence = _load_json(evidence_path)
        if not (
            checkpoint.parent == interrupted_checkpoint.parent
            and resume_metadata.get("phase") == "adjudicate"
            and resume_metadata.get("run_id") == failed_run_id
            and resume_metadata.get("turn_id") == turn_id
            and int(resume_metadata.get("interaction_index", -1)) == retry_interaction_index - 1
            and evidence.get("turn_id") == turn_id
            and int(evidence.get("interaction_index", -1)) == retry_interaction_index - 1
            and isinstance(evidence.get("request"), str)
            and isinstance(evidence.get("reply"), dict)
            and isinstance(evidence.get("history"), list)
            and isinstance(evidence.get("interactions"), list)
            and len(evidence["interactions"]) == retry_interaction_index - 2
            and isinstance(evidence.get("before_files"), dict)
        ):
            raise L2AgentRunError("later interrupted retry saved reply mismatch")
        target = interrupted_checkpoint.parent
        index = int(interrupted_metadata.get("next_turn_index", -1))
        received = target / f"r{index:04d}-{retry_interaction_index:02d}"
        pending = target / f"p{index:04d}-{retry_interaction_index:02d}"
        if received.exists() or pending.exists():
            raise L2AgentRunError("later interrupted retry target already has reply evidence")
    source_run_dir = Path(str(data.get("source_run_dir", ""))).resolve()
    if not source_run_dir.is_dir():
        raise L2AgentRunError("interrupted retry authorization source run directory is invalid")
    claim_path = source_run_dir / f"interrupted-retry-{failed_run_id}-{turn_id}.runner-claim.json"
    claim = {
        "status": "executing",
        "claimed_at": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "failed_run_id": failed_run_id,
        "turn_id": turn_id,
        "job": job_id,
        "stream_id": stream_id,
        "baseline": baseline,
        "condition": condition,
        "checkpoint": str(checkpoint.resolve()),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(claim_path, flags, 0o600)
    except FileExistsError as exc:
        raise L2AgentRunError("interrupted retry authorization was already claimed") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(claim, ensure_ascii=False, indent=2) + "\n")


def _public_trajectory(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "interaction_index": record["interaction_index"],
        "request": record["request"],
        "response": record["response"],
        "tool_calls": record["tool_calls"],
        "events": record["events"],
        "workspace_changes": record["workspace_changes"],
        "score": record["score"],
        "passed": record["passed"],
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _storage_gate_from_environment():
    from .storage_recovery import from_environment
    return from_environment()


def _docker_executable() -> str:
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    return str(candidate) if candidate.is_file() else "docker"
