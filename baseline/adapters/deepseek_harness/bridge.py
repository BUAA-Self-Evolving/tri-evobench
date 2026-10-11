from __future__ import annotations

import json
import os
from typing import Any, Callable

from baseline.adapters.native_runtime import NativeBridge, integer, serve


class DeepSeekHarnessBridge(NativeBridge):
    baseline_id = "deepseek-harness"
    integration = "python_sdk"

    def __init__(self, harness_factory: Callable[..., Any] | None = None) -> None:
        super().__init__()
        self.harness_factory = harness_factory
        self.harness: Any = None

    def close_session(self) -> None:
        if self.harness is not None:
            self.harness.close()
            self.harness = None

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt = self.prompt(params)
        if self.harness is None:
            factory = self.harness_factory
            if factory is None:
                from deepseek_harness import DeepSeekHarness
                factory = DeepSeekHarness
            key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
            self.harness = factory(
                cwd=str(self.workspace), dsh_home=str(self.home / "dsh"),
                dsh_bin=self.options.get("executable", "dsh"), profile="sdk-minimal",
                provider="deepseek-official",
                model=self.options.get("model") or os.getenv("TRIEVO_MODEL") or "deepseek-v4-flash",
                reasoning_effort=self.options.get("reasoning_effort"),
                max_tokens=self.options.get("max_output_tokens"),
                request_timeout_seconds=float(self.options.get("request_timeout_seconds", 1700)),
                base_url=self.options.get("base_url") or os.getenv("TRIEVO_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL"),
                api_key=os.getenv(key_env) or os.getenv("DEEPSEEK_API_KEY"),
                env={"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config")},
            )
        result = self.harness.run(prompt, session_id=self.session_id)
        if result.finish_reason != "completed":
            reason = next((event.get("data", {}).get("reason", {})
                           for event in reversed(getattr(result, "events", []))
                           if event.get("type") == "turn/end"), {})
            detail = reason.get("error", {}).get("message", "")
            raise RuntimeError(f"DeepSeek Harness turn failed: {result.finish_reason}: {detail}")
        if not isinstance(result.session_id, str) or not result.session_id:
            raise ValueError("DeepSeek Harness result is missing session id")
        if self.session_id is not None and result.session_id != self.session_id:
            raise ValueError("DeepSeek Harness changed session within a Turn")
        parsed = parse_events(result.events)
        self.session_id = result.session_id
        return {
            "text": result.final_response, "actions": [], "tool_calls": parsed["tool_calls"],
            "events": result.events, "usage": parsed["usage"],
            "actions_are_executed": True, "tool_calls_are_executed": True,
            "raw": {"session_id": self.session_id},
        }


def parse_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {name: 0 for name in (
        "input_tokens", "output_tokens", "total_tokens", "cached_read_tokens",
        "cached_write_tokens", "reasoning_tokens", "api_calls",
    )}
    calls: dict[str, dict[str, Any]] = {}
    seen: set[Any] = set()
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise ValueError("malformed DeepSeek Harness session event")
        sequence = event.get("seq")
        if sequence is not None:
            if sequence in seen:
                continue
            seen.add(sequence)
        data = event["data"]
        kind = event.get("type")
        if kind in {"assistant/message", "assistant/attempt"}:
            raw = data.get("usage")
            if raw is None:
                records = [row["chunk"].get("usage") for row in data.get("stream", [])
                           if isinstance(row, dict) and isinstance(row.get("chunk"), dict)
                           and row["chunk"].get("type") == "usage"]
                raw = records[-1] if records else None
            if raw is None:
                continue
            counts = {key: integer(raw.get(key, 0)) for key in (
                "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens",
            )}
            usage["input_tokens"] += counts["inputTokens"] + counts["cacheReadTokens"] + counts["cacheWriteTokens"]
            usage["output_tokens"] += counts["outputTokens"]
            usage["total_tokens"] += integer(raw.get("totalTokens", counts["inputTokens"] + counts["outputTokens"] + counts["cacheReadTokens"] + counts["cacheWriteTokens"]))
            usage["cached_read_tokens"] += counts["cacheReadTokens"]
            usage["cached_write_tokens"] += counts["cacheWriteTokens"]
            usage["reasoning_tokens"] += counts["reasoningTokens"]
            usage["api_calls"] += 1
        elif kind == "tool/call":
            arguments = data.get("arguments", "{}")
            calls[str(data["callId"])] = {
                "tool": str(data["name"]),
                "arguments": json_arguments(arguments), "call_id": str(data["callId"]),
            }
        elif kind == "tool/result":
            message = data["message"]
            call = calls.get(str(message["toolCallId"]))
            if call is not None:
                call["result"] = message.get("content")
                call["is_error"] = bool(message.get("isError", False))
    if usage["api_calls"] == 0:
        raise ValueError("DeepSeek Harness completed without model usage")
    return {"usage": usage, "tool_calls": list(calls.values())}


def json_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {"raw_arguments": arguments}


if __name__ == "__main__":
    raise SystemExit(serve(DeepSeekHarnessBridge()))
