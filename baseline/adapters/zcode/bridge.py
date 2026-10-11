from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from baseline.adapters.native_runtime import NativeBridge, integer, serve


class ZCodeBridge(NativeBridge):
    baseline_id = "zcode"
    integration = "cli_stream_json"

    def __init__(self, run_command: Callable[..., Any] | None = None) -> None:
        super().__init__()
        self.run_command = run_command or subprocess.run

    def provider_config(self, key: str) -> dict[str, Any]:
        model = self.options.get("model") or os.getenv("TRIEVO_MODEL")
        if not model:
            raise ValueError("options.model or TRIEVO_MODEL is required")
        selection: dict[str, Any] = {"providerId": "trievo", "modelId": model}
        reasoning = self.options.get("reasoning_effort")
        if reasoning:
            selection["options"] = {"reasoningLevel": reasoning}
        model_config = deepcopy(self.options.get("model_config", {}))
        model_config["enabled"] = True
        model_config.setdefault("properties", {})["contextWindow"] = int(self.options.get("context_window", 128000))
        return {
            "schemaVersion": 1,
            "config": {
                "providerConfigRules": {"providerRules": [{
                    "providerId": "trievo",
                    "config": {
                        "group": "standard-personal",
                        "access": {"type": "api-key", "apiKey": key},
                        "api": {
                            "type": self.options.get("api_type", "openai-chat-completions"),
                            "baseUrl": self.options.get("base_url") or os.getenv("TRIEVO_BASE_URL") or "https://api.z.ai/api/paas/v4",
                        },
                        "personalModelIds": [model],
                    },
                }]},
                "modelConfigRules": {
                    "providerModelRules": [{"providerId": "trievo", "modelId": model, "config": model_config}],
                    "manualProviderModelRules": [],
                },
                "defaultModelSelection": selection,
            },
        }

    def environment(self, personal: Path) -> dict[str, str]:
        env = dict(os.environ)
        for name in tuple(env):
            if name.startswith("ZCODE_") or name.startswith("XDG_"):
                env.pop(name)
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "ZCODE_DATA_BASE_DIR": str(self.home),
            "ZCODE_STORAGE_DIR": str(self.home / "storage"),
            "ZCODE_SESSION_DB_PATH": str(self.home / "sessions.db"),
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": str(personal),
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(self.options.get(
                "builtin_config", "/opt/zcode/config/provider/zcode-builtin.json",
            )),
        })
        return env

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt = self.prompt(params)
        if prompt.lstrip().startswith("/"):
            prompt = "User task:\n" + prompt
        key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
        key = os.getenv(key_env) or os.getenv("ZAI_API_KEY") or ""
        if not key:
            raise ValueError(f"{key_env} or ZAI_API_KEY is required")
        personal = self.home / "provider_config.json"
        with personal.open("w", encoding="utf-8") as handle:
            personal.chmod(0o600)
            json.dump(self.provider_config(key), handle, ensure_ascii=True)
        config_dir = self.home / ".zcode" / "cli"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps({
            "features": {"memory": False, "subagent": False, "mcp": False},
            "memory": {"use": False}, "plugins": {"enabled": False},
            "hooks": {"enabled": False},
        }), encoding="utf-8")
        executable = self.options.get("executable", "zcode")
        command = list(executable) if isinstance(executable, list) else [str(executable)]
        command.extend([
            "--output-format", "stream-json", "--no-color", "--no-browser",
            "--cwd", str(self.workspace), "--mode", "yolo", "--workflow-mode", "disabled",
        ])
        if self.session_id:
            command.extend(["--resume", self.session_id])
        command.extend(["--prompt", prompt])
        try:
            completed = self.run_command(
                command, cwd=str(self.workspace), env=self.environment(personal),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=float(self.options.get("request_timeout_seconds", 1700)),
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip()[-2000:].replace(key, "[redacted]")
                raise RuntimeError(f"ZCode exited with code {completed.returncode}: {detail}")
            parsed = parse_output(completed.stdout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ZCode timed out; do not replay executed side effects automatically") from exc
        finally:
            personal.unlink(missing_ok=True)
        if self.session_id is not None and parsed["session_id"] != self.session_id:
            raise ValueError("ZCode changed session within a Turn")
        self.session_id = parsed["session_id"]
        return {
            "text": parsed["text"], "actions": [], "tool_calls": parsed["tool_calls"],
            "events": parsed["events"], "usage": parsed["usage"],
            "actions_are_executed": True, "tool_calls_are_executed": True,
            "raw": {"session_id": self.session_id},
        }


def parse_output(output: str) -> dict[str, Any]:
    result = None
    events = []
    calls: dict[str, dict[str, Any]] = {}
    session_ids: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("malformed ZCode stream event")
        if result is not None:
            raise ValueError("ZCode emitted events after its result")
        if event.get("sessionId"):
            session_ids.add(event["sessionId"])
        kind = event["type"]
        if kind in {"turn.failed", "provider.error", "session.error"}:
            raise RuntimeError(f"ZCode failed: {kind}")
        if kind == "result":
            result = event
            continue
        events.append(event)
        if kind == "tool.updated":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("malformed ZCode tool event")
            call_id = payload.get("toolCallId")
            if call_id and payload.get("kind") in {"scheduled", "started", "result", "error"}:
                call = calls.setdefault(call_id, {"call_id": call_id, "arguments": {}})
                call["tool"] = payload.get("toolName", payload.get("name", call.get("tool", "unknown")))
                if "input" in payload:
                    call["arguments"] = payload["input"]
                if payload.get("kind") in {"result", "error"}:
                    call["result"] = payload.get("result", payload.get("error"))
                    call["is_error"] = payload["kind"] == "error"
    if result is None or not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
        raise ValueError("ZCode stream is missing a terminal result or session")
    if session_ids != {result["sessionId"]}:
        raise ValueError("ZCode stream mixes sessions")
    if result.get("projection", {}).get("status") in {"error", "failed", "running"}:
        raise RuntimeError("ZCode did not complete its turn")
    raw = result.get("usage")
    if not isinstance(raw, dict) or "totalTokens" not in raw:
        raise ValueError("ZCode completed without provider usage")
    usage = {target: integer(raw.get(source, 0)) for source, target in {
        "inputTokens": "input_tokens", "outputTokens": "output_tokens",
        "totalTokens": "total_tokens", "cacheReadTokens": "cached_read_tokens",
        "cacheWriteTokens": "cached_write_tokens", "reasoningTokens": "reasoning_tokens",
        "modelRequestCount": "api_calls",
    }.items()}
    if usage["api_calls"] <= 0:
        raise ValueError("ZCode completed without a model request count")
    if not isinstance(result.get("response"), str):
        raise ValueError("ZCode response must be text")
    return {
        "session_id": result["sessionId"], "text": result["response"],
        "usage": usage, "events": events, "tool_calls": list(calls.values()),
    }


if __name__ == "__main__":
    raise SystemExit(serve(ZCodeBridge()))
