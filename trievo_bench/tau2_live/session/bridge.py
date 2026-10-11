"""The τ² policy participant that the agent under test drives.

``BridgeAgent`` takes the place of AReaL's ``LLMAgent`` in the official
orchestrator. Each time the orchestrator hands the agent a user or tool
message, the bridge publishes it on a ``Channel`` and blocks until the agent
under test answers through the session socket with one message to the user or
one tool call, the two moves ``tau2.agent.llm_agent.AGENT_INSTRUCTION`` allows.
The orchestrator, environment, user simulator and limits are unchanged.

Like ``LLMAgent``, the bridge never ends the conversation itself; the user
simulator does. If the agent's Interaction ends while the conversation is
still open, the bridge sends ``ABANDONED`` and the service records the run as
``agent_error`` (evaluated as reward 0 by the official evaluator).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import uuid4

from tau2.agent.base_agent import HalfDuplexAgent
from tau2.agent.llm_agent import AGENT_INSTRUCTION, SYSTEM_PROMPT, LLMAgentState
from tau2.data_model.message import (
    AssistantMessage,
    MultiToolMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)

ABANDONED = "###L2_AGENT_LEFT###"


class Channel:
    """Hands each observation to the external agent and waits for its action.

    ``turn`` is ``starting`` until the first message reaches the agent,
    ``agent`` while an observation awaits an action, ``pending`` while the user
    simulator or environment responds, and ``ended`` once the run is over.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self.turn = "starting"
        self.observation: Any = None
        self._action: dict[str, Any] | None = None
        self.abandoned = False
        self.termination_reason: str | None = None
        self.error: str | None = None
        self.prior_history: list[dict[str, Any]] = []
        self.counts = {"messages_to_user": 0, "tool_calls": 0, "observations": 0}

    # Orchestrator thread -------------------------------------------------
    def exchange(self, observation: Any) -> dict[str, Any] | None:
        with self._cond:
            if self.abandoned:
                return None
            self.observation = observation
            self.counts["observations"] += 1
            self.turn = "agent"
            self._cond.notify_all()
            while self._action is None and not self.abandoned:
                self._cond.wait()
            action, self._action = self._action, None
            if action is None:
                return None
            self.turn = "pending"
            self._cond.notify_all()
            return action

    def close(self, termination_reason: str | None, error: str | None = None) -> None:
        with self._cond:
            self.turn = "ended"
            self.termination_reason = termination_reason
            self.error = error
            self._cond.notify_all()

    # Agent / control threads ---------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        state = {"turn": self.turn, "ended": self.turn == "ended"}
        if self.turn == "agent":
            state["message"] = self.observation
        if self.turn == "ended":
            state["termination_reason"] = self.termination_reason
            if self.observation is not None:
                state["last_message"] = self.observation
        return state

    def wait_settled(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.turn in {"starting", "pending"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self.snapshot()

    def act(self, action: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._cond:
            if self.turn != "agent":
                return {"error": f"it is not your turn (turn={self.turn})", **self.snapshot()}
            self._action = action
            self.counts["messages_to_user" if action["kind"] == "say" else "tool_calls"] += 1
            self.observation = None
            self.turn = "pending"
            self._cond.notify_all()
        return self.wait_settled(timeout)

    def set_last_observation(self, observation: Any) -> None:
        with self._cond:
            self.observation = observation

    def abandon(self) -> None:
        with self._cond:
            self.abandoned = True
            self._cond.notify_all()


def _message_view(message: Any, call_names: dict[str, str]) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        return {"from": "user", "content": message.content}
    if isinstance(message, ToolMessage):
        return {
            "from": "tool",
            "tool": call_names.get(message.id),
            "content": message.content,
            "error": message.error,
        }
    if isinstance(message, MultiToolMessage):
        return {"from": "tools", "results": [_message_view(m, call_names) for m in message.tool_messages]}
    if isinstance(message, AssistantMessage):
        view: dict[str, Any] = {"from": "assistant", "content": message.content}
        if message.tool_calls:
            view["tool_calls"] = [{"name": c.name, "arguments": c.arguments} for c in message.tool_calls]
        return view
    return {"from": getattr(message, "role", "unknown"), "content": getattr(message, "content", None)}


class BridgeAgent(HalfDuplexAgent[LLMAgentState]):
    """Half-duplex τ² agent whose moves come from the agent under test."""

    def __init__(self, tools, domain_policy: str, channel: Channel):
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.channel = channel
        self.call_names: dict[str, str] = {}

    @property
    def system_prompt(self) -> str:
        # The official LLMAgent system prompt (tau2.agent.llm_agent).
        return SYSTEM_PROMPT.format(domain_policy=self.domain_policy, agent_instruction=AGENT_INSTRUCTION)

    def get_init_state(self, message_history=None) -> LLMAgentState:
        history = list(message_history or [])
        self.channel.prior_history = [_message_view(m, self.call_names) for m in history]
        return LLMAgentState(
            system_messages=[SystemMessage(role="system", content=self.system_prompt)],
            messages=history,
        )

    def generate_next_message(self, message, state: LLMAgentState):
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)
        action = self.channel.exchange(_message_view(message, self.call_names))
        if action is None:
            reply = AssistantMessage(role="assistant", content=ABANDONED, raw_data={"l2_bridge": "abandoned"})
        elif action["kind"] == "say":
            reply = AssistantMessage(role="assistant", content=action["content"], raw_data={"l2_bridge": "say"})
        else:
            call_id = f"call_{uuid4().hex[:16]}"
            self.call_names[call_id] = action["name"]
            reply = AssistantMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id=call_id, name=action["name"], arguments=action["arguments"], requestor="assistant")],
                raw_data={"l2_bridge": "call"},
            )
        state.messages.append(reply)
        return reply, state

    @classmethod
    def is_stop(cls, message: AssistantMessage) -> bool:
        return message.content == ABANDONED
