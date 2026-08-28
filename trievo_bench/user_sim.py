from __future__ import annotations

import json
from typing import Any


class UserSimulator:
    def __init__(self, turn: dict[str, Any], initial_request: str) -> None:
        self.turn = turn
        self.initial_request = initial_request
        self.followups = list(turn.get("followups", []))
        self.simulator = dict(turn.get("simulator", {})) if isinstance(turn.get("simulator"), dict) else {}
        self.revealed = False
        self.index = 0

    def next_request(self, *, history: list[dict[str, Any]], feedback: str) -> str:
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
