from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


PROTOCOL_VERSION = "1.0"


class OpenCodeError(RuntimeError):
    pass


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class OpenCodeBridge:
    """Map one benchmark Flow to one durable OpenCode session."""

    def __init__(self, run_command: RunCommand | None = None) -> None:
        self.options: dict[str, Any] = {}
        self.evolution: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.flow_id: str | None = None
        self.turn_id: str | None = None
        self.session_id: str | None = None
        self._run_command = run_command or subprocess.run

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("protocol_version", "")) != PROTOCOL_VERSION:
            raise ValueError("unsupported baseline runtime protocol")
        baseline_id = str(params.get("baseline_id", "opencode"))
        if baseline_id != "opencode":
            raise ValueError(f"unexpected baseline id: {baseline_id}")
        self.options = dict(params.get("options", {}))
        self.evolution = dict(params.get("evolution", {}))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "framework": "opencode",
            "integration": "headless_cli",
            "session_policy": "one_per_flow",
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self.workspace = Path(str(params["workspace"])).resolve()
        self.state_dir = Path(str(params["state_dir"])).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for relative in ("xdg/data", "xdg/cache", "xdg/config", "xdg/state", "home"):
            (self.state_dir / relative).mkdir(parents=True, exist_ok=True)
        self.flow_id = str(params.get("flow_id", ""))
        self.session_id = self._load_session_id()
        return {
            "flow_id": self.flow_id,
            "session_policy": "one_per_flow",
            "resumed_session": bool(self.session_id),
        }

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.workspace is None or self.state_dir is None:
            raise RuntimeError("flow has not started")
        self.turn_id = str(params["turn_id"])
        from trievo_bench.task_terminal import SCHEMA, socket_from_metadata
        self._task_terminal_socket = socket_from_metadata(params)
        if self._task_terminal_socket:
            self._turn_limits = dict(params["metadata"]["agent_limits"])
        return {
            "turn_id": self.turn_id,
            "fresh_session": False,
            "session_id": self.session_id,
            **({"task_terminal_transport": SCHEMA} if self._task_terminal_socket else {}),
        }

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.workspace is None or self.state_dir is None:
            raise RuntimeError("flow has not started")
        if str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("interaction belongs to an inactive turn")

        v5 = bool(getattr(self, "_task_terminal_socket", None))
        if v5:
            from baseline.adapters.native_budget import begin
            begin(self.state_dir / 'foreground-budget.json', self._turn_limits)
        try:
            completed = self._run_command(
            self._opencode_command(),
            input=str(params.get("request", "")),
            cwd=str(self.workspace),
            env=self._environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
                timeout=(float(self._turn_limits["episode_timeout_seconds"]) if v5 else
                         float(self.options.get("request_timeout_seconds", 1800))),
            )
        except subprocess.TimeoutExpired as error:
            if not v5:
                raise
            from baseline.adapters.native_budget import receipt
            path = self.state_dir / 'foreground-budget.json'
            value = receipt(path)
            value['limit_reasons'].append('episode_timeout_seconds')
            path.write_text(json.dumps(value))
            def decode(value):
                return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
            completed = subprocess.CompletedProcess(self._opencode_command(), 124,
                decode(error.stdout), decode(error.stderr))
        parsed = _parse_output(completed.stdout)
        budget = {}
        if v5:
            from baseline.adapters.native_budget import receipt
            budget = receipt(self.state_dir / 'foreground-budget.json')
        exhausted = bool(budget.get('limit_reasons'))
        if parsed["session_id"]:
            received = str(parsed["session_id"])
            if self.session_id is not None and received != self.session_id:
                raise OpenCodeError(
                    f"OpenCode changed session within a Flow: expected {self.session_id}, got {received}"
                )
            self.session_id = received
            self._save_session_id()
        if completed.returncode != 0 and not exhausted:
            detail = parsed["error"] or completed.stderr.strip() or f"exit code {completed.returncode}"
            raise OpenCodeError(f"OpenCode request failed: {detail}")
        if self.session_id is None:
            raise OpenCodeError("OpenCode output did not identify the created session")
        if parsed["error"] and not exhausted:
            raise OpenCodeError(f"OpenCode request failed: {parsed['error']}")

        return {
            "text": parsed["text"],
            "actions": [],
            "tool_calls": parsed["tool_calls"],
            "events": parsed["events"],
            "usage": parsed["usage"],
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
            "raw": {"session_id": self.session_id, **({"native_budget": budget,
                    "limit_exhausted": exhausted, "limit_reasons": budget['limit_reasons']} if v5 else {})},
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("evolution belongs to an inactive turn")
        if getattr(self, '_task_terminal_socket', None):
            (self.state_dir / 'foreground-budget.json').unlink(missing_ok=True)
        reason = params.get("skip_reason") if not bool(params.get("update_enabled", True)) else None
        return {
            "completed": True,
            "events": [{"type": "evolution.skipped", "reason": reason or "opencode_session_continuity"}],
            "usage": {},
        }

    def end_flow(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.turn_id = None
        return {"completed": True, "session_id": self.session_id}

    def _opencode_command(self) -> list[str]:
        executable = self.options.get("executable")
        if isinstance(executable, list) and executable and all(isinstance(item, str) for item in executable):
            command = list(executable)
        elif isinstance(executable, str) and executable.strip():
            command = [executable]
        else:
            command = ["opencode"]

        if bool(self.options.get("pure", True)) and not getattr(self, "_task_terminal_socket", None):
            command.append("--pure")
        command.extend(["run", "--format", "json", "--dir", str(self.workspace)])
        if bool(self.options.get("auto_approve", True)):
            command.append("--auto")
        if self.session_id:
            command.extend(["--session", self.session_id])
        else:
            command.extend(["--title", self.flow_id or "Tri-EvoBench"])
        model = self._model_selector()
        if model:
            command.extend(["--model", model])
        agent = str(self.options.get("agent", "build")).strip()
        if agent:
            command.extend(["--agent", agent])
        variant = str(self.options.get("variant", "")).strip()
        if variant:
            command.extend(["--variant", variant])
        return command

    def _model_selector(self) -> str:
        model = str(self.options.get("model", "")).strip() or os.environ.get("TRIEVO_MODEL", "").strip()
        if not model:
            return ""
        if self._base_url():
            return f"trievo/{model.split('/', 1)[-1]}"
        if "/" in model:
            return model
        provider = str(self.options.get("provider", "openai")).strip()
        return f"{provider}/{model}" if provider else model

    def _base_url(self) -> str:
        return str(self.options.get("base_url", "")).strip() or os.environ.get("TRIEVO_BASE_URL", "").strip()

    def _environment(self) -> dict[str, str]:
        if self.state_dir is None:
            raise RuntimeError("flow has not started")
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(self.state_dir / "home"),
                "XDG_DATA_HOME": str(self.state_dir / "xdg/data"),
                "XDG_CACHE_HOME": str(self.state_dir / "xdg/cache"),
                "XDG_CONFIG_HOME": str(self.state_dir / "xdg/config"),
                "XDG_STATE_HOME": str(self.state_dir / "xdg/state"),
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
                "OPENCODE_DISABLE_SHARE": "1",
                "OPENCODE_CLIENT": "trievo-bench",
                "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": str(
                    int(self.options.get("max_output_tokens", 16384))
                ),
                "OPENCODE_CONFIG_CONTENT": json.dumps(self._config(), ensure_ascii=True),
            }
        )
        if getattr(self, "_task_terminal_socket", None):
            env['TRIEVO_NATIVE_BUDGET'] = str(self.state_dir / 'foreground-budget.json')
        api_key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
        if env.get(api_key_env) and not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = env[api_key_env]
        return env

    def _config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "autoupdate": False,
            "share": "disabled",
            "permission": {
                "*": "allow",
                "external_directory": "deny",
                "question": "deny",
                "plan_enter": "deny",
                "plan_exit": "deny",
            },
        }
        base_url = self._base_url()
        model = str(self.options.get("model", "")).strip() or os.environ.get("TRIEVO_MODEL", "").strip()
        if base_url and model:
            model_id = model.split("/", 1)[-1]
            api_key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
            config["provider"] = {
                "trievo": {
                    "name": "Tri-EvoBench OpenAI-compatible provider",
                    "npm": "@ai-sdk/openai-compatible",
                    "env": [api_key_env],
                    "options": {"baseURL": base_url, "apiKey": "{env:" + api_key_env + "}"},
                    "models": {
                        model_id: {
                            "name": model_id,
                            "tool_call": True,
                            "reasoning": bool(self.options.get("reasoning", True)),
                            "limit": {
                                "context": int(self.options.get("context_window", 128000)),
                                "output": int(self.options.get("max_output_tokens", 16384)),
                            },
                        }
                    },
                }
            }
        extra = self.options.get("config")
        config = _merge(config, extra) if isinstance(extra, dict) else config
        if getattr(self, "_task_terminal_socket", None):
            config["shell"] = "/opt/trievo/task-shell"
            config["plugin"] = ["file:///opt/trievo/baseline/adapters/opencode/task_budget.mjs"]
        return config

    def _session_file(self) -> Path:
        if self.state_dir is None:
            raise RuntimeError("flow has not started")
        return self.state_dir / "session.json"

    def _load_session_id(self) -> str | None:
        path = self._session_file()
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        session_id = raw.get("session_id") if isinstance(raw, dict) else None
        return str(session_id) if session_id else None

    def _save_session_id(self) -> None:
        path = self._session_file()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"flow_id": self.flow_id, "session_id": self.session_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def _parse_output(output: str) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    session_id: str | None = None
    error: str | None = None
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_read_tokens": 0,
        "cached_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "api_calls": 0,
        "estimated_cost_usd": 0.0,
    }
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("sessionID"):
            session_id = str(event["sessionID"])
        event_type = str(event.get("type", ""))
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event_type == "text" and str(part.get("text", "")).strip():
            text_parts.append(str(part["text"]).strip())
            continue
        if event_type == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            tool_calls.append(
                {
                    "tool": str(part.get("tool", "")),
                    "arguments": state.get("input", {}),
                    "result": state.get("output", state.get("error")),
                    "is_error": state.get("status") == "error",
                }
            )
            events.append({"type": "tool.completed", "tool": str(part.get("tool", ""))})
            continue
        if event_type == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            input_tokens = _non_negative_int(tokens.get("input"))
            output_tokens = _non_negative_int(tokens.get("output"))
            reasoning_tokens = _non_negative_int(tokens.get("reasoning"))
            cached_read = _non_negative_int(cache.get("read"))
            cached_write = _non_negative_int(cache.get("write"))
            usage["input_tokens"] += input_tokens
            usage["output_tokens"] += output_tokens
            usage["reasoning_tokens"] += reasoning_tokens
            usage["cached_read_tokens"] += cached_read
            usage["cached_write_tokens"] += cached_write
            reported_total = _non_negative_int(tokens.get("total"))
            usage["total_tokens"] += reported_total or (
                input_tokens + output_tokens + reasoning_tokens + cached_read + cached_write
            )
            usage["api_calls"] += 1
            usage["estimated_cost_usd"] += max(0.0, _float(part.get("cost")))
            events.append({"type": "model.step_finished", "reason": str(part.get("reason", ""))})
            continue
        if event_type == "error":
            error = _error_message(event.get("error"))
    return {
        "text": "\n\n".join(text_parts),
        "tool_calls": tool_calls,
        "events": events,
        "usage": usage,
        "session_id": session_id,
        "error": error,
    }


def _error_message(value: Any) -> str:
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        if value.get("message"):
            return str(value["message"])
        if value.get("name"):
            return str(value["name"])
    return str(value or "unknown OpenCode error")


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _serve() -> None:
    bridge = OpenCodeBridge()
    handlers = {
        "initialize": bridge.initialize,
        "start_flow": bridge.start_flow,
        "start_turn": bridge.start_turn,
        "interact": bridge.interact,
        "evolve": bridge.evolve,
        "end_flow": bridge.end_flow,
    }
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            method = str(request.get("method", ""))
            if method not in handlers:
                raise ValueError(f"unknown method: {method}")
            result = handlers[method](dict(request.get("params", {})))
            response = {"id": request.get("id"), "result": result}
        except Exception as exc:
            response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"message": str(exc)},
            }
        print(json.dumps(response, ensure_ascii=True), flush=True)
        if isinstance(request, dict) and request.get("method") == "end_flow":
            break


if __name__ == "__main__":
    _serve()
