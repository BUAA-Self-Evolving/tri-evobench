"""Generic Stream loop for the repository's executable L2 demo."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from l2_runtime.api import ModelResponse
from l2_runtime.user_simulator import BoundedRetryUserSimulator
from l2_runtime.verification import VerifyResult


class TextModel(Protocol):
    model: str

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        episode_id: str,
        attempt: int,
    ) -> ModelResponse: ...


_AGENT_SYSTEM = """You are the Task Agent in a benchmark.
Follow the current task request and return the requested deliverable in its declared format.
Do not invent access to files or tools that the request does not provide.
Do not include unrelated prose.
Persistent learner memory is advice from earlier Episodes, not hidden test data.
"""

_LEARNER_SYSTEM = """You maintain concise persistent memory for a coding Agent.
Given only the public task request, the Agent's latest response, and a pass/fail
result, extract reusable lessons for later tasks. Never claim to know hidden tests,
gold answers, or criterion weights. Write at most six short Markdown bullets.
"""


class StreamRunner:
    def __init__(
        self,
        *,
        stream_dir: Path,
        model: TextModel,
        output_root: Path,
        allow_unsafe_execution: bool,
    ) -> None:
        self.stream_dir = stream_dir.resolve()
        self.model = model
        self.output_root = output_root.resolve()
        self.allow_unsafe_execution = allow_unsafe_execution
        self.verifier = None
        self.user_simulator = BoundedRetryUserSimulator()

    def run(self) -> dict[str, object]:
        manifest = json.loads((self.stream_dir / "stream.json").read_text())
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        run_dir = self.output_root / manifest["stream_id"] / run_id
        workspace = run_dir / "workspace"
        run_dir.mkdir(parents=True, exist_ok=False)
        shutil.copytree(
            self.stream_dir / manifest["workspace"]["initial_snapshot"], workspace
        )
        state_dir = workspace / ".l2_state"
        state_dir.mkdir(parents=True)
        private_state_dir = run_dir / "benchmark_private" / "episodes"
        private_state_dir.mkdir(parents=True)
        memory_path = state_dir / "memory.md"
        memory_path.write_text("# Persistent learner memory\n")
        (state_dir / "protocol.json").write_text(
            json.dumps(
                {
                    "scope": "stream",
                    "stream_id": manifest["stream_id"],
                    "memory": ".l2_state/memory.md",
                    "episode_records": ".l2_state/episodes/<episode_id>/",
                    "model_response_schema": "model-response-0.1",
                    "episode_result_policy": "latest_interaction",
                },
                indent=2,
            )
            + "\n"
        )

        report: dict[str, object] = {
            "schema_version": "runtime-0.1",
            "run_id": run_id,
            "stream_id": manifest["stream_id"],
            "model": getattr(self.model, "model", "unknown"),
            "reasoning_effort": getattr(self.model, "reasoning_effort", None),
            "started_at": datetime.now(UTC).isoformat(),
            "run_dir": str(run_dir),
            "workspace": str(workspace),
            "episodes": [],
            "success": False,
        }
        _write_report(run_dir / "report.json", report)

        try:
            for episode_id, relative_path in zip(
                manifest["ordering"]["episode_order"], manifest["episodes"], strict=True
            ):
                episode_path = self.stream_dir / relative_path
                episode_index = len(report["episodes"])
                report["episodes"].append(
                    {
                        "episode_id": episode_id,
                        "status": "running",
                    }
                )
                _write_report(run_dir / "report.json", report)
                try:
                    episode_result = self._run_episode(
                        episode_id=episode_id,
                        episode_path=episode_path,
                        state_dir=state_dir,
                        private_state_dir=private_state_dir,
                        memory_path=memory_path,
                    )
                except BaseException as exc:
                    report["episodes"][episode_index] = {
                        "episode_id": episode_id,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    _write_report(run_dir / "report.json", report)
                    raise
                episode_result["status"] = "completed"
                report["episodes"][episode_index] = episode_result
                _write_report(run_dir / "report.json", report)
            report["success"] = all(
                episode["passed"] for episode in report["episodes"]
            )
        except BaseException as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            _write_report(run_dir / "report.json", report)
        return report

    def _run_episode(
        self,
        *,
        episode_id: str,
        episode_path: Path,
        state_dir: Path,
        private_state_dir: Path,
        memory_path: Path,
    ) -> dict[str, object]:
        episode = json.loads(episode_path.read_text())
        evaluation = json.loads(
            (episode_path.parent / episode["evaluation_ref"]).read_text()
        )
        if episode["episode_id"] != episode_id:
            raise ValueError(f"episode order mismatch for {episode_id}")
        workspace = state_dir.parent
        self._apply_workspace_transition(episode=episode, episode_dir=episode_path.parent, workspace=workspace)

        request = _materialize_agent_view(
            episode_path.parent, episode["agent_view"]
        )
        max_interactions = episode["interaction_policy"]["max_interactions"]
        if not isinstance(max_interactions, int) or isinstance(max_interactions, bool):
            raise ValueError(f"{episode_id} max_interactions must be an integer")
        if not 1 <= max_interactions <= 3:
            raise ValueError(f"{episode_id} max_interactions must be between 1 and 3")
        if evaluation["scoring"]["episode_score_policy"] != "latest_interaction":
            raise ValueError(f"{episode_id} must declare latest_interaction scoring")
        success_threshold = float(evaluation["scoring"]["success_threshold"])
        memory = memory_path.read_text()
        user_content = request
        if memory.strip() != "# Persistent learner memory":
            user_content += f"\n\n<PERSISTENT_MEMORY>\n{memory}\n</PERSISTENT_MEMORY>"
        messages = [
            {"role": "system", "content": _AGENT_SYSTEM},
            {"role": "user", "content": user_content},
        ]
        episode_state = state_dir / "episodes" / episode_id
        episode_state.mkdir(parents=True)
        private_episode_state = private_state_dir / episode_id
        private_episode_state.mkdir(parents=True)
        interactions: list[dict[str, object]] = []
        latest: tuple[VerifyResult, ModelResponse, int] | None = None
        verifier = self.verifier or _load_verifier(
            evaluation["verifier"]["entrypoint"],
            allow_unsafe_execution=self.allow_unsafe_execution,
        )

        for attempt in range(1, max_interactions + 1):
            response = self.model.generate(
                messages,
                purpose="agent",
                episode_id=episode_id,
                attempt=attempt,
            )
            response_path = episode_state / f"interaction-{attempt}-response.json"
            response_sha256 = _write_model_response(
                path=response_path,
                response=response,
                purpose="agent",
                stream_id=episode["stream_id"],
                episode_id=episode_id,
                interaction=attempt,
            )
            verify_result = _verify_with_workspace(
                verifier, response.text, episode_path.parent, workspace
            )
            record = {
                "attempt": attempt,
                "request": messages[-1]["content"],
                "response_file": str(response_path.relative_to(state_dir.parent)),
                "response_sha256": response_sha256,
                "model": response.model,
                "request_id": response.request_id,
                "usage": response.usage,
                "score": verify_result.score,
                "result": verify_result.category,
                "private_verifier_details": verify_result.details,
            }
            interactions.append(record)
            (private_episode_state / "interactions.json").write_text(
                json.dumps(interactions, ensure_ascii=False, indent=2) + "\n"
            )
            latest = (verify_result, response, attempt)
            if verify_result.score >= success_threshold and episode["interaction_policy"][
                "stop_on_success"
            ]:
                break
            if attempt >= max_interactions:
                break
            retry_request = self.user_simulator.next_request(
                result_category=verify_result.category,
                attempt=attempt,
            )
            messages.extend(
                [
                    {"role": "assistant", "content": response.text},
                    {"role": "user", "content": retry_request},
                ]
            )

        if latest is None:
            raise RuntimeError(f"episode {episode_id} produced no scored interactions")
        learner_response = self._update_memory(
            episode_id=episode_id,
            request=request,
            latest_response=latest[1].text,
            result_category=latest[0].category,
            attempt=latest[2],
            existing_memory=memory_path.read_text(),
        )
        learner_path = episode_state / "learner-update.json"
        _write_model_response(
            path=learner_path,
            response=learner_response,
            purpose="learner",
            stream_id=episode["stream_id"],
            episode_id=episode_id,
            interaction=None,
            source_interaction=latest[2],
        )
        with memory_path.open("a") as memory_file:
            memory_file.write(f"\n## After {episode_id}\n{learner_response.text.strip()}\n")

        return {
            "episode_id": episode_id,
            "source_instance_id": episode["source_provenance"]["instance_id"],
            "passed": latest[0].score >= success_threshold,
            "final_score": latest[0].score,
            "latest_interaction": latest[2],
            "interaction_count": len(interactions),
            "interactions_file": str(
                private_episode_state / "interactions.json"
            ),
            "learner_update_file": str(learner_path.relative_to(state_dir.parent)),
        }

    def _update_memory(
        self,
        *,
        episode_id: str,
        request: str,
        latest_response: str,
        result_category: str,
        attempt: int,
        existing_memory: str,
    ) -> ModelResponse:
        learner_input = (
            f"Existing memory:\n{existing_memory}\n\n"
            f"Public task request:\n{request}\n\n"
            f"Agent's latest response:\n{latest_response}\n\n"
            f"Verifier result category: {result_category}\n"
        )
        return self.model.generate(
            [
                {"role": "system", "content": _LEARNER_SYSTEM},
                {"role": "user", "content": learner_input},
            ],
            purpose="learner",
            episode_id=episode_id,
            attempt=attempt,
        )

    def _apply_workspace_transition(
        self,
        *,
        episode: dict[str, object],
        episode_dir: Path,
        workspace: Path,
    ) -> None:
        transition = episode.get("workspace_transition")
        if not isinstance(transition, dict):
            raise ValueError("workspace_transition must be an object")
        mode = transition.get("mode")
        if mode == "continue":
            patch_items = transition.get("workspace_patch", [])
            if patch_items:
                raise ValueError("continue transition must not declare workspace_patch")
            return
        if mode != "continue_with_patch":
            raise ValueError(
                "this minimal runner supports continue and continue_with_patch transitions"
            )
        patch_items = transition.get("workspace_patch")
        if not isinstance(patch_items, list):
            raise ValueError("continue_with_patch requires workspace_patch list")
        stream_dir = episode_dir.parents[1]
        for item in patch_items:
            if not isinstance(item, dict):
                raise ValueError("workspace_patch entries must be objects")
            if item.get("op") != "copy":
                raise ValueError("this minimal runner only supports copy workspace patches")
            source = item.get("from")
            target = item.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError("workspace_patch copy requires string from/to")
            source_path = (stream_dir / source).resolve()
            if stream_dir != source_path and stream_dir not in source_path.parents:
                raise ValueError(f"workspace_patch source escapes Stream package: {source}")
            if not source_path.exists():
                raise ValueError(f"workspace_patch source does not exist: {source}")
            workspace_relative = target.removeprefix("workspace/")
            target_path = (workspace / workspace_relative).resolve()
            if workspace != target_path and workspace not in target_path.parents:
                raise ValueError(f"workspace_patch target escapes Workspace: {target}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if source_path.is_dir():
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(source_path, target_path)
            else:
                shutil.copy2(source_path, target_path)


def _write_model_response(
    *,
    path: Path,
    response: ModelResponse,
    purpose: str,
    stream_id: str,
    episode_id: str,
    interaction: int | None,
    source_interaction: int | None = None,
) -> str:
    """Persist a provider-neutral model response envelope and return its digest."""

    content_sha256 = hashlib.sha256(response.text.encode()).hexdigest()
    artifact = {
        "schema_version": "model-response-0.1",
        "stream_id": stream_id,
        "episode_id": episode_id,
        "interaction": interaction,
        "source_interaction": source_interaction,
        "purpose": purpose,
        "role": "assistant",
        "model": response.model,
        "response_id": response.response_id,
        "request_id": response.request_id,
        "finish_reason": response.finish_reason,
        "api_retry_count": response.api_retry_count,
        "usage": response.usage,
        "content": response.text,
        "content_sha256": content_sha256,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
    return content_sha256


def _write_report(path: Path, report: dict[str, object]) -> None:
    """Write the run report after each lifecycle step for debuggable failures."""

    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _load_verifier(entrypoint: str, *, allow_unsafe_execution: bool):
    """Load a project-owned verifier declared as ``module:Class``."""

    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        raise ValueError("verifier entrypoint must use module:Class syntax")
    module_name, symbol = entrypoint.split(":", 1)
    if not module_name.startswith("l2_runtime."):
        raise ValueError("only project-owned l2_runtime verifier modules are allowed")
    verifier_class = getattr(importlib.import_module(module_name), symbol)
    return verifier_class(allow_unsafe_execution=allow_unsafe_execution)


def _verify_with_workspace(
    verifier, raw_model_output: str, episode_dir: Path, workspace: Path
) -> VerifyResult:
    """Call verifiers that either do or do not need the run Workspace."""

    signature = inspect.signature(verifier.verify)
    if "workspace" in signature.parameters:
        return verifier.verify(raw_model_output, episode_dir, workspace=workspace)
    return verifier.verify(raw_model_output, episode_dir)


def _materialize_agent_view(episode_dir: Path, agent_view: dict[str, object]) -> str:
    """Render public text sidecars and declare mounted Workspace assets.

    Policies and request files beside ``episode.json`` must be inlined because
    the text model cannot open them. Data and tool directories in the Stream
    Workspace stay mounted for the generated program and are declared without
    dumping their potentially large contents into the prompt.
    """

    request = agent_view.get("request")
    if not isinstance(request, str) or not request.strip():
        raise ValueError("agent_view.request must be non-empty text")
    sections = [request]

    request_file = agent_view.get("request_file")
    if request_file is not None:
        if not isinstance(request_file, str):
            raise ValueError("agent_view.request_file must be a relative path")
        request_path = _public_sidecar_path(episode_dir, request_file)
        if not request_path.is_file():
            raise ValueError(f"invalid agent-visible request file: {request_file}")
        sections.append(
            f"<AGENT_VISIBLE_REQUEST path={json.dumps(request_file)}>\n"
            f"{request_path.read_text()}\n"
            "</AGENT_VISIBLE_REQUEST>"
        )

    tools = agent_view.get("available_tools")
    if tools is not None:
        sections.append(
            "<AVAILABLE_TOOLS>\n"
            + json.dumps(tools, ensure_ascii=False, indent=2)
            + "\n</AVAILABLE_TOOLS>"
        )

    assets = agent_view.get("assets", [])
    if not isinstance(assets, list) or not all(isinstance(item, str) for item in assets):
        raise ValueError("agent_view.assets must be a list of relative paths")
    workspace_root = (episode_dir.parents[1] / "workspace").resolve()
    for relative in assets:
        if relative == "assets" or relative.startswith("assets/"):
            sidecar_path = _public_sidecar_path(episode_dir, relative)
            if not sidecar_path.is_file():
                raise ValueError(f"invalid agent-visible asset: {relative}")
            sections.append(
                f"<AGENT_VISIBLE_ASSET path={json.dumps(relative)}>\n"
                f"{sidecar_path.read_text()}\n"
                "</AGENT_VISIBLE_ASSET>"
            )
            continue

        workspace_relative = relative.removeprefix("workspace/")
        mounted_path = (workspace_root / workspace_relative).resolve()
        if workspace_root != mounted_path and workspace_root not in mounted_path.parents:
            raise ValueError(f"invalid agent-visible asset: {relative}")
        if not mounted_path.exists():
            raise ValueError(f"invalid agent-visible asset: {relative}")
        asset_type = "directory" if mounted_path.is_dir() else "file"
        sections.append(
            f"<WORKSPACE_ASSET path={json.dumps(relative)} type={json.dumps(asset_type)} />"
        )
    return "\n\n".join(sections)


def _public_sidecar_path(episode_dir: Path, relative: str) -> Path:
    public_root = (episode_dir / "assets").resolve()
    path = (episode_dir / relative).resolve()
    if public_root not in path.parents:
        raise ValueError(f"agent-visible sidecar must be under assets/: {relative}")
    return path
