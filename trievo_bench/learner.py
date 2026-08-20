from __future__ import annotations

from typing import Any


class Learner:
    def before_turn(self, state: dict[str, Any]) -> dict[str, Any]:
        return dict(state)

    def after_turn(self, state: dict[str, Any], trajectory: list[dict[str, Any]], turn: Any) -> dict[str, Any]:
        return dict(state)


class FrozenLearner(Learner):
    pass


class EvolvingLearner(Learner):
    def after_turn(self, state: dict[str, Any], trajectory: list[dict[str, Any]], turn: Any) -> dict[str, Any]:
        updated = dict(state)
        for interaction in trajectory:
            update = interaction.get("state_update", {})
            if isinstance(update, dict):
                updated = _merge(updated, update)
        return updated


def _merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class RawReplayLearner(Learner):
    def after_turn(self, state: dict[str, Any], trajectory: list[dict[str, Any]], turn: Any) -> dict[str, Any]:
        updated = dict(state)
        replay = list(updated.get("raw_replay", []))
        replay.append({"turn_id": turn.id, "trajectory": trajectory})
        updated["raw_replay"] = replay
        return updated


class OracleLearner(Learner):
    def after_turn(self, state: dict[str, Any], trajectory: list[dict[str, Any]], turn: Any) -> dict[str, Any]:
        updated = dict(state)
        if turn.oracle_update:
            updated.update(turn.oracle_update)
        return updated


def learner_from_mode(mode: str) -> Learner:
    if mode in {"reset", "frozen"}:
        return FrozenLearner()
    if mode == "evolving":
        return EvolvingLearner()
    if mode == "raw-replay":
        return RawReplayLearner()
    if mode == "oracle-update":
        return OracleLearner()
    raise ValueError(f"unsupported learner mode: {mode}")
