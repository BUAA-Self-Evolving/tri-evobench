from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


PROTOCOL_VERSION = "1.0"
USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cached_read_tokens",
    "cache_write_input_tokens": "cached_write_tokens",
    "reasoning_output_tokens": "reasoning_tokens",
}


class CodexError(RuntimeError):
    pass


class CodexBridge:
    """Map Codex exec JSONL onto the Flow/Turn/Interaction lifecycle."""

    def __init__(self, run_command: Callable[..., Any] | None = None) -> None:
        self.options: dict[str, Any] = {}
        self.evolution: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.turn_id: str | None = None
        self.session_id: str | None = None
        self._usage: dict[str, int] = {}
        self._evolved = True
        self._run_command = run_command or subprocess.run

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("unsupported baseline runtime protocol")
        if params.get("baseline_id") != "codex":
            raise ValueError("unexpected baseline id")
        self.options = dict(params.get("options", {}))
        self.evolution = dict(params.get("evolution", {}))
        if self.evolution.get("policy", "disabled") not in {"disabled", "turn_boundary"}:
            raise ValueError("Codex evolution.policy must be disabled or turn_boundary")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "framework": "codex",
            "integration": "exec_jsonl",
            "session_policy": "fresh_per_turn",
            "api_calls_semantics": "exec_invocations_lower_bound",
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self.workspace = Path(str(params["workspace"])).resolve()
        self.state_dir = Path(str(params["state_dir"])).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "home").mkdir(exist_ok=True)
        (self.state_dir / "learning").mkdir(exist_ok=True)
        self.session_id = None
        self.turn_id = None
        self._evolved = True
        return {"flow_id": str(params.get("flow_id", "")), "session_policy": "fresh_per_turn"}

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_flow()
        if not self._evolved:
            raise RuntimeError("previous turn has not crossed the evolution barrier")
        scratch = self.state_dir / "sessions"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir()
        self.turn_id = str(params["turn_id"])
        self.session_id = None
        self._usage = {}
        self._evolved = False
        return {"turn_id": self.turn_id, "fresh_session": True}

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_turn(params)
        if self._evolved:
            raise RuntimeError("turn already crossed the evolution barrier")
        if int(params.get("remaining_token_budget", 1)) <= 0:
            raise CodexError("Codex interaction token budget is exhausted")
        prompt = str(params.get("request", ""))
        if not self.session_id:
            prompt = (
                "Complete the user task in the shared workspace. Do not access evaluator or "
                "baseline state directories or modify persistent learning state.\n"
                + "Flow memory (read-only):\n" + self._memory() + "\n\n" + prompt
            )
            if params.get("history"):
                prompt += "\nPublic conversation:\n" + json.dumps(params["history"], ensure_ascii=True)
        parsed = self._execute(prompt, evolution=False)
        received = parsed["session_id"]
        if self.session_id is not None and received != self.session_id:
            raise CodexError("Codex changed session within a Turn")
        self.session_id = received
        usage = _usage_delta(self._usage, parsed["usage"])
        self._usage = parsed["usage"]
        return {
            "text": parsed["text"],
            "actions": [],
            "tool_calls": parsed["tool_calls"],
            "events": parsed["events"],
            "usage": usage,
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
            "raw": {"session_id": self.session_id},
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_turn(params)
        if self._evolved:
            raise RuntimeError("turn already crossed the evolution barrier")
        if not params.get("update_enabled", True) or self.evolution.get("policy", "disabled") == "disabled":
            self._evolved = True
            return {
                "completed": True,
                "events": [{"type": "evolution.skipped", "reason": params.get("skip_reason") or "policy_disabled"}],
                "usage": {},
            }
        prompt = (
            "Review only the public trajectory and feedback below. Return a JSON object with "
            "a memory string containing concise reusable lessons, preserving useful existing lessons. "
            "Do not use tools, inspect files, infer hidden answers, or solve another task.\n"
            + "Existing memory:\n" + self._memory() + "\nPublic evidence:\n"
            + json.dumps(_public_evolution_context(params), ensure_ascii=True)
        )
        parsed = self._execute(prompt, evolution=True)
        usage = _usage_delta({}, parsed["usage"])
        self._evolved = True
        try:
            learned = json.loads(parsed["text"])
            memory = learned["memory"]
            if not isinstance(memory, str) or len(memory) > int(self.options.get("memory_max_chars", 12000)):
                raise ValueError("memory must be a bounded string")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return {
                "completed": False,
                "events": [{"type": "evolution.failed", "message": str(exc)}],
                "usage": usage,
            }
        target = self.state_dir / "learning" / "memory.md"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(memory, encoding="utf-8")
        temporary.replace(target)
        return {"completed": True, "events": [{"type": "evolution.completed"}], "usage": usage}

    def end_flow(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.turn_id = None
        self.session_id = None
        return {"completed": True}

    def _require_flow(self) -> None:
        if self.workspace is None or self.state_dir is None:
            raise RuntimeError("flow has not started")

    def _require_turn(self, params: dict[str, Any]) -> None:
        self._require_flow()
        if self.turn_id is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("request belongs to an inactive turn")

    def _memory(self) -> str:
        target = self.state_dir / "learning" / "memory.md"
        return target.read_text(encoding="utf-8") if target.exists() else ""

    def _environment(self, codex_home: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.state_dir / "home")
        env["CODEX_HOME"] = str(codex_home)
        key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
        key = env.get(key_env) or env.get("CODEX_API_KEY") or env.get("OPENAI_API_KEY")
        if key:
            env["TRIEVO_CODEX_API_KEY"] = key
            env["CODEX_API_KEY"] = key
        return env

    def _execute(self, prompt: str, *, evolution: bool) -> dict[str, Any]:
        codex_home = self.state_dir / ("evolution-home" if evolution else "sessions")
        if evolution and codex_home.exists():
            shutil.rmtree(codex_home)
        codex_home.mkdir(exist_ok=True)
        config = self._config(evolution=evolution)
        (codex_home / "config.toml").write_text(config, encoding="utf-8")
        executable = self.options.get("executable", "codex")
        command = list(executable) if isinstance(executable, list) else [str(executable)]
        command.extend(["exec", "--json", "--skip-git-repo-check"])
        if evolution:
            command.extend(["--ephemeral", "--sandbox", "read-only"])
            schema = codex_home / "memory.schema.json"
            schema.write_text(json.dumps({
                "type": "object", "properties": {"memory": {"type": "string"}},
                "required": ["memory"], "additionalProperties": False,
            }), encoding="utf-8")
            command.extend(["--output-schema", str(schema)])
            cwd = codex_home / "workspace"
            cwd.mkdir(exist_ok=True)
        else:
            command.extend(["--sandbox", "danger-full-access"])
            cwd = self.workspace
        if self.session_id and not evolution:
            command.extend(["resume", self.session_id])
        command.append("-")
        timeout = float(
            self.evolution.get("timeout_seconds", 900) if evolution
            else self.options.get("request_timeout_seconds", 1700)
        )
        try:
            completed = self._run_command(
                command, input=prompt, cwd=str(cwd), env=self._environment(codex_home),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexError("Codex request timed out; do not replay executed side effects automatically") from exc
        try:
            parsed = _parse_output(completed.stdout)
        except CodexError as exc:
            if completed.returncode != 0:
                detail = completed.stderr.strip()[-2000:]
                key = self._environment(codex_home).get("TRIEVO_CODEX_API_KEY")
                if key:
                    detail = detail.replace(key, "[redacted]")
                raise CodexError(f"Codex exited with code {completed.returncode}: {detail}") from exc
            raise
        if completed.returncode != 0:
            raise CodexError(parsed["error"] or f"Codex exited with code {completed.returncode}")
        if parsed["error"]:
            raise CodexError(parsed["error"])
        return parsed

    def _config(self, *, evolution: bool) -> str:
        model = str(self.options.get("model") or os.getenv("TRIEVO_MODEL") or "")
        if not model:
            raise ValueError("options.model or TRIEVO_MODEL is required")
        base_url = str(
            self.options.get("base_url") or os.getenv("TRIEVO_BASE_URL")
            or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        )
        values: dict[str, Any] = {
            "model": model,
            "model_provider": "trievo",
            "approval_policy": "never",
            "model_reasoning_effort": str(self.options.get("reasoning_effort", "medium")),
            "web_search": "disabled" if evolution else str(self.options.get("web_search", "disabled")),
            "memories.generate_memories": False,
            "memories.use_memories": False,
            "features.memories": False,
            "features.multi_agent": False,
            "features.multi_agent_v2": False,
            "features.shell_tool": not evolution,
        }
        for name in ("model_context_window", "model_auto_compact_token_limit"):
            if self.options.get(name) is not None:
                values[name] = int(self.options[name])
        settings = "\n".join(f"{name} = {json.dumps(value)}" for name, value in values.items())
        return (
            settings + "\n[model_providers.trievo]\nname = \"Tri-EvoBench\"\n"
            + f"base_url = {json.dumps(base_url)}\n"
            + "env_key = \"TRIEVO_CODEX_API_KEY\"\nwire_api = \"responses\"\n"
            + f"request_max_retries = {int(self.options.get('request_max_retries', 2))}\n"
        )


def _usage_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    usage = {
        target: max(0, after.get(source, 0) - before.get(source, 0))
        for source, target in USAGE_FIELDS.items()
    }
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["api_calls"] = 1
    return usage


def _parse_output(output: str) -> dict[str, Any]:
    session_id = None
    text = ""
    usage = None
    error = None
    events = []
    calls = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexError("Codex emitted non-JSON output") from exc
        if not isinstance(event, dict):
            raise CodexError("Codex event must be an object")
        kind = event.get("type")
        events.append(event)
        if kind == "thread.started":
            session_id = event.get("thread_id")
        elif kind == "turn.completed":
            raw = event.get("usage")
            if not isinstance(raw, dict):
                raise CodexError("Codex completion is missing usage")
            try:
                usage = {name: int(raw.get(name, 0)) for name in USAGE_FIELDS}
            except (TypeError, ValueError) as exc:
                raise CodexError("Codex usage must contain integers") from exc
            if any(value < 0 for value in usage.values()):
                raise CodexError("Codex usage must be non-negative")
        elif kind in {"error", "turn.failed"}:
            detail = event.get("error", event)
            error = str(detail.get("message", "Codex failed")) if isinstance(detail, dict) else str(detail)
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise CodexError("Codex item must be an object")
            item_type = item.get("type")
            if item_type == "agent_message":
                text = str(item.get("text", ""))
            elif item_type in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}:
                calls.append({"tool": item_type, "arguments": item, "result": item.get("aggregated_output", item.get("result"))})
    if not error and (not isinstance(session_id, str) or not session_id or usage is None):
        raise CodexError("Codex output is missing a session or completed turn")
    return {"session_id": session_id, "text": text, "usage": usage or {}, "error": error, "events": events, "tool_calls": calls}


def _public_evolution_context(params: dict[str, Any]) -> dict[str, Any]:
    trajectory_keys = {"index", "interaction_index", "request", "response", "actions", "tool_calls", "events", "score", "passed"}
    feedback_keys = {"interaction_index", "feedback", "score", "passed"}
    return {
        "turn_id": params.get("turn_id"),
        "trajectory": [{key: value for key, value in row.items() if key in trajectory_keys}
                       for row in params.get("trajectory", []) if isinstance(row, dict)],
        "feedback": [{key: value for key, value in row.items() if key in feedback_keys}
                     for row in params.get("feedback", []) if isinstance(row, dict)],
    }


def main() -> int:
    bridge = CodexBridge()
    methods = {name: getattr(bridge, name) for name in (
        "initialize", "start_flow", "start_turn", "interact", "evolve", "end_flow",
    )}
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("baseline request must be an object")
            method = request["method"]
            result = methods[method](request.get("params", {}))
            response = {"id": request.get("id"), "result": result}
        except Exception as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            response = {"id": request_id, "error": {"message": f"{type(exc).__name__}: {exc}"}}
        print(json.dumps(response, ensure_ascii=True), flush=True)
        if isinstance(request, dict) and request.get("method") == "end_flow":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
