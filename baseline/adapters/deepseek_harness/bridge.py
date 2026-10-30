from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace
from typing import Any, Callable

from baseline.adapters.native_runtime import NativeBridge, integer, serve
from baseline.adapters.deepseek_harness.friday_messages import FridayMessagesRelay


class DeepSeekHarnessBridge(NativeBridge):
    baseline_id = "deepseek-harness"
    integration = "python_sdk"

    def __init__(self, harness_factory: Callable[..., Any] | None = None) -> None:
        super().__init__()
        self.harness_factory = harness_factory
        self.harness: Any = None
        self.relay: FridayMessagesRelay | None = None
        self.last_provider_request = 0.0
        self.turn_limits: dict[str, Any] = {}
        self.turn_timeout_seconds = 600.0

    @property
    def opencode_profile(self) -> bool:
        return self.options.get("execution_profile") == "opencode-full-stream-20260928"

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        result = super().initialize(params)
        if self.opencode_profile:
            result.update(session_policy="one_per_flow",
                          execution_profile="opencode-full-stream-20260928",
                          provider_request_interval_seconds=0,
                          model_call_limit_enforced_in_bridge=False)
        return result

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        result = super().start_flow(params)
        if self.opencode_profile:
            saved = self.state_dir / "session.json"
            if saved.exists():
                self.session_id = json.loads(saved.read_text())["session_id"]
            result.update(session_policy="one_per_flow", resumed_session=bool(self.session_id))
        return result

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.opencode_profile:
            if self.state_dir is None:
                raise RuntimeError("flow has not started")
            if not self.evolved:
                raise RuntimeError("previous turn has not crossed the evolution barrier")
            self.home = self.state_dir / "sessions"
            self.home.mkdir(mode=0o700, exist_ok=True)
            self.turn_id = str(params["turn_id"])
            self.evolved = False
            result = {"turn_id": self.turn_id, "fresh_session": not bool(self.session_id)}
        else:
            result = super().start_turn(params)
        from trievo_bench.task_terminal import SCHEMA, socket_from_metadata
        self._task_terminal_socket = socket_from_metadata(params)
        if self._task_terminal_socket:
            result["task_terminal_transport"] = SCHEMA
        self.turn_limits = dict(params.get("metadata", {}).get("agent_limits", self.options.get("l2_agent_limits", {})))
        # OpenCode's subprocess allowance is 900s, but the benchmark RPC
        # allowance is 660s in L1 and 900s in L2. Match that effective boundary;
        # the host transport's existing DSH grace is teardown time only.
        self.turn_timeout_seconds = (
            min(float(self.options.get("request_timeout_seconds", 900)),
                float(params.get("metadata", {}).get("interaction_timeout_seconds", 900)))
            if self.opencode_profile else float(self.turn_limits.get("episode_timeout_seconds", 600))
        )
        return result

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        if getattr(self, '_task_terminal_socket', None):
            (self.state_dir / 'foreground-budget.json').unlink(missing_ok=True)
        if not self.opencode_profile:
            return super().evolve(params)
        self.require_turn(params)
        self.evolved = True
        return {"completed": True, "usage": {}, "events": [
            {"type": "evolution.skipped", "reason": "native_learner_unsupported_session_retained"}]}

    def end_flow(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.opencode_profile:
            return super().end_flow(params)
        self.close_session()
        # Keep the native transcript in the final/checkpoint archive, as
        # OpenCode retains its session database. A reset gets a new state root.
        self.turn_id = None
        return {"completed": True, "session_id": self.session_id}

    def close_session(self) -> None:
        try:
            if self.harness is not None:
                self.harness.close()
        finally:
            self.harness = None
            if self.relay is not None:
                self.last_provider_request = self.relay.last_request
                self.relay.close()
                self.relay = None

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt = self.prompt(params)
        v5 = bool(getattr(self, "_task_terminal_socket", None))
        if v5:
            from baseline.adapters.native_budget import begin
            begin(self.state_dir / 'foreground-budget.json', self.turn_limits)
        if self.harness is None:
            factory = self.harness_factory
            if factory is None:
                from deepseek_harness import DeepSeekHarness
                factory = DeepSeekHarness
            key_env = str(self.options.get("api_key_env", "TRIEVO_API_KEY"))
            base_url = self.options.get("base_url") or os.getenv("TRIEVO_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL")
            model = self.options.get("model") or os.getenv("TRIEVO_MODEL") or "deepseek-v4-flash"
            key = os.getenv(key_env) or os.getenv("DEEPSEEK_API_KEY")
            if self.options.get("provider_transport") == "friday_chat":
                output_cap = min(int(self.options.get("max_output_tokens", 4096)),
                                 int(self.turn_limits.get("max_output_tokens", 4096)))
                relay_options = ({"min_interval": 0, "max_calls": None}
                                 if self.opencode_profile else
                                 {"max_calls": int(self.turn_limits.get("max_model_calls", 12))})
                self.relay = FridayMessagesRelay(
                    base_url, key, model, output_cap,
                    timeout=float(self.options.get("upstream_timeout_seconds", 300)),
                    **relay_options)
                self.relay.last_request = self.last_provider_request
                base_url = self.relay.start()
                # The native SDK only sees a non-secret local transport marker.
                key = "local-friday-transport"
            native_env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config")}
            patches = list(self.options.get("patches", []))
            if getattr(self, "_task_terminal_socket", None):
                patches.append("/opt/trievo/baseline/adapters/deepseek_harness/task_terminal.patch.yml")
                # Native boot config is Flow-scoped. Dynamic per-Turn tool
                # deadlines are applied by the native execution hook.
                native_env["TRIEVO_TASK_TERMINAL_TIMEOUT_MS"] = '3600000'
                native_env['TRIEVO_NATIVE_BUDGET'] = str(self.state_dir / 'foreground-budget.json')
            if self.opencode_profile:
                native_env["DSH_CONTEXT_WINDOW"] = str(self.options.get("context_window", 128000))
            self.harness = factory(
                cwd=str(self.workspace), dsh_home=str(self.home / "dsh"),
                dsh_bin=self.options.get("executable", "dsh"), profile="sdk-minimal",
                provider="deepseek-official",
                model=model,
                reasoning_effort=self.options.get("reasoning_effort"),
                max_tokens=self.options.get("max_output_tokens"),
                request_timeout_seconds=float(self.options.get("request_timeout_seconds", 1700)),
                base_url=base_url,
                api_key=key,
                env=native_env,
                patches=tuple(patches),
            )
        elif self.relay is not None and not self.opencode_profile:
            self.relay.max_calls = self.relay.calls + int(self.turn_limits.get("max_model_calls", 12))
        if v5 and self.relay is not None:
            self.relay.output_cap = min(int(self.options.get('max_output_tokens', 4096)),
                                       int(self.turn_limits['max_output_tokens']))
        result = self._run_bounded(prompt)
        events = getattr(result, "events", [])
        budget = {}
        if v5:
            from baseline.adapters.native_budget import receipt
            budget = receipt(self.state_dir / 'foreground-budget.json')
        exhausted = bool(budget.get('limit_reasons'))
        # A native token-limit result is a durable, paid attempt, not a
        # transport failure. Preserve its reply, tools and actual usage so the
        # benchmark scores this attempt without resending it.
        token_limit = result.finish_reason == "max-tokens" and any(
            event.get("type") == "turn/end"
            and event.get("data", {}).get("reason", {}).get("kind") == "max-tokens"
            for event in events
        )
        deadline = result.finish_reason == "adapter-deadline"
        if result.finish_reason != "completed" and not token_limit and not deadline and not exhausted:
            reason = next((event.get("data", {}).get("reason", {})
                           for event in reversed(getattr(result, "events", []))
                           if event.get("type") == "turn/end"), {})
            detail = reason.get("error", {}).get("message", "")
            raise RuntimeError(f"DeepSeek Harness turn failed: {result.finish_reason}: {detail}")
        if not isinstance(result.session_id, str) or not result.session_id:
            raise ValueError("DeepSeek Harness result is missing session id")
        if self.session_id is not None and result.session_id != self.session_id:
            raise ValueError("DeepSeek Harness changed session within a Turn")
        parsed = parse_events(result.events, require_usage=not deadline)
        self.session_id = result.session_id
        if self.opencode_profile:
            (self.state_dir / "session.json").write_text(json.dumps({"session_id": self.session_id}))
        return {
            "text": result.final_response, "actions": [], "tool_calls": parsed["tool_calls"],
            "events": result.events, "usage": parsed["usage"],
            "actions_are_executed": True, "tool_calls_are_executed": True,
            "raw": {"session_id": self.session_id, "native_finish_reason": result.finish_reason,
                    **({"native_budget": budget, "limit_exhausted": exhausted,
                        "limit_reasons": budget['limit_reasons']} if v5 else {}),
                    **({"limit_exhausted": True, "limit_reasons": ["max_output_tokens"]}
                       if token_limit and not self.opencode_profile else {}),
                    **({"execution_error": {"type": "adapter_deadline", "provider_requests_replayed": 0},
                        "usage_status": "observed_prefix_only; in-flight usage may be unavailable"}
                       if deadline else {})},
        }

    def _run_bounded(self, prompt: str):
        # request_timeout_seconds bounds RPC responses, but the pinned SDK's
        # NotificationSubscription.next() waits on an unbounded queue. Capture
        # notifications while waiting and close this runtime at the advertised
        # episode boundary. Never label this as a proven execution_limit.
        if self.harness_factory is not None:
            return self.harness.run(prompt, session_id=self.session_id)
        seconds = (self.turn_timeout_seconds if self.opencode_profile else
                   float(self.turn_limits.get("episode_timeout_seconds", 600)))
        session = self.harness.start_session(self.session_id)
        observed, values, errors = [], [], []
        def collect(notification):
            payload = notification.payload
            if notification.method == "session.event" and payload.get("sessionId") == session.id:
                event = payload.get("event")
                if isinstance(event, dict):
                    observed.append(event)
        def run():
            try:
                values.append(session.run(prompt, on_notification=collect))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=seconds)
        if not thread.is_alive():
            if errors:
                raise errors[0]
            return values[0]
        if self.relay is not None:
            self.relay.accepting = False
        self.harness.close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("native runtime could not be stopped safely; no replay permitted")
        if self.opencode_profile:
            self.close_session()
        from deepseek_harness.api import final_response
        return SimpleNamespace(session_id=session.id, final_response=final_response(observed),
                               finish_reason="adapter-deadline", events=list(observed))


def parse_events(events: list[dict[str, Any]], *, require_usage: bool = True) -> dict[str, Any]:
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
    if require_usage and usage["api_calls"] == 0:
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
