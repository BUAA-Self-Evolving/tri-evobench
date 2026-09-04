from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunConditionError(ValueError):
    """Raised when a run condition is not part of the shared benchmark contract."""


class RunCondition(str, Enum):
    """Reference run conditions shared by L1/L2 callers.

    The enum describes policy only.  Runner-specific code remains responsible
    for copying workspaces, applying transitions, and enabling or blocking any
    learner/SUT update path.
    """

    RESET = "reset"
    FROZEN = "frozen"
    EVOLVING = "evolving"

    @property
    def policy(self) -> "RunConditionPolicy":
        return _POLICIES[self]


@dataclass(frozen=True)
class RunConditionPolicy:
    condition: RunCondition
    workspace_persists_across_turns: bool
    reseed_workspace_before_each_turn: bool
    apply_only_current_turn_transition: bool
    sut_state_persists_across_turns: bool
    recreate_sut_state_before_each_turn: bool
    sut_evolution_enabled: bool

    @property
    def learner_updates_enabled(self) -> bool:
        """Alias for callers that model the SUT update path as a learner."""

        return self.sut_evolution_enabled

    @property
    def harness_updates_enabled(self) -> bool:
        """Alias for persistent prompt, skill, memory, or harness state updates."""

        return self.sut_evolution_enabled


DEFAULT_RUN_CONDITION = RunCondition.FROZEN
VALID_RUN_CONDITIONS = tuple(condition.value for condition in RunCondition)

_POLICIES: dict[RunCondition, RunConditionPolicy] = {
    RunCondition.RESET: RunConditionPolicy(
        condition=RunCondition.RESET,
        workspace_persists_across_turns=False,
        reseed_workspace_before_each_turn=True,
        apply_only_current_turn_transition=True,
        sut_state_persists_across_turns=False,
        recreate_sut_state_before_each_turn=True,
        sut_evolution_enabled=False,
    ),
    RunCondition.FROZEN: RunConditionPolicy(
        condition=RunCondition.FROZEN,
        workspace_persists_across_turns=True,
        reseed_workspace_before_each_turn=False,
        apply_only_current_turn_transition=False,
        sut_state_persists_across_turns=True,
        recreate_sut_state_before_each_turn=False,
        sut_evolution_enabled=False,
    ),
    RunCondition.EVOLVING: RunConditionPolicy(
        condition=RunCondition.EVOLVING,
        workspace_persists_across_turns=True,
        reseed_workspace_before_each_turn=False,
        apply_only_current_turn_transition=False,
        sut_state_persists_across_turns=True,
        recreate_sut_state_before_each_turn=False,
        sut_evolution_enabled=True,
    ),
}


def parse_run_condition(
    value: str | RunCondition | None,
    *,
    default: RunCondition | str | None = DEFAULT_RUN_CONDITION,
) -> RunCondition:
    """Return a normalized run condition.

    ``None`` maps to the explicit default to preserve current runner behavior:
    legacy invocations keep a persistent workspace while learner/SUT updates are
    blocked.  Pass ``default=None`` when a caller wants to require an explicit
    condition.
    """

    if isinstance(default, str):
        default = _parse_text(default)
    if value is None:
        if default is None:
            raise RunConditionError("run condition is required")
        return default
    if isinstance(value, RunCondition):
        return value
    if isinstance(value, str):
        return _parse_text(value)
    raise RunConditionError(
        f"run condition must be one of {VALID_RUN_CONDITIONS}, got {type(value).__name__}"
    )


def require_run_condition(value: str | RunCondition | None) -> RunCondition:
    """Parse a condition, rejecting missing values instead of using the default."""

    return parse_run_condition(value, default=None)


def run_condition_policy(value: str | RunCondition | None) -> RunConditionPolicy:
    """Parse a condition and return the externally visible execution policy."""

    return parse_run_condition(value).policy


def _parse_text(value: str) -> RunCondition:
    normalized = value.strip().lower()
    try:
        return RunCondition(normalized)
    except ValueError as exc:
        raise RunConditionError(
            f"unknown run condition {value!r}; expected one of {VALID_RUN_CONDITIONS}"
        ) from exc
