from __future__ import annotations

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
                return item
            return str(item.get("request", "Please try again."))
        return "Please correct the issue identified in the last feedback and try again."
