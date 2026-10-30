from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..model import TokenBudget
from .base import (
    RUNTIME_PROTOCOL_VERSION,
    BaselineInteractionError,
    BaselineReply,
    BaselineRuntime,
    BaselineSnapshot,
    EvolutionContext,
    EvolutionResult,
    FlowContext,
    InteractionContext,
    TurnContext,
    UsageDetails,
)
from .manifest import BaselineManifest
from ..docker_proxy import DockerProxyBridge, is_docker_command
from ..docker_volume import DockerVolumeMirror


class BaselineProtocolError(RuntimeError):
    pass


_STREAM_CLOSED = object()


class NdjsonProcess:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stderr_path: Path,
        timeout_seconds: float,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        # A reset-condition Stream starts a fresh runtime for every Turn but
        # intentionally keeps one run directory. Append so later runtimes (or
        # an exact checkpoint resume) cannot erase earlier diagnostic logs.
        self._stderr_stream = stderr_path.open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_stream,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception:
            self._stderr_stream.close()
            raise
        self._messages: queue.Queue[Any] = queue.Queue()
        # Adapters may emit responses out of order when an asynchronous
        # evolution worker finishes just after the foreground request. Keep
        # unmatched responses until the corresponding request is awaited.
        self._pending: dict[int, dict[str, Any]] = {}
        self._request_id = 0
        self._reader = threading.Thread(target=self._read_stdout, name="baseline-protocol-reader", daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            self._messages.put(_STREAM_CLOSED)
            return
        try:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    message = {"protocol_error": f"adapter emitted non-JSON output: {stripped[:500]}"}
                self._messages.put(message)
        finally:
            self._messages.put(_STREAM_CLOSED)

    def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise BaselineProtocolError(f"baseline adapter exited with code {self.process.returncode}")
        self._request_id += 1
        request_id = self._request_id
        payload = {"id": request_id, "method": method, "params": params}
        stream = self.process.stdin
        if stream is None:
            raise BaselineProtocolError("baseline adapter stdin is unavailable")
        try:
            # Protocol input may contain lone UTF-16 surrogates from benchmark
            # fixtures.  Escaping non-ASCII keeps the NDJSON transport valid
            # on Windows and lets the adapter decode the payload safely.
            stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
            stream.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BaselineProtocolError("baseline adapter closed its input") from exc

        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BaselineProtocolError(f"baseline adapter timed out during {method}")
            message = self._pending.pop(request_id, None)
            if message is None:
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise BaselineProtocolError(f"baseline adapter timed out during {method}") from exc
            if message is _STREAM_CLOSED:
                code = self.process.poll()
                raise BaselineProtocolError(f"baseline adapter closed stdout during {method} (exit={code})")
            if not isinstance(message, dict):
                raise BaselineProtocolError("baseline adapter emitted a non-object message")
            if message.get("protocol_error"):
                raise BaselineProtocolError(str(message["protocol_error"]))
            if message.get("event"):
                continue
            if message.get("id") != request_id:
                other_id = message.get("id")
                if isinstance(other_id, int):
                    self._pending[other_id] = message
                    continue
                raise BaselineProtocolError(f"baseline adapter response id mismatch: expected {request_id}, got {other_id}")
            if message.get("error") is not None:
                error = message["error"]
                if isinstance(error, dict):
                    error = error.get("message", error)
                raise BaselineProtocolError(f"baseline adapter {method} failed: {error}")
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise BaselineProtocolError(f"baseline adapter {method} result must be an object")
            return result

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr_stream.close()


@dataclass
class _StateCapture:
    files: dict[str, bytes]
    snapshot: BaselineSnapshot


class ProcessBaselineRuntime(BaselineRuntime):
    """Out-of-process runtime used by Hermes and future agent frameworks."""

    def __init__(
        self,
        manifest: BaselineManifest,
        *,
        budget: TokenBudget,
        repo_root: Path,
        resume_state_archive: Path | None = None,
    ) -> None:
        self.manifest = manifest
        self.baseline_id = manifest.baseline_id
        self.budget = budget
        self.repo_root = repo_root.resolve()
        self._transport: NdjsonProcess | None = None
        self._state_dir: Path | None = None
        self._flow: FlowContext | None = None
        self._current_turn: str | None = None
        self._current_turn_metadata: dict[str, Any] = {}
        self._turn_evolved = False
        self._adapter_metadata: dict[str, Any] = {}
        self._semantic_state: dict[str, Any] = {}
        self._stable_state: _StateCapture | None = None
        self._cleanup_command: list[str] | None = None
        self._template_values: dict[str, str] | None = None
        self._resume_state_archive = resume_state_archive.resolve() if resume_state_archive else None
        self._docker_proxy_bridge: DockerProxyBridge | None = None
        self._docker_proxy_bridge_used = False
        self._state_volume: DockerVolumeMirror | None = None
        self._docker_volume_transport_used = False

    def set_resume_state_archive(self, archive: Path) -> None:
        """Select a saved learning-state archive before the flow starts."""
        if self._transport is not None:
            raise RuntimeError("cannot change resume state after baseline flow start")
        self._resume_state_archive = archive.resolve()

    def start_flow(self, context: FlowContext) -> None:
        if self._transport is not None:
            raise RuntimeError("baseline flow already started")
        self._verify_source_revision()
        self._flow = context
        self._state_dir = Path(tempfile.mkdtemp(prefix=f"trievo-{_safe_name(self.baseline_id)}-state-"))

        try:
            self._seed_state()
            adapter = self.manifest.adapter
            if context.workspace_mount is not None and is_docker_command(adapter["command"]):
                self._state_volume = DockerVolumeMirror.create(
                    self._state_dir,
                    role=f"{self.baseline_id}-state",
                )
                self._docker_volume_transport_used = True
            variables = self._template_variables(context)
            self._template_values = variables
            command = [_render(item, variables) for item in adapter["command"]]
            terminal_volume = context.metadata.get("task_terminal_volume")
            if terminal_volume is not None:
                from trievo_bench.task_terminal import PUBLIC_DIRECTORY
                if not is_docker_command(command) or not isinstance(terminal_volume, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", terminal_volume):
                    raise BaselineProtocolError("task terminal requires a valid Docker volume")
                position = command.index("run") + 1
                command[position:position] = ["--mount", f"type=volume,src={terminal_volume},dst={PUBLIC_DIRECTORY},volume-nocopy"]
            if context.workspace_mount is not None:
                command = _rewrite_named_volume_mount(
                    command, context.workspace_mount, "/workspace", no_copy=terminal_volume is not None
                )
            if self._state_volume is not None:
                command = _rewrite_named_volume_mount(
                    command, self._state_volume.volume_name, "/state", no_copy=terminal_volume is not None
                )
            command[0] = _resolve_executable(command[0])
            cleanup = adapter.get("cleanup_command")
            self._cleanup_command = (
                [_render(item, variables) for item in cleanup]
                if isinstance(cleanup, list) and all(isinstance(item, str) for item in cleanup)
                else None
            )
            if self._cleanup_command:
                self._cleanup_command[0] = _resolve_executable(self._cleanup_command[0])
            cwd_value = _render(str(adapter.get("cwd", "{repo_root}")), variables)
            cwd = Path(cwd_value)
            if not cwd.is_absolute():
                cwd = self.repo_root / cwd
            env = self._build_environment(variables)
            if is_docker_command(command):
                self._docker_proxy_bridge = DockerProxyBridge.from_environment()
                self._docker_proxy_bridge_used = self._docker_proxy_bridge.active
                env.update(self._docker_proxy_bridge.environment)
            timeout = float(adapter.get("timeout_seconds", 900))
        except BaseException:
            try:
                self.end_flow()
            except BaseException:
                pass
            raise
        try:
            self._transport = NdjsonProcess(
                command,
                cwd=cwd,
                env=env,
                stderr_path=context.output_dir / "baseline.stderr.log",
                timeout_seconds=timeout,
            )
            initialized = self._request(
                "initialize",
                {
                    "protocol_version": RUNTIME_PROTOCOL_VERSION,
                    "baseline_id": self.baseline_id,
                    "options": self.manifest.options,
                    "evolution": self.manifest.evolution,
                },
            )
            protocol_version = str(initialized.get("protocol_version", ""))
            if protocol_version != RUNTIME_PROTOCOL_VERSION:
                raise BaselineProtocolError(
                    f"adapter protocol mismatch: expected {RUNTIME_PROTOCOL_VERSION}, got {protocol_version!r}"
                )
            workspace_path = _render(str(self.manifest.workspace.get("adapter_path", "{workspace}")), variables)
            state_path = _render(str(self.manifest.state.get("adapter_path", "{state_dir}")), variables)
            started = self._request(
                "start_flow",
                {
                    "flow_id": context.flow_id,
                    "workspace": workspace_path,
                    "state_dir": state_path,
                    "metadata": context.metadata,
                },
            )
            self._adapter_metadata = {**initialized, **started}
            self._stable_state = self._capture_state(include_bytes=True)
        except Exception:
            try:
                self.end_flow()
            except BaseException:
                pass
            raise

    def start_turn(self, context: TurnContext) -> None:
        if self._transport is None:
            raise RuntimeError("baseline flow has not started")
        if self._current_turn is not None and not self._turn_evolved:
            raise RuntimeError(f"turn {self._current_turn} has not completed evolution")
        self._ensure_stable_state("between evolution and the next Turn")
        prior_state = self._stable_state
        started = self._request(
            "start_turn",
            {
                "turn_id": context.turn_id,
                "request": context.request,
                "max_interactions": context.max_interactions,
                "metadata": context.metadata,
                # Semantic state is public baseline state, not evaluator
                # feedback.  L1 adapters use it to restore facts after their
                # required fresh per-turn session is created.
                "semantic_state": self._semantic_state,
            },
        )
        declared_terminal = (context.metadata.get("task_sandbox") or {}).get("terminal_transport")
        if declared_terminal is not None and started.get("task_terminal_transport") != declared_terminal:
            raise BaselineProtocolError("baseline did not acknowledge the declared task terminal transport")
        self._current_turn = context.turn_id
        self._current_turn_metadata = dict(context.metadata)
        self._turn_evolved = False
        started_state = self._capture_state(include_bytes=True)
        if prior_state is not None and bool(self.manifest.state.get("enforce_turn_freeze", False)):
            changed = _changed_paths(prior_state.snapshot, started_state.snapshot)
            if changed:
                self._restore_state(prior_state)
                raise BaselineProtocolError(
                    "baseline modified persistent learning state during start_turn: " + ", ".join(changed)
                )
        self._stable_state = started_state

    def interact(self, context: InteractionContext) -> BaselineReply:
        if context.turn_id != self._current_turn:
            raise RuntimeError(f"interaction belongs to inactive turn: {context.turn_id}")
        self._ensure_stable_state("before interact", interaction_error=True)
        before = (
            self._stable_state
            if self._stable_state is not None
            and bool(self.manifest.state.get("enforce_turn_freeze", False))
            else self._capture_state(include_bytes=True)
        )
        interaction_timeout = _positive_seconds(
            self._current_turn_metadata.get("interaction_timeout_seconds")
        )
        if self.baseline_id == "deepseek-harness" and interaction_timeout is not None:
            # DSH stops native work at the episode boundary, then closes its
            # runtime and returns saved events. The transport must outlive
            # that boundary; this grace does not extend native work or retry.
            interaction_timeout += 60.0
        elif ((self.baseline_id == "opencode" or (
                  self.baseline_id == "prime-agent" and self.manifest.options.get("collect_episode_limit_reply")))
              and interaction_timeout is not None
              and (self._current_turn_metadata.get("task_sandbox") or {}).get("terminal_transport")):
            # V5 native work stops at its episode deadline; transport outlives
            # cancellation and collection of the original reply and usage.
            interaction_timeout += 60.0
        result = self._request(
            "interact",
            {
                "turn_id": context.turn_id,
                "interaction_index": context.interaction_index,
                "request": context.request,
                "history": context.history,
                "sandbox": context.sandbox,
                "remaining_token_budget": context.remaining_token_budget,
            },
            timeout=interaction_timeout,
        )
        usage = UsageDetails.from_mapping(result.get("usage"))
        self._record_usage(usage)
        reply = BaselineReply(
            text=str(result.get("text", result.get("response", ""))),
            actions=_dict_list(result.get("actions")),
            state_update=dict(result.get("state_update", {})) if isinstance(result.get("state_update"), dict) else {},
            tool_calls=_dict_list(result.get("tool_calls")),
            events=_dict_list(result.get("events")),
            usage=usage,
            actions_are_executed=bool(result.get("actions_are_executed", True)),
            tool_calls_are_executed=bool(result.get("tool_calls_are_executed", True)),
            raw=result.get("raw"),
        )
        if reply.state_update:
            self._semantic_state = _merge_state(self._semantic_state, reply.state_update)
        after = self._capture_state(include_bytes=False)
        changed = _changed_paths(before.snapshot, after.snapshot)
        if changed and bool(self.manifest.state.get("enforce_turn_freeze", False)):
            self._restore_state(before)
            raise BaselineInteractionError(
                "baseline modified persistent learning state during interact: " + ", ".join(changed),
                reply=reply,
            )
        self._stable_state = self._with_current_semantic_state(before)
        return reply

    def evolve(self, context: EvolutionContext) -> EvolutionResult:
        if context.turn_id != self._current_turn:
            raise RuntimeError(f"evolution belongs to inactive turn: {context.turn_id}")
        if self._turn_evolved:
            raise RuntimeError(f"turn {context.turn_id} was already evolved")
        self._ensure_stable_state("before evolve")
        before_capture = (
            self._stable_state
            if self._stable_state is not None
            and bool(self.manifest.state.get("enforce_turn_freeze", False))
            else self._capture_state(include_bytes=True)
        )
        before_capture = self._with_current_semantic_state(before_capture)
        before = before_capture.snapshot
        timeout = float(self.manifest.evolution.get("timeout_seconds", self.manifest.adapter.get("timeout_seconds", 900)))
        try:
            result = self._request(
                "evolve",
                {
                    "turn_id": context.turn_id,
                    "trajectory": context.trajectory,
                    "feedback": context.feedback,
                    "update_enabled": context.update_enabled,
                    "skip_reason": context.skip_reason,
                },
                timeout=timeout,
            )
        except Exception:
            # end_flow enforces the stable state and can erase evidence of a
            # failed learning pass. Export declared bytes privately first.
            self._preserve_failed_evolution(context.turn_id)
            raise
        usage = UsageDetails.from_mapping(result.get("usage"))
        self._record_usage(usage)
        if not bool(result.get("completed", True)):
            self._preserve_failed_evolution(context.turn_id)
            self._restore_state(before_capture)
            self._stable_state = before_capture
            if not bool(result.get("recoverable_failure", False)):
                raise BaselineProtocolError(
                    f"baseline evolution did not complete for turn {context.turn_id}"
                )
            # A bounded evolution pass can fail without making the baseline
            # transport unusable. Roll back its learning state, cross the
            # lifecycle barrier, and let the evaluator mark this Turn failed.
            self._turn_evolved = True
            return EvolutionResult(
                completed=False,
                events=_dict_list(result.get("events")),
                usage=usage,
                state_before=before,
                state_after=before,
                changed_files=[],
            )
        after_capture = self._capture_state(include_bytes=True)
        after = after_capture.snapshot
        self._turn_evolved = True
        self._stable_state = after_capture
        return EvolutionResult(
            completed=bool(result.get("completed", True)),
            events=_dict_list(result.get("events")),
            usage=usage,
            state_before=before,
            state_after=after,
            changed_files=_changed_paths(before, after),
        )

    def _preserve_failed_evolution(self, turn_id: str) -> None:
        if self._flow is None or self._state_dir is None:
            return
        capture = self._capture_state(include_bytes=True)
        output = self._flow.output_dir / "failed_evolution" / uuid.uuid4().hex
        output.mkdir(parents=True, exist_ok=False)
        self._export_state(output, capture.snapshot)
        (output / "failure.json").write_text(json.dumps({
            "turn_id": turn_id, "state_digest": capture.snapshot.digest,
            "recoverable_checkpoint": False,
        }, indent=2) + "\n", encoding="utf-8")

    def snapshot_state(self) -> BaselineSnapshot:
        return self._capture_state(include_bytes=False).snapshot

    def save_checkpoint_state(self, archive: Path) -> None:
        """Persist exact learning state in a short, portable zip archive."""
        self._ensure_stable_state("before checkpoint")
        capture = (
            self._stable_state
            if self._stable_state is not None
            and bool(self.manifest.state.get("enforce_turn_freeze", False))
            else self._capture_state(include_bytes=True)
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative, content in capture.files.items():
                bundle.writestr(relative, content)
            bundle.writestr(
                ".trievo_semantic_state.json",
                json.dumps(self._semantic_state, ensure_ascii=False),
            )

    def end_flow(self) -> None:
        transport = self._transport
        final_snapshot = BaselineSnapshot()
        snapshot_ready = self._state_dir is None
        cleanup_errors: list[BaseException] = []
        try:
            try:
                self._ensure_stable_state("before end_flow")
            except BaseException as exc:
                cleanup_errors.append(exc)
            if transport is not None and transport.process.poll() is None:
                try:
                    transport.request("end_flow", {}, timeout=30)
                except RuntimeError:
                    pass
        finally:
            if self._state_dir is not None:
                try:
                    # Materialize the latest daemon-owned state before teardown.
                    # A metadata-only snapshot leaves the host mirror stale when
                    # interact failed before its normal state capture completed.
                    final_snapshot = self._capture_state(include_bytes=True).snapshot
                    snapshot_ready = True
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if transport is not None:
                try:
                    transport.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if self._cleanup_command is not None:
                try:
                    subprocess.run(
                        self._cleanup_command,
                        cwd=str(self.repo_root),
                        env=self._build_environment(self._template_values or {}),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(exc)
            if self._docker_proxy_bridge is not None:
                try:
                    self._docker_proxy_bridge.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                self._docker_proxy_bridge = None
            self._transport = None
            if (
                snapshot_ready
                and self._flow is not None
                and self._state_dir is not None
            ):
                try:
                    self._export_state(self._flow.output_dir, final_snapshot)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if self._state_volume is not None:
                try:
                    self._state_volume.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                self._state_volume = None
            if self._state_dir is not None:
                shutil.rmtree(self._state_dir, ignore_errors=True)
            self._state_dir = None
            self._current_turn = None
            self._current_turn_metadata = {}
            self._stable_state = None
            self._cleanup_command = None
            self._template_values = None
            self._semantic_state = {}
        if cleanup_errors:
            raise cleanup_errors[0]

    def metadata(self) -> dict[str, Any]:
        source = self.manifest.source
        return {
            **super().metadata(),
            "adapter": "process",
            "learner": "FrameworkLearner",
            "manifest": str(self.manifest.path),
            "manifest_digest": self.manifest.digest,
            "image": self.manifest.options.get("image"),
            "source_revision": source.get("revision"),
            "workspace_mode": self.manifest.workspace.get("mode", "host_path"),
            "evolution_policy": self.manifest.evolution.get("policy", "turn_boundary"),
            "capabilities": list(self.manifest.raw.get("capabilities", [])),
            "adapter_metadata": self._adapter_metadata,
            "docker_proxy_bridge": self._docker_proxy_bridge_used,
            "docker_volume_transport": self._docker_volume_transport_used,
        }

    def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if self._transport is None:
            raise RuntimeError("baseline adapter is not running")
        return self._transport.request(method, params, timeout=timeout)

    def _record_usage(self, usage: UsageDetails) -> None:
        if usage.total_tokens or usage.api_calls:
            self.budget.record(
                usage.total_tokens,
                calls=usage.api_calls or (1 if usage.total_tokens else 0),
            )

    def _template_variables(self, context: FlowContext) -> dict[str, str]:
        if self._state_dir is None:
            raise RuntimeError("state directory is not initialized")
        variables = {
            "repo_root": str(self.repo_root),
            "workspace": str(context.workspace.resolve()),
            "workspace_mount": context.workspace_mount or str(context.workspace.resolve()),
            "state_dir": str(self._state_dir.resolve()),
            "state_mount": (
                self._state_volume.volume_name
                if self._state_volume is not None
                else str(self._state_dir.resolve())
            ),
            "flow_id": context.flow_id,
            "flow_name": _safe_name(context.flow_id),
            "instance_id": uuid.uuid4().hex[:12],
            "uid": str(os.getuid()) if hasattr(os, "getuid") else "1000",
            "gid": str(os.getgid()) if hasattr(os, "getgid") else "1000",
        }
        for key, value in self.manifest.options.items():
            name = str(key)
            if name not in variables and isinstance(value, (str, int, float, bool)):
                variables[name] = str(value)
        return variables

    def _build_environment(self, variables: dict[str, str]) -> dict[str, str]:
        environment = self.manifest.environment
        inherit = {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_API_VERSION",
            "XDG_RUNTIME_DIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            *[str(item) for item in environment.get("inherit", [])],
        }
        env = {key: value for key, value in os.environ.items() if key in inherit}
        if os.name == "nt":
            # Python's Windows socket extension requires SYSTEMROOT. Keep the
            # small set of OS runtime variables even when the manifest uses a
            # strict application-environment allowlist.
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "COMSPEC"):
                value = os.environ.get(key)
                if value:
                    env[key] = value
        env["PYTHONUNBUFFERED"] = "1"
        for key, value in environment.get("set", {}).items():
            env[str(key)] = _render(str(value), variables)
        return env

    def _seed_state(self) -> None:
        if self._state_dir is None:
            return
        (self._state_dir / "home").mkdir(parents=True, exist_ok=True)
        state = self.manifest.state
        seed_from = state.get("seed_from")
        if seed_from:
            variables = {"repo_root": str(self.repo_root)}
            for key, value in self.manifest.options.items():
                name = str(key)
                if name not in variables and isinstance(value, (str, int, float, bool)):
                    variables[name] = str(value)
            seed_root = Path(_render(str(seed_from), variables)).expanduser().resolve()
            if not seed_root.is_dir():
                raise BaselineProtocolError(f"baseline state seed directory not found: {seed_root}")
            for relative in state.get("seed_files", ["config.yaml"]):
                safe_relative = _safe_relative_state_path(str(relative))
                source = seed_root / safe_relative
                if not source.exists():
                    continue
                target = self._state_dir / safe_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
        if self._resume_state_archive is not None:
            if not self._resume_state_archive.is_file():
                raise BaselineProtocolError(
                    f"baseline resume-state archive not found: {self._resume_state_archive}"
                )
            with zipfile.ZipFile(self._resume_state_archive) as bundle:
                for name in bundle.namelist():
                    if name == ".trievo_semantic_state.json":
                        raw = json.loads(bundle.read(name).decode("utf-8"))
                        self._semantic_state = raw if isinstance(raw, dict) else {}
                        continue
                    relative = _safe_relative_state_path(name)
                    target = self._state_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.read(name))

    def _capture_state(self, *, include_bytes: bool) -> _StateCapture:
        root = self._state_dir
        if root is None or not root.exists():
            return _StateCapture({}, BaselineSnapshot())
        patterns = [
            *self.manifest.state.get("paths", []),
            *self.manifest.state.get("guard_paths", []),
        ]
        ignored = [str(pattern) for pattern in self.manifest.state.get("ignore_paths", [])]
        if self._state_volume is not None:
            volume_capture = self._state_volume.capture_selected(
                [str(pattern) for pattern in patterns],
                ignored,
                include_bytes=include_bytes,
            )
            captured = volume_capture.files
            metadata = volume_capture.metadata
            if include_bytes:
                self._materialize_selected_host_state(captured, patterns)
        else:
            captured = {}
            metadata = []
            for relative, path in self._iter_state_files(patterns):
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                metadata.append({"path": relative, "size": len(content), "sha256": digest})
                if include_bytes:
                    captured[relative] = content
        metadata.sort(key=lambda item: item["path"])
        hasher = hashlib.sha256()
        for item in metadata:
            hasher.update(str(item["path"]).encode("utf-8"))
            hasher.update(str(item["sha256"]).encode("ascii"))
        digest = hasher.hexdigest()
        state = {"digest": digest, "files": [item["path"] for item in metadata], **self._semantic_state}
        return _StateCapture(captured, BaselineSnapshot(state=state, digest=digest, files=metadata))

    def _materialize_selected_host_state(
        self,
        files: dict[str, bytes],
        patterns: list[str],
    ) -> None:
        if self._state_dir is None:
            return
        for _, path in self._iter_state_files(patterns):
            path.unlink(missing_ok=True)
        for relative, content in files.items():
            target = self._state_dir / _safe_relative_state_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def _with_current_semantic_state(self, capture: _StateCapture) -> _StateCapture:
        snapshot = capture.snapshot
        state = {
            "digest": snapshot.digest,
            "files": [item["path"] for item in snapshot.files],
            **self._semantic_state,
        }
        return _StateCapture(
            capture.files,
            BaselineSnapshot(
                state=state,
                digest=snapshot.digest,
                files=list(snapshot.files),
            ),
        )

    def _iter_state_files(self, patterns: list[str] | None = None) -> list[tuple[str, Path]]:
        root = self._state_dir
        if root is None:
            return []
        found: dict[str, Path] = {}
        root_resolved = root.resolve()
        selected_patterns = patterns if patterns is not None else list(self.manifest.state.get("paths", []))
        for pattern in selected_patterns:
            for path in root.glob(str(pattern)):
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                if resolved != root_resolved and root_resolved not in resolved.parents:
                    continue
                relative = resolved.relative_to(root_resolved).as_posix()
                found[relative] = resolved
        ignored = [str(pattern) for pattern in self.manifest.state.get("ignore_paths", [])]
        return sorted(
            (relative, path)
            for relative, path in found.items()
            if not any(PurePosixPath(relative).match(pattern) for pattern in ignored)
        )

    def _restore_state(self, capture: _StateCapture) -> None:
        if self._state_dir is None:
            return
        patterns = [
            *self.manifest.state.get("paths", []),
            *self.manifest.state.get("guard_paths", []),
        ]
        for _, path in self._iter_state_files(patterns):
            path.unlink(missing_ok=True)
        for relative, content in capture.files.items():
            target = self._state_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if self._state_volume is not None:
            self._state_volume.sync_selected_from_host(
                [str(pattern) for pattern in patterns],
                [str(pattern) for pattern in self.manifest.state.get("ignore_paths", [])],
            )

    def _ensure_stable_state(self, phase: str, *, interaction_error: bool = False) -> None:
        if not bool(self.manifest.state.get("enforce_turn_freeze", False)):
            return
        expected = self._stable_state
        if expected is None:
            return
        current = self._capture_state(include_bytes=False)
        changed = _changed_paths(expected.snapshot, current.snapshot)
        if not changed:
            return
        self._restore_state(expected)
        message = f"baseline persistent state changed asynchronously {phase}: " + ", ".join(changed)
        if interaction_error:
            raise BaselineInteractionError(message)
        raise BaselineProtocolError(message)

    def _export_state(self, output_dir: Path, snapshot: BaselineSnapshot) -> None:
        artifact_root = output_dir / "baseline_state"
        state_files = [(relative, path) for relative, path in self._iter_state_files() if path.is_file()]
        # Self-evolving baselines can create deep history/evolver trees. On
        # Windows the nested run path may exceed the filesystem limit during
        # export even when the state itself is valid, so preserve the same
        # files in a zip with POSIX archive names.
        # The output path itself is already deeply nested in Windows runs;
        # checking individual descendants is too late because mkdir can fail
        # on an intermediate directory first. Always use the short zip file on
        # Windows and retain the directory form on POSIX hosts.
        # The zip is the portable, exact checkpoint/export form on every host.
        # Keeping it cross-platform also means a Linux-produced run can be moved
        # to a Windows reviewer without rebuilding a deep directory tree.
        archive = output_dir / "baseline_state.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative, path in state_files:
                try:
                    if path.is_file():
                        bundle.write(path, arcname=relative)
                except FileNotFoundError:
                    continue
        use_zip = os.name == "nt"
        if use_zip:
            # Keep the traditional directory form for short paths so existing
            # consumers remain compatible. Deep entries are already preserved
            # losslessly in the zip above; skip only entries rejected by the
            # Windows path-length limit.
            try:
                artifact_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                artifact_root = None
            if artifact_root is not None:
                for relative, path in state_files:
                    if not path.is_file():
                        continue
                    target = artifact_root / relative
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
                    except (FileNotFoundError, OSError):
                        continue
        else:
            artifact_root.mkdir(parents=True, exist_ok=True)
            for relative, path in state_files:
                if not path.is_file():
                    continue
                target = artifact_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(path, target)
                except FileNotFoundError:
                    continue
        # The run directory can exceed Win32 MAX_PATH even when report and
        # trace files were created successfully.  State is already preserved
        # in baseline_state.zip in that case; do not turn a completed run into
        # a teardown failure merely because the optional JSON sidecar cannot
        # be created at the deeply nested path.
        try:
            (output_dir / "baseline_state_snapshot.json").write_text(
                json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (FileNotFoundError, OSError):
            pass

    def _verify_source_revision(self) -> None:
        source = self.manifest.source
        expected = str(source.get("revision", "")).strip()
        source_path = source.get("path")
        if not expected or not source_path:
            return
        path = Path(str(source_path))
        if not path.is_absolute():
            path = self.repo_root / path
        if not path.is_dir():
            raise BaselineProtocolError(f"baseline source directory not found: {path}")
        expected_archive = str(source.get("archive_sha256", "")).strip().lower()
        if not (path / ".git").exists() and expected_archive:
            actual_archive = _archive_tree_sha256(path)
            if actual_archive != expected_archive:
                raise BaselineProtocolError(
                    f"baseline archive digest mismatch for {self.baseline_id}: "
                    f"expected {expected_archive}, found {actual_archive}"
                )
            return
        try:
            actual = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=30,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise BaselineProtocolError(f"cannot verify baseline revision at {path}") from exc
        allow_mismatch = bool(self.manifest.options.get("allow_revision_mismatch", False))
        if actual != expected and not allow_mismatch:
            raise BaselineProtocolError(
                f"baseline revision mismatch for {self.baseline_id}: expected {expected}, found {actual}"
            )
        try:
            dirty = subprocess.run(
                ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=30,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise BaselineProtocolError(f"cannot inspect baseline source tree at {path}") from exc
        if dirty and not allow_mismatch:
            raise BaselineProtocolError(
                f"baseline source tree is dirty for {self.baseline_id}; commit or remove local changes before evaluation"
            )


def _archive_tree_sha256(root: Path) -> str:
    """Verify a delivered source archive when its Git metadata is absent.

    Python bytecode is created on import and is not part of the delivered
    source. Excluding it keeps an offline archive identity stable after local
    tests or adapter startup.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.relative_to(root).parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            digest.update(b"L\0" + relative.encode("utf-8") + b"\0")
            digest.update(path.readlink().as_posix().encode("utf-8") + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative.encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _merge_state(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge public semantic state without treating it as a filesystem write."""

    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_state(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _positive_seconds(value: Any) -> float | None:
    """Accept only a finite positive per-interaction timeout override."""

    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 and seconds != float("inf") else None


def _changed_paths(before: BaselineSnapshot, after: BaselineSnapshot) -> list[str]:
    before_hashes = {str(item.get("path")): item.get("sha256") for item in before.files}
    after_hashes = {str(item.get("path")): item.get("sha256") for item in after.files}
    return sorted(path for path in set(before_hashes) | set(after_hashes) if before_hashes.get(path) != after_hashes.get(path))


def _render(value: str, variables: dict[str, str]) -> str:
    rendered = value
    for key, replacement in variables.items():
        rendered = rendered.replace("{" + key + "}", replacement)
    return rendered


def _rewrite_named_volume_mount(
    command: list[str], volume_name: str, target: str, *, no_copy: bool = False
) -> list[str]:
    """Rewrite one known named-volume ``-v`` pair to explicit mount syntax."""
    expected = f"{volume_name}:{target}"
    rewritten: list[str] = []
    index = 0
    while index < len(command):
        if index + 1 < len(command) and command[index] == "-v" and command[index + 1] == expected:
            rewritten.extend([
                "--mount",
                f"type=volume,src={volume_name},dst={target}" + (",volume-nocopy" if no_copy else ""),
            ])
            index += 2
            continue
        rewritten.append(command[index])
        index += 1
    return rewritten


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    return clean[:48] or "baseline"


def _safe_relative_state_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise BaselineProtocolError(f"state seed path escapes seed root: {value}")
    return Path(*path.parts)


def _resolve_executable(value: str) -> str:
    """Resolve common host CLIs when a GUI installer omitted PATH registration."""

    if Path(value).is_file() or shutil.which(value):
        return value
    if value == "docker" and os.name == "nt":
        docker_desktop = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
        if docker_desktop.is_file():
            return str(docker_desktop)
    return value
