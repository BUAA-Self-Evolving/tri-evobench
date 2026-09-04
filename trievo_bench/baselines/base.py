from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


RUNTIME_PROTOCOL_VERSION = "1.0"


@dataclass(frozen=True)
class UsageDetails:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    api_calls: int = 0
    estimated_cost_usd: float | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> "UsageDetails":
        if not isinstance(value, dict):
            return cls()
        input_tokens = _non_negative_int(value.get("input_tokens"))
        output_tokens = _non_negative_int(value.get("output_tokens"))
        total_tokens = value.get("total_tokens")
        if total_tokens is None or (not int(total_tokens or 0) and input_tokens + output_tokens):
            total_tokens = input_tokens + output_tokens
        cost = value.get("estimated_cost_usd")
        try:
            estimated_cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            estimated_cost = None
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_read_tokens=_non_negative_int(value.get("cached_read_tokens", value.get("cache_read_tokens"))),
            cached_write_tokens=_non_negative_int(value.get("cached_write_tokens", value.get("cache_write_tokens"))),
            reasoning_tokens=_non_negative_int(value.get("reasoning_tokens")),
            total_tokens=_non_negative_int(total_tokens),
            api_calls=_non_negative_int(value.get("api_calls", value.get("calls"))),
            estimated_cost_usd=estimated_cost,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class FlowContext:
    flow_id: str
    workspace: Path
    output_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    workspace_mount: str | None = None


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    request: str
    max_interactions: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InteractionContext:
    turn_id: str
    interaction_index: int
    request: str
    history: list[dict[str, Any]]
    sandbox: dict[str, Any]
    remaining_token_budget: int


@dataclass
class BaselineReply:
    text: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    state_update: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    usage: UsageDetails = field(default_factory=UsageDetails)
    actions_are_executed: bool = False
    tool_calls_are_executed: bool = False
    raw: Any = None


class BaselineInteractionError(RuntimeError):
    def __init__(self, message: str, reply: BaselineReply | None = None) -> None:
        super().__init__(message)
        self.reply = reply or BaselineReply()


@dataclass(frozen=True)
class EvolutionContext:
    turn_id: str
    trajectory: list[dict[str, Any]]
    feedback: list[dict[str, Any]]
    update_enabled: bool = True
    skip_reason: str | None = None


@dataclass
class BaselineSnapshot:
    state: dict[str, Any] = field(default_factory=dict)
    digest: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "digest": self.digest, "files": self.files}


@dataclass
class EvolutionResult:
    completed: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    usage: UsageDetails = field(default_factory=UsageDetails)
    state_before: BaselineSnapshot = field(default_factory=BaselineSnapshot)
    state_after: BaselineSnapshot = field(default_factory=BaselineSnapshot)
    changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "events": self.events,
            "usage": self.usage.to_dict(),
            "state_before": self.state_before.to_dict(),
            "state_after": self.state_after.to_dict(),
            "changed_files": self.changed_files,
        }


class BaselineRuntime:
    """Framework-neutral lifecycle implemented by every evaluated baseline."""

    baseline_id = "unknown"

    def start_flow(self, context: FlowContext) -> None:
        raise NotImplementedError

    def start_turn(self, context: TurnContext) -> None:
        raise NotImplementedError

    def interact(self, context: InteractionContext) -> BaselineReply:
        raise NotImplementedError

    def evolve(self, context: EvolutionContext) -> EvolutionResult:
        raise NotImplementedError

    def snapshot_state(self) -> BaselineSnapshot:
        raise NotImplementedError

    def end_flow(self) -> None:
        pass

    def metadata(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "runtime_protocol_version": RUNTIME_PROTOCOL_VERSION,
        }
