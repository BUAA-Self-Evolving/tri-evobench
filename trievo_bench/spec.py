from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SpecError(ValueError):
    """Raised when a benchmark specification is invalid."""


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise SpecError(f"{where} is missing required field '{key}'")
    return mapping[key]


@dataclass(frozen=True)
class TurnSpec:
    id: str
    request: str
    max_interactions: int = 1
    success_threshold: float = 1.0
    setup: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    oracle_update: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "TurnSpec":
        where = f"turn[{index}]"
        turn_id = str(_require(raw, "id", where))
        request = str(_require(raw, "request", where))
        max_interactions = int(raw.get("max_interactions", 1))
        if max_interactions < 1:
            raise SpecError(f"{where}.max_interactions must be >= 1")
        threshold = float(raw.get("success_threshold", 1.0))
        if not 0 <= threshold <= 1:
            raise SpecError(f"{where}.success_threshold must be in [0, 1]")
        verifier = raw.get("verifier", {})
        if not isinstance(verifier, dict) or "type" not in verifier:
            raise SpecError(f"{where}.verifier must be an object with a type")
        return cls(
            id=turn_id,
            request=request,
            max_interactions=max_interactions,
            success_threshold=threshold,
            setup=dict(raw.get("setup", {})),
            user=dict(raw.get("user", {})),
            verifier=verifier,
            metadata=dict(raw.get("metadata", {})),
            oracle_update=dict(raw["oracle_update"]) if raw.get("oracle_update") else None,
        )


@dataclass(frozen=True)
class FlowSpec:
    id: str
    sandbox: dict[str, Any]
    turns: list[TurnSpec]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "FlowSpec":
        where = f"flow[{index}]"
        flow_id = str(_require(raw, "id", where))
        turns_raw = _require(raw, "turns", where)
        if not isinstance(turns_raw, list) or not turns_raw:
            raise SpecError(f"{where}.turns must be a non-empty list")
        return cls(
            id=flow_id,
            sandbox=dict(raw.get("sandbox", {})),
            turns=[TurnSpec.from_dict(item, i) for i, item in enumerate(turns_raw)],
            metadata=dict(raw.get("metadata", {})),
        )


@dataclass(frozen=True)
class BenchmarkSpec:
    schema_version: str
    benchmark: dict[str, Any]
    flows: list[FlowSpec]
    agent: dict[str, Any] = field(default_factory=dict)
    judge: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BenchmarkSpec":
        version = str(_require(raw, "schema_version", "root"))
        if version != "0.1":
            raise SpecError(f"unsupported schema_version {version!r}; expected '0.1'")
        flows_raw = _require(raw, "flows", "root")
        if not isinstance(flows_raw, list) or not flows_raw:
            raise SpecError("root.flows must be a non-empty list")
        return cls(
            schema_version=version,
            benchmark=dict(raw.get("benchmark", {})),
            flows=[FlowSpec.from_dict(item, i) for i, item in enumerate(flows_raw)],
            agent=dict(raw.get("agent", {})),
            judge=dict(raw.get("judge", {})),
        )


def load_spec(path: str | Path) -> BenchmarkSpec:
    """Load JSON, or YAML when PyYAML is installed, into the stable spec model."""
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SpecError("YAML specs require PyYAML; use JSON or install PyYAML") from exc
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    if not isinstance(raw, dict):
        raise SpecError("benchmark spec root must be an object")
    return BenchmarkSpec.from_dict(raw)
