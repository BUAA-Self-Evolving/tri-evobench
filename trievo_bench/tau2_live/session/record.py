"""The sealed record of one live τ² session (``trievo_tau2_live_session/1.0``).

The service writes it after the official evaluation; the pool builder writes
fixtures in the same shape. It holds no credentials. The L2 runtime passes it
to the Turn's native verifier as the inline ``interaction_record`` submission.
"""

from __future__ import annotations

from typing import Any

from .areal.runner import EpisodeResult, Tau2EnvConfig, _litellm_user_model, policy_turns, user_llm_usage

RECORD_SCHEMA = "trievo_tau2_live_session/1.0"


def harness_description(econfig: Tau2EnvConfig, timeout: float) -> dict[str, Any]:
    user_args = {k: v for k, v in (econfig.user_llm_args or {}).items() if k != "api_key"}
    user_args.setdefault("top_p", 1.0)
    return {
        "source": "AReaL examples/tau2 Tau2Runner (evaluation path), commit 979f4221",
        "orchestrator": "tau2.orchestrator.orchestrator.Orchestrator",
        "max_steps": econfig.max_steps,
        "max_errors": 10,
        "solo_mode": econfig.solo_mode,
        "timeout_seconds": float(timeout),
        "agent": "BridgeAgent: official LLMAgent system prompt; the agent under test chooses each move",
        "user_simulator": {
            "class": "RecordingUserSimulator",
            "llm": _litellm_user_model(econfig.user_llm),
            "api_base": econfig.user_llm_base_url,
            "llm_args": user_args,
            "num_retries": econfig.user_llm_num_retries,
            "request_timeout_seconds": min(float(timeout), 120.0),
        },
        "nl_assertion_judge": {"llm": _litellm_user_model(econfig.user_llm), "bound_to": "user_simulator"},
    }


def usage_counts(messages: list[Any]) -> dict[str, int]:
    return {
        **user_llm_usage(messages),
        "policy_turns": policy_turns(messages),
        "messages": len(messages),
        "user_messages": sum(getattr(m, "role", None) == "user" for m in messages),
        "assistant_tool_calls": sum(
            len(m.tool_calls or []) for m in messages if getattr(m, "role", None) == "assistant"
        ),
        "user_tool_calls": sum(len(m.tool_calls or []) for m in messages if getattr(m, "role", None) == "user"),
    }


def build_record(
    *,
    econfig: Tau2EnvConfig,
    task_id: str,
    task_sha256: str,
    tau2_revision: str,
    timeout: float,
    result: EpisodeResult | None,
    error: str | None,
    partial_messages: list[Any],
    abandoned: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA,
        "domain": econfig.domain,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "tau2_revision": tau2_revision,
        "benchmark_variant": econfig.benchmark_variant,
        "harness": harness_description(econfig, timeout),
        "abandoned": abandoned,
        **(extra or {}),
    }
    if result is not None:
        messages = result.simulation.messages
        record.update(
            status="completed",
            error=None,
            termination_reason=result.termination_reason,
            reward=float(result.reward_info.reward),
            reward_info=result.reward_info.model_dump(mode="json"),
            nl_reward_info=result.nl_reward_info.model_dump(mode="json") if result.nl_reward_info else None,
            simulation=result.simulation.model_dump(mode="json"),
        )
    else:
        messages = list(partial_messages)
        record.update(
            status="error",
            error=error or "the τ² session did not finish",
            termination_reason=None,
            reward=None,
            reward_info=None,
            nl_reward_info=None,
            simulation=None,
            partial_messages=[m.model_dump(mode="json") for m in messages],
        )
    record["usage"] = usage_counts(messages)
    return record
