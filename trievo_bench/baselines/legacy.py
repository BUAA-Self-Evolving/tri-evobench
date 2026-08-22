from __future__ import annotations

from typing import Any

from ..agent import Agent
from ..learner import Learner
from ..spec import TurnSpec
from .base import (
    BaselineReply,
    BaselineRuntime,
    BaselineSnapshot,
    EvolutionContext,
    EvolutionResult,
    FlowContext,
    InteractionContext,
    TurnContext,
)


class LegacyActionRuntime(BaselineRuntime):
    """Compatibility wrapper for the original respond-and-actions baselines."""

    def __init__(self, baseline_id: str, agent: Agent, learner: Learner, turns: list[TurnSpec]) -> None:
        self.baseline_id = baseline_id
        self.agent = agent
        self.learner = learner
        self._turns = {turn.id: turn for turn in turns}
        self._state: dict[str, Any] = {}
        self._current_turn: str | None = None

    def start_flow(self, context: FlowContext) -> None:
        self._state = {}
        self._current_turn = None

    def start_turn(self, context: TurnContext) -> None:
        self._current_turn = context.turn_id
        self._state = self.learner.before_turn(self._state)

    def interact(self, context: InteractionContext) -> BaselineReply:
        reply = self.agent.respond(
            turn_id=context.turn_id,
            interaction_index=context.interaction_index,
            request=context.request,
            history=context.history,
            state=self._state,
            sandbox=context.sandbox,
        )
        self._state = _merge_state(self._state, reply.state_update)
        return BaselineReply(
            text=reply.text,
            actions=reply.actions,
            state_update=reply.state_update,
            tool_calls=reply.tool_calls,
            events=reply.events,
            raw=reply.raw,
        )

    def evolve(self, context: EvolutionContext) -> EvolutionResult:
        before = self.snapshot_state()
        turn = self._turns.get(context.turn_id)
        if turn is None:
            raise RuntimeError(f"unknown legacy turn: {context.turn_id}")
        self._state = self.learner.after_turn(self._state, context.trajectory, turn)
        after = self.snapshot_state()
        changed = sorted(key for key in set(before.state) | set(after.state) if before.state.get(key) != after.state.get(key))
        return EvolutionResult(state_before=before, state_after=after, changed_files=changed)

    def snapshot_state(self) -> BaselineSnapshot:
        return BaselineSnapshot(state=dict(self._state))

    def metadata(self) -> dict[str, Any]:
        return {
            **super().metadata(),
            "adapter": "legacy_action",
            "learner": self.learner.__class__.__name__,
            "workspace_mode": "runner_actions",
        }


def _merge_state(base: dict[str, Any], update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        return dict(base)
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_state(merged[key], value)
        else:
            merged[key] = value
    return merged
