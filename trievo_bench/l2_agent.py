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
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .baselines.base import EvolutionContext, FlowContext, InteractionContext, TurnContext
from .baselines.manifest import BaselineManifest, find_manifest, load_manifest, merge_mappings
from .baselines.process import ProcessBaselineRuntime
from .model import TokenBudget


class L2AgentRunError(RuntimeError):
    """Raised when the real-agent L2 runtime cannot safely continue."""


@dataclass(frozen=True)
class L2AgentWorkLimits:
    """Hard bounded-autonomy contract for one L2 Episode."""

    max_model_calls: int = 10
    max_tool_calls: int = 8
    terminal_timeout_seconds: int = 90
    episode_timeout_seconds: int = 480
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


class L2AgentStreamRunner:
    """Execute one L2 Stream as a sequence of BaselineRuntime Turns."""

    def __init__(self, options: L2AgentRunOptions) -> None:
        self.options = options
        self.package_dir = options.package_dir.resolve()
        self.stream_dir = self.package_dir / "streams" / options.stream_id
        self.budget = TokenBudget(options.max_total_tokens)
        self._runtime: ProcessBaselineRuntime | None = None
        self.work_limits = _work_limits(options.baseline_overrides)

    def run(self) -> dict[str, Any]:
        manifest = _load_json(self.stream_dir / "stream.json")
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

    def _create_runtime(self) -> ProcessBaselineRuntime:
        # L2 uses the same BaselineRuntime protocol as L1.  Resolve the
        # selected adapter from its registered manifest so Hermes, SkillOpt,
        # and Trace2Skill can be compared under one evaluator.  Previously
        # this method hard-coded Hermes, making the advertised three-agent L2
        # matrix impossible to run.
        manifest_path = find_manifest(self.options.baseline, self.options.repo_root)
        overrides = merge_mappings(
            self.options.baseline_overrides,
            {"evolution": {"policy": "turn_boundary", "timeout_seconds": self.work_limits.evolution_timeout_seconds}},
        )
        manifest = load_manifest(manifest_path).with_overrides(overrides)
        return ProcessBaselineRuntime(manifest, budget=self.budget, repo_root=self.options.repo_root)

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


def _under(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise L2AgentRunError(f"path escapes allowed root: {relative}")
    return candidate


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
        "episode_timeout_seconds": (30, 3600),
        "evolution_timeout_seconds": (10, 900),
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


def _docker_executable() -> str:
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    return str(candidate) if candidate.is_file() else "docker"
