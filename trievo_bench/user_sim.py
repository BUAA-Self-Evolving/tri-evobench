from __future__ import annotations

import json
from typing import Any


class UserSimulator:
    def __init__(self, turn: dict[str, Any], initial_request: str) -> None:
        self.turn = turn
        self.initial_request = initial_request
        self.followups = list(turn.get("followups", []))
        self.index = 0

    def next_request(self, *, history: list[dict[str, Any]], feedback: str) -> str:
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
