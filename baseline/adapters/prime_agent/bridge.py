from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


PROTOCOL_VERSION = "1.0"


class PrimeRpcError(RuntimeError):
    pass


_RPC_STREAM_CLOSED = object()


class PrimeRpcProcess:
    """Small synchronous client for Prime Agent's documented JSONL RPC mode."""

    def __init__(self, command: list[str], *, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=os.name != "nt",
        )
        self._request_id = 0
        self._messages: queue.Queue[Any] = queue.Queue()
        self._pending: dict[str, dict[str, Any]] = {}
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="prime-rpc-reader",
            daemon=True,
        )
        self._reader.start()

    def _read_stdout(self) -> None:
        output = self.process.stdout
        if output is None:
            self._messages.put(_RPC_STREAM_CLOSED)
            return
        try:
            for line in output:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {"protocol_error": f"Prime Agent emitted non-JSON RPC output: {line[:500]}"}
                self._messages.put(message)
        finally:
            self._messages.put(_RPC_STREAM_CLOSED)

    def _send(self, command_type: str, params: dict[str, Any]) -> str:
        with self._write_lock:
            self._request_id += 1
            request_id = f"trievo-{self._request_id}"
            payload = {"id": request_id, "type": command_type, **params}
            stream = self.process.stdin
            if stream is None:
                raise PrimeRpcError("Prime Agent RPC stdin is unavailable")
            try:
                stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
                stream.flush()
            except (BrokenPipeError, OSError) as exc:
                raise PrimeRpcError("Prime Agent RPC closed its input") from exc
            return request_id

    def abort(self) -> None:
        """Request cancellation without waiting behind the in-flight prompt."""
        if self.process.poll() is None:
            self._send("abort", {})

    def _next_message(self, deadline: float | None, command_type: str) -> dict[str, Any]:
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        if timeout == 0:
            try:
                self.abort()
            except PrimeRpcError:
                pass
            raise PrimeRpcError(f"Prime Agent RPC absolute deadline expired during {command_type}")
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            try:
                self.abort()
            except PrimeRpcError:
                pass
            raise PrimeRpcError(f"Prime Agent RPC absolute deadline expired during {command_type}") from exc
        if message is _RPC_STREAM_CLOSED:
            raise PrimeRpcError(
                f"Prime Agent RPC closed during {command_type} (exit={self.process.poll()})"
            )
        if not isinstance(message, dict):
            raise PrimeRpcError("Prime Agent emitted a non-object RPC message")
        if message.get("protocol_error"):
            raise PrimeRpcError(str(message["protocol_error"]))
        return message

    def drain(self) -> None:
        """Discard fire-and-forget events left after an RPC command response."""
        while True:
            try:
                message = self._messages.get_nowait()
            except queue.Empty:
                return
            if message is _RPC_STREAM_CLOSED:
                self._messages.put(message)
                return

    def request(
        self,
        command_type: str,
        *,
        deadline: float | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        **params: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        request_id = self._send(command_type, params)
        response: dict[str, Any] | None = None
        events: list[dict[str, Any]] = []
        agent_finished = command_type != "prompt"
        saw_agent_start = False
        idle_candidate = False
        state_request_id: str | None = None
        while response is None or not agent_finished:
            message = self._pending.pop(request_id, None)
            if message is None:
                message = self._next_message(deadline, command_type)
            if message.get("type") == "response":
                message_id = str(message.get("id", ""))
                if state_request_id is not None and message_id == state_request_id:
                    if not message.get("success", False):
                        raise PrimeRpcError(str(message.get("error", "get_state failed")))
                    state = message.get("data")
                    if idle_candidate and isinstance(state, dict) and not bool(state.get("isStreaming", True)):
                        agent_finished = True
                    elif isinstance(state, dict) and bool(state.get("isStreaming", False)):
                        idle_candidate = False
                    state_request_id = None
                    continue
                if message_id != request_id:
                    if message_id:
                        self._pending[message_id] = message
                    continue
                if not message.get("success", False):
                    raise PrimeRpcError(str(message.get("error", f"{command_type} failed")))
                data = message.get("data")
                response = dict(data) if isinstance(data, dict) else {}
                if command_type == "prompt" and idle_candidate and state_request_id is None:
                    state_request_id = self._send("get_state", {})
                continue
            events.append(message)
            if event_callback is not None:
                event_callback(message)
            if message.get("type") == "agent_start":
                # A retry can start after an earlier agent_end. Only an idle
                # state observed after the newest start is authoritative.
                saw_agent_start = True
                idle_candidate = False
                agent_finished = False
            if message.get("type") == "agent_end":
                idle_candidate = saw_agent_start
            if response is not None and idle_candidate and state_request_id is None:
                state_request_id = self._send("get_state", {})
        return response or {}, events

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._signal_tree(force=False)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_tree(force=True)
                self.process.wait(timeout=5)

    def _signal_tree(self, *, force: bool) -> None:
        sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(self.process.pid), sig)
                return
            except (OSError, ProcessLookupError):
                pass
        if force:
            self.process.kill()
        else:
            self.process.terminate()


class PrimeAgentBridge:
    def __init__(
        self,
        rpc_factory: Callable[[list[str], dict[str, str]], Any] | None = None,
    ) -> None:
        self.options: dict[str, Any] = {}
        self.evolution: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.rpc: Any = None
        self.turn_id: str | None = None
        self._turn_deadline: float | None = None
        self._model_calls = 0
        self._tool_calls = 0
        self._max_model_calls = 0
        self._max_tool_calls = 0
        self._limit_reason: str | None = None
        self._abort_sent = False
        self._rpc_factory = rpc_factory or (lambda command, env: PrimeRpcProcess(command, env=env))

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("protocol_version", "")) != PROTOCOL_VERSION:
            raise ValueError("unsupported baseline runtime protocol")
        baseline_id = str(params.get("baseline_id", "prime-agent"))
        if baseline_id != "prime-agent":
            raise ValueError(f"unexpected baseline id: {baseline_id}")
        self.options = dict(params.get("options", {}))
        self.evolution = dict(params.get("evolution", {}))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "framework": "prime-agent",
            "integration": "rpc",
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self.workspace = Path(str(params["workspace"])).resolve()
        self.state_dir = Path(str(params["state_dir"])).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "home").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "prime-home").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "sessions").mkdir(parents=True, exist_ok=True)
        self._write_custom_model_config()
        self._write_runtime_settings()
        return {"flow_id": str(params.get("flow_id", "")), "session_policy": "fresh_per_turn"}

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        self._close_rpc()
        if self.workspace is None or self.state_dir is None:
            raise RuntimeError("flow has not started")
        # Keep only the Flow-level continual harness. Session transcripts and
        # Python REPL snapshots must never leak into the next Turn.
        for path in (self.state_dir / "sessions").glob("*"):
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
            else:
                path.unlink()
        self.turn_id = str(params["turn_id"])
        self._configure_turn_limits(params)
        command = self._prime_command()
        env = dict(os.environ)
        env["PRIME_AGENT_CODING_AGENT_DIR"] = str(self.state_dir / "prime-home")
        env["PRIME_AGENT_SESSION_DIR"] = str(self.state_dir / "sessions")
        env["HOME"] = str(self.state_dir / "home")
        if os.environ.get("TRIEVO_API_KEY") and not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.environ["TRIEVO_API_KEY"]
        self.rpc = self._rpc_factory(command, env)
        state, _ = self._request("get_state")
        if bool(state.get("isStreaming", False)):
            raise PrimeRpcError("Prime Agent session is unexpectedly streaming at Turn start")
        return {"turn_id": self.turn_id, "fresh_session": True}

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.rpc is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("interaction belongs to an inactive turn")
        if self._model_calls >= self._max_model_calls:
            self._exhaust("max_model_calls")
        elif self._tool_calls >= self._max_tool_calls:
            self._exhaust("max_tool_calls")
        if self._limit_reason is not None:
            raise PrimeRpcError(f"Prime Agent Turn budget exhausted: {self._limit_reason}")
        before, _ = self._request("get_session_stats")
        _, events = self._request(
            "prompt",
            event_callback=self._observe_event,
            message=str(params.get("request", "")),
        )
        last, _ = self._request("get_last_assistant_text")
        after, _ = self._request("get_session_stats")
        return {
            "text": str(last.get("text", "")),
            "actions": [],
            "tool_calls": _tool_trace(events),
            "events": _public_events(events),
            "usage": _usage_delta(before, after),
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
            "raw": {
                "limit_exhausted": self._limit_reason is not None,
                "limit_reasons": [self._limit_reason] if self._limit_reason else [],
                "model_calls": self._model_calls,
                "tool_calls": self._tool_calls,
            },
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.rpc is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("evolution belongs to an inactive turn")
        if not bool(params.get("update_enabled", True)):
            return {
                "completed": True,
                "events": [{"type": "evolution.skipped", "reason": params.get("skip_reason")}],
                "usage": {},
            }
        if str(self.evolution.get("policy", "turn_boundary")) == "disabled":
            return {
                "completed": True,
                "events": [{"type": "evolution.skipped", "reason": "policy_disabled"}],
                "usage": {},
            }

        drain = getattr(self.rpc, "drain", None)
        if callable(drain):
            drain()

        public_context = _public_evolution_context(params)
        instructions = (
            "This is the Tri-EvoBench turn-boundary evolution barrier. Review only the public "
            "trajectory and public judge feedback below. Apply small, reusable improvements to "
            "the continual harness. Do not infer or request hidden verifier data.\n\n"
            + json.dumps(public_context, ensure_ascii=False, indent=2)
        )
        evolution_timeout = _positive_number(
            self.evolution.get("timeout_seconds"),
            default=900.0,
        )
        deadline = time.monotonic() + evolution_timeout
        before, _ = self._request("get_session_stats", deadline=deadline)
        result, events = self._request(
            "refine",
            deadline=deadline,
            instructions=instructions,
            **{"global": True},
        )
        after, _ = self._request("get_session_stats", deadline=deadline)
        usage = _usage_delta(before, after)
        if not usage["api_calls"]:
            # Prime's refinement request is a model call, although current RPC
            # session stats do not include its off-transcript usage.
            usage["api_calls"] = 1
        return {
            "completed": True,
            "events": [
                {
                    "type": "evolution.completed",
                    "refinement_id": result.get("id"),
                    "applied_edits": len(result.get("appliedEdits", [])),
                },
                *_public_events(events),
            ],
            "usage": usage,
        }

    def end_flow(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._close_rpc()
        self.turn_id = None
        return {"completed": True}

    def _prime_command(self) -> list[str]:
        if self.workspace is None or self.state_dir is None:
            raise RuntimeError("flow has not started")
        executable = str(
            self.options.get("executable")
            or os.environ.get("PRIME_AGENT_CLI")
            or "baseline/prime-agent/prime-agent.sh"
        )
        command = [executable]
        if bool(self.options.get("use_dist", False)):
            command.append("--dist")
        command.extend(
            [
                "--mode",
                "rpc",
                "--cwd",
                str(self.workspace),
                "--session-dir",
                str(self.state_dir / "sessions"),
                "--offline",
                "--no-context-files",
                "--no-extensions",
                "--no-prompt-templates",
                "--no-skills",
                "--append-system-prompt",
                (
                    "Tri-EvoBench controls learning at turn boundaries. During task execution, do not "
                    "call refine.run, /refine, or any rlm.harness create/update/delete method. Work "
                    "only in the task workspace. The evaluator will invoke refinement separately."
                ),
            ]
        )
        model = self._model_selector()
        if model:
            command.extend(["--model", model])
        thinking = str(self.options.get("thinking", "")).strip()
        if thinking:
            command.extend(["--thinking", thinking])
        return command

    def _model_selector(self) -> str:
        model = str(self.options.get("model", "")).strip()
        base_url = str(self.options.get("base_url", "")).strip()
        if base_url and model:
            return f"trievo/{model.split('/', 1)[-1]}"
        provider = str(self.options.get("provider", "")).strip()
        if provider and model and "/" not in model:
            return f"{provider}/{model}"
        return model

    def _write_custom_model_config(self) -> None:
        if self.state_dir is None:
            return
        base_url = str(self.options.get("base_url", "")).strip()
        model = str(self.options.get("model", "")).strip()
        if not base_url or not model:
            return
        api_key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
        config = {
            "providers": {
                "trievo": {
                    "baseUrl": base_url,
                    "api": str(self.options.get("api", "openai-completions")),
                    "apiKey": api_key_env,
                    "models": [
                        {
                            "id": model.split("/", 1)[-1],
                            "name": model.split("/", 1)[-1],
                            "reasoning": bool(self.options.get("reasoning", False)),
                            "contextWindow": int(self.options.get("context_window", 128000)),
                            "maxTokens": int(self.options.get("max_output_tokens", 16384)),
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
            }
        }
        path = self.state_dir / "prime-home" / "models.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_runtime_settings(self) -> None:
        if self.state_dir is None:
            return
        path = self.state_dir / "prime-home" / "settings.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        retry = current.get("retry") if isinstance(current.get("retry"), dict) else {}
        provider = retry.get("provider") if isinstance(retry.get("provider"), dict) else {}
        timeout_seconds = _positive_number(
            self.options.get("request_timeout_seconds")
            or os.environ.get("TRIEVO_API_TIMEOUT_SECONDS"),
            default=300.0,
        )
        configured_retries = _non_negative_int(
            self.options.get("max_retries")
            if self.options.get("max_retries") is not None
            else os.environ.get("TRIEVO_API_MAX_RETRIES"),
            default=5,
        )
        current["retry"] = {
            **retry,
            "enabled": True,
            "maxRetries": min(5, configured_retries),
            "baseDelayMs": 2000,
            "provider": {
                **provider,
                "timeoutMs": int(timeout_seconds * 1000),
                "maxRetryDelayMs": 60000,
            },
        }
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    def _configure_turn_limits(self, params: dict[str, Any]) -> None:
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        limits = metadata.get("agent_limits") if isinstance(metadata.get("agent_limits"), dict) else {}
        if not limits and isinstance(self.options.get("l2_agent_limits"), dict):
            limits = self.options["l2_agent_limits"]
        episode_seconds = _positive_number(limits.get("episode_timeout_seconds"), default=900.0)
        self._turn_deadline = time.monotonic() + episode_seconds
        self._max_model_calls = _positive_int(limits.get("max_model_calls"), default=24)
        self._max_tool_calls = _positive_int(limits.get("max_tool_calls"), default=20)
        self._model_calls = 0
        self._tool_calls = 0
        self._limit_reason = None
        self._abort_sent = False

    def _request(
        self,
        command_type: str,
        *,
        deadline: float | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        **params: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.rpc is None:
            raise PrimeRpcError("Prime Agent RPC is not running")
        effective_deadline = deadline if deadline is not None else self._turn_deadline
        if isinstance(self.rpc, PrimeRpcProcess):
            return self.rpc.request(
                command_type,
                deadline=effective_deadline,
                event_callback=event_callback,
                **params,
            )
        result, events = self.rpc.request(command_type, **params)
        if event_callback is not None:
            for event in events:
                if isinstance(event, dict):
                    event_callback(event)
        return result, events

    def _observe_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                self._model_calls += 1
        elif event_type == "tool_execution_start":
            self._tool_calls += 1
        if self._model_calls > self._max_model_calls:
            self._exhaust("max_model_calls")
        elif self._tool_calls > self._max_tool_calls:
            self._exhaust("max_tool_calls")
        elif event_type == "tool_execution_end" and self._model_calls >= self._max_model_calls:
            self._exhaust("max_model_calls")

    def _exhaust(self, reason: str) -> None:
        if self._limit_reason is None:
            self._limit_reason = reason
        if self.rpc is not None and not self._abort_sent:
            abort = getattr(self.rpc, "abort", None)
            if callable(abort):
                self._abort_sent = True
                abort()

    def _close_rpc(self) -> None:
        if self.rpc is not None:
            self.rpc.close()
            self.rpc = None


def _positive_number(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _usage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_tokens = before.get("tokens", {}) if isinstance(before.get("tokens"), dict) else {}
    after_tokens = after.get("tokens", {}) if isinstance(after.get("tokens"), dict) else {}
    input_tokens = max(0, int(after_tokens.get("input", 0)) - int(before_tokens.get("input", 0)))
    output_tokens = max(0, int(after_tokens.get("output", 0)) - int(before_tokens.get("output", 0)))
    cached_read = max(0, int(after_tokens.get("cacheRead", 0)) - int(before_tokens.get("cacheRead", 0)))
    cached_write = max(0, int(after_tokens.get("cacheWrite", 0)) - int(before_tokens.get("cacheWrite", 0)))
    api_calls = max(0, int(after.get("assistantMessages", 0)) - int(before.get("assistantMessages", 0)))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_read_tokens": cached_read,
        "cached_write_tokens": cached_write,
        "total_tokens": input_tokens + output_tokens + cached_read + cached_write,
        "api_calls": api_calls,
        "estimated_cost_usd": max(0.0, float(after.get("cost", 0.0)) - float(before.get("cost", 0.0))),
    }


def _tool_trace(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_execution_end":
            continue
        trace.append(
            {
                "tool": str(event.get("toolName", "")),
                "arguments": event.get("args", {}),
                "result": event.get("result"),
                "is_error": bool(event.get("isError", False)),
            }
        )
    return trace


def _public_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "tool_execution_start",
        "tool_execution_end",
        "compaction_start",
        "compaction_end",
        "refine_complete",
        "refine_failed",
    }
    return [{"type": str(event.get("type", "unknown"))} for event in events if event.get("type") in allowed]


def _public_evolution_context(params: dict[str, Any]) -> dict[str, Any]:
    trajectory = []
    for raw in params.get("trajectory", []):
        if not isinstance(raw, dict):
            continue
        trajectory.append(
            {
                key: raw[key]
                for key in ("index", "request", "response", "score", "passed", "tool_calls", "events")
                if key in raw
            }
        )
    feedback = []
    for raw in params.get("feedback", []):
        if not isinstance(raw, dict):
            continue
        feedback.append(
            {
                key: raw[key]
                for key in ("interaction_index", "feedback", "score", "passed")
                if key in raw
            }
        )
    return {"turn_id": str(params.get("turn_id", "")), "trajectory": trajectory, "feedback": feedback}


def _serve() -> None:
    bridge = PrimeAgentBridge()
    handlers = {
        "initialize": bridge.initialize,
        "start_flow": bridge.start_flow,
        "start_turn": bridge.start_turn,
        "interact": bridge.interact,
        "evolve": bridge.evolve,
        "end_flow": bridge.end_flow,
    }
    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = str(request.get("method", ""))
            if method not in handlers:
                raise ValueError(f"unknown method: {method}")
            result = handlers[method](dict(request.get("params", {})))
            response = {"id": request_id, "result": result}
        except Exception as exc:
            response = {"id": request.get("id") if isinstance(request, dict) else None, "error": {"message": str(exc)}}
        print(json.dumps(response, ensure_ascii=True), flush=True)
        if isinstance(request, dict) and request.get("method") == "end_flow":
            break


if __name__ == "__main__":
    _serve()
