from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


PROTOCOL_VERSION = "1.0"


class PrimeRpcError(RuntimeError):
    pass


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
        )
        self._request_id = 0

    def drain(self) -> None:
        """Discard fire-and-forget events left after an RPC command response."""
        output = self.process.stdout
        if output is None:
            return
        deadline = time.monotonic() + 0.05
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([output], [], [], remaining)[0]:
                return
            if not output.readline():
                return

    def request(self, command_type: str, **params: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._request_id += 1
        request_id = f"trievo-{self._request_id}"
        payload = {"id": request_id, "type": command_type, **params}
        stream = self.process.stdin
        if stream is None:
            raise PrimeRpcError("Prime Agent RPC stdin is unavailable")
        stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
        stream.flush()

        response: dict[str, Any] | None = None
        events: list[dict[str, Any]] = []
        agent_finished = command_type != "prompt"
        early_response: dict[str, Any] | None = None
        while response is None or not agent_finished:
            output = self.process.stdout
            line = output.readline() if output is not None else ""
            if not line:
                raise PrimeRpcError(
                    f"Prime Agent RPC closed during {command_type} (exit={self.process.poll()})"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PrimeRpcError(f"Prime Agent emitted non-JSON RPC output: {line[:500]}") from exc
            if not isinstance(message, dict):
                continue
            if message.get("type") == "response" and message.get("id") == request_id:
                if not message.get("success", False):
                    raise PrimeRpcError(str(message.get("error", f"{command_type} failed")))
                data = message.get("data")
                response = dict(data) if isinstance(data, dict) else {}
                early_response = response
                continue
            events.append(message)
            if message.get("type") == "agent_end":
                agent_finished = True
        if response is None and agent_finished and command_type == "prompt":
            response = self._await_response(request_id, command_type, events)
        return response or early_response or {}, events

    def _await_response(
        self,
        request_id: str,
        command_type: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        while True:
            output = self.process.stdout
            line = output.readline() if output is not None else ""
            if not line:
                raise PrimeRpcError(f"Prime Agent RPC closed while awaiting {command_type} acknowledgement")
            message = json.loads(line)
            if isinstance(message, dict) and message.get("type") == "response" and message.get("id") == request_id:
                if not message.get("success", False):
                    raise PrimeRpcError(str(message.get("error", f"{command_type} failed")))
                data = message.get("data")
                return dict(data) if isinstance(data, dict) else {}
            if isinstance(message, dict):
                events.append(message)

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


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
        command = self._prime_command()
        env = dict(os.environ)
        env["PRIME_AGENT_CODING_AGENT_DIR"] = str(self.state_dir / "prime-home")
        env["PRIME_AGENT_SESSION_DIR"] = str(self.state_dir / "sessions")
        env["HOME"] = str(self.state_dir / "home")
        if os.environ.get("TRIEVO_API_KEY") and not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.environ["TRIEVO_API_KEY"]
        self.rpc = self._rpc_factory(command, env)
        self.rpc.request("get_state")
        return {"turn_id": self.turn_id, "fresh_session": True}

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.rpc is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("interaction belongs to an inactive turn")
        before, _ = self.rpc.request("get_session_stats")
        _, events = self.rpc.request("prompt", message=str(params.get("request", "")))
        last, _ = self.rpc.request("get_last_assistant_text")
        after, _ = self.rpc.request("get_session_stats")
        return {
            "text": str(last.get("text", "")),
            "actions": [],
            "tool_calls": _tool_trace(events),
            "events": _public_events(events),
            "usage": _usage_delta(before, after),
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
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
        before, _ = self.rpc.request("get_session_stats")
        result, events = self.rpc.request("refine", instructions=instructions, **{"global": True})
        after, _ = self.rpc.request("get_session_stats")
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

    def _close_rpc(self) -> None:
        if self.rpc is not None:
            self.rpc.close()
            self.rpc = None


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
