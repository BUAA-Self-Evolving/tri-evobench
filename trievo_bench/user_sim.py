from __future__ import annotations

import json
from typing import Any


class UserSimulator:
    def __init__(self, turn: dict[str, Any], initial_request: str, client: Any = None) -> None:
        self.turn = turn
        self.initial_request = initial_request
        self.followups = list(turn.get("followups", []))
        self.simulator = dict(turn.get("simulator", {})) if isinstance(turn.get("simulator"), dict) else {}
        self.revealed = False
        self.index = 0
        self.client = client

    def next_request(self, *, history: list[dict[str, Any]], feedback: str) -> str:
        if self.turn.get("llm_post_failure"):
            if self.client is None:
                raise RuntimeError("LLM user simulator requires a client")
            from .verifier import L1LifeJudge
            config = self.turn["llm_post_failure"]
            prompt = L1LifeJudge._render(config["simulator_prompt"], {
                "agent_input": self.turn.get("agent_input", {}), "interaction_transcript": history,
                "latest_agent_response": next((item["content"] for item in reversed(history) if item["role"] == "assistant"), ""),
                "bounded_evaluation_feedback": json.loads(feedback),
            })
            text = self.client.complete([{"role": "system", "content": "Follow the bounded public user-simulator contract. Return JSON only."},
                                         {"role": "user", "content": prompt}], max_tokens=1024, json_mode=True)
            result = L1LifeJudge._parse(text)
            message = result.get("next_user_message")
            if not isinstance(message, str) or not message.strip():
                raise RuntimeError("invalid L1 user-simulator output")
            return message
        if self.simulator.get("policy") == "reveal_only_after_relevant_clarification" and not self.revealed:
            if self._has_clarification(history):
                self.revealed = True
                reply = self.simulator.get("scripted_reply")
                if isinstance(reply, str) and reply.strip():
                    return reply
            # The simulator must not volunteer a hidden fact when the agent did
            # not ask for it.  The ordinary evaluator feedback can still drive
            # a retry, but it may not disclose benchmark truth.
            return self._render(
                "Please address the observable issue in the last feedback and try again: {feedback}",
                history,
                feedback,
            )
        if self.index < len(self.followups):
            item = self.followups[self.index]
            self.index += 1
            if isinstance(item, str):
                template = item
            else:
                template = str(item.get("request", "Please try again."))
            return self._render(template, history, feedback)
        return self._render(
            "Please correct the issue identified in the last feedback and try again: {feedback}",
            history,
            feedback,
        )

    @staticmethod
    def _has_clarification(history: list[dict[str, Any]]) -> bool:
        """Recognize an actual question without exposing simulator truth.

        L1-Life's interactive arm releases its scripted fact only after an
        agent asks.  A question mark is deliberately a conservative and
        language-neutral signal; answer-like responses cannot trigger it.
        """
        for item in reversed(history):
            if item.get("role") == "assistant":
                content = str(item.get("content", ""))
                return "?" in content or "？" in content
        return False

    def _render(self, template: str, history: list[dict[str, Any]], feedback: str) -> str:
        """Render a deterministic follow-up from the current Turn context.

        Static followups remain valid. Data authors can opt into grounded
        requests with ``{feedback}``, ``{history}``, or ``{turn_id}`` without
        changing the User Simulator interface.
        """
        values = {
            "feedback": feedback,
            "history": json.dumps(history, ensure_ascii=False),
            "turn_id": str(self.turn.get("id", "")),
        }
        try:
            return template.format(**values)
        except (KeyError, ValueError):
            return template
