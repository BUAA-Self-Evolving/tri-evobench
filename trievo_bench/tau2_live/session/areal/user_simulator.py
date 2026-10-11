"""Record failed user-simulator calls before the episode attempt fails."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from tau2.data_model.message import UserMessage
from tau2.user.user_simulator import UserSimulator
from tau2.user.user_simulator_base import UserState, ValidUserInputMessage
from tau2.utils.llm_utils import to_litellm_messages

import logging

logger = logging.getLogger("Tau2Agent")


class EmptyUserResponseError(ValueError):
    """The simulator returned neither text nor tool calls."""


class RecordingUserSimulator(UserSimulator):
    """Official user simulator that keeps evidence of failed calls.

    Failures are not retried here. As in `tau2 run`, the exception ends the
    attempt and the episode-level retry replays the whole episode.
    """

    def __init__(self, *, debug_dir: Path, task_id: str, domain: str, **kwargs):
        super().__init__(**kwargs)
        self.debug_dir = debug_dir
        self.task_id = task_id
        self.domain = domain

    def _generate_next_message(
        self, message: ValidUserInputMessage, state: UserState
    ) -> UserMessage:
        try:
            response = super()._generate_next_message(message, state)
        except Exception as exc:
            self._record(state, error=exc)
            raise
        if not (response.has_content() or response.is_tool_call()):
            path = self._record(state, response=response)
            # Official `UserMessage.validate()` rejects the same message one step
            # later; failing here keeps the raw provider response with the error.
            raise EmptyUserResponseError(
                f"Empty user response for {self.domain}/{self.task_id}; "
                f"evidence: {path}"
            )
        return response

    def _record(
        self,
        state: UserState,
        *,
        response: UserMessage | None = None,
        error: Exception | None = None,
    ) -> Path:
        path = self.debug_dir / f"{uuid4().hex}.json"
        try:
            # Keep full conversation and raw response, including reasoning and IDs.
            # Authentication is transport configuration, not part of the request body.
            args = {
                k: v
                for k, v in self.llm_args.items()
                if k not in {"api_key", "extra_headers", "headers"}
            }
            record = {
                "recorded_at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "domain": self.domain,
                "task_id": self.task_id,
                "request": {
                    "model": self.llm,
                    "messages": to_litellm_messages(
                        state.system_messages + state.flip_roles()
                    ),
                    "tools": [tool.openai_schema for tool in self.tools]
                    if self.tools
                    else None,
                    "tool_choice": "auto" if self.tools else None,
                    "args": args,
                },
                "response": response.raw_data if response is not None else None,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                    "body": getattr(error, "body", None),
                    **(
                        {"doc": error.doc, "pos": error.pos}
                        if isinstance(error, json.JSONDecodeError)
                        else {}
                    ),
                }
                if error
                else None,
            }
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2, default=str)
        except Exception:
            logger.warning(
                "Could not write user-simulator incident %s", path, exc_info=True
            )
        return path
