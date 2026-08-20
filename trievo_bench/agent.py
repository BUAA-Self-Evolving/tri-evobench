from __future__ import annotations

import json
from typing import Any, Protocol

from .model import ModelReply, OpenAICompatibleClient, parse_reply


class Agent(Protocol):
    def respond(self, *, turn_id: str, interaction_index: int, request: str, history: list[dict[str, Any]], state: dict[str, Any], sandbox: dict[str, Any]) -> ModelReply:
        ...


class ScriptedAgent:
    """Deterministic adapter used to validate the runtime without model tokens."""

    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        self.policy = policy or {}

    def respond(self, *, turn_id: str, interaction_index: int, request: str, history: list[dict[str, Any]], state: dict[str, Any], sandbox: dict[str, Any]) -> ModelReply:
        turn_policy = self.policy.get(turn_id, {})
        item = turn_policy.get(str(interaction_index), turn_policy.get("default", {"response": "Acknowledged."}))
        if isinstance(item, str):
            return parse_reply(item)
        return parse_reply(json.dumps(item, ensure_ascii=False))


class OpenAICompatibleAgent:
    def __init__(self, client: OpenAICompatibleClient, system_prompt: str | None = None, response_format: str = "json", max_output_tokens: int = 1024) -> None:
        self.client = client
        self.response_format = response_format
        self.max_output_tokens = max_output_tokens
        self.system_prompt = system_prompt or (
            "You are the task agent in a benchmark. Return JSON only with keys: "
            "response (string), actions (array of {op:'write_file',path,content} or {op:'remove',path}), "
            "and optional state_update (object). Do not claim an action was done unless you include the action."
        )

    def respond(self, *, turn_id: str, interaction_index: int, request: str, history: list[dict[str, Any]], state: dict[str, Any], sandbox: dict[str, Any]) -> ModelReply:
        prompt = {
            "turn_id": turn_id,
            "interaction_index": interaction_index,
            "request": request,
            "conversation": history,
            "persistent_state": state,
            "sandbox_snapshot": sandbox,
        }
        text = self.client.complete([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ], max_tokens=self.max_output_tokens, json_mode=self.response_format != "text")
        if self.response_format == "text":
            return ModelReply(text=text, actions=[], state_update={}, raw=text)
        return parse_reply(text)
