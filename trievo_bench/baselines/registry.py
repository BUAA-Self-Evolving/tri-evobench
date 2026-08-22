from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agent import OpenAICompatibleAgent, ScriptedAgent
from ..learner import learner_from_mode
from ..model import TokenBudget, client_from_config
from ..spec import BenchmarkSpec
from .base import BaselineRuntime
from .legacy import LegacyActionRuntime
from .manifest import find_manifest, load_manifest, merge_mappings
from .process import ProcessBaselineRuntime


LEGACY_BASELINES = frozenset({"scripted", "openai_compatible"})


def create_baseline_runtime(
    baseline_id: str,
    *,
    spec: BenchmarkSpec,
    budget: TokenBudget,
    learner_mode: str,
    repo_root: Path,
    manifest_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> BaselineRuntime:
    if baseline_id == "scripted":
        agent = ScriptedAgent(spec.agent.get("policy", {}))
        return LegacyActionRuntime(baseline_id, agent, learner_from_mode(learner_mode), _all_turns(spec))
    if baseline_id == "openai_compatible":
        client = client_from_config(spec.agent, budget)
        agent = OpenAICompatibleAgent(
            client,
            spec.agent.get("system_prompt"),
            str(spec.agent.get("response_format", "json")),
            int(spec.agent.get("max_output_tokens", 1024)),
        )
        return LegacyActionRuntime(baseline_id, agent, learner_from_mode(learner_mode), _all_turns(spec))

    resolved = find_manifest(baseline_id, repo_root, explicit=manifest_path)
    manifest = load_manifest(resolved)
    if manifest.baseline_id != baseline_id:
        raise ValueError(
            f"baseline manifest id mismatch: selected {baseline_id!r}, manifest declares {manifest.baseline_id!r}"
        )
    manifest = manifest.with_overrides(overrides)
    if manifest.baseline_id != baseline_id:
        raise ValueError(
            f"baseline overrides cannot change manifest id from {baseline_id!r} to {manifest.baseline_id!r}"
        )
    return ProcessBaselineRuntime(manifest, budget=budget, repo_root=repo_root)


def resolve_baseline_selection(
    spec: BenchmarkSpec,
    *,
    baseline_override: str | None,
    agent_override: str | None,
    config_override: dict[str, Any] | None,
    manifest_override: str | None,
) -> tuple[str, str | None, dict[str, Any]]:
    configured = dict(spec.baseline)
    configured = merge_mappings(configured, config_override or {})
    configured_type = configured.pop("type", None)
    configured_manifest = configured.pop("manifest", None)
    baseline_id = str(
        baseline_override
        or agent_override
        or configured_type
        or spec.agent.get("type", "scripted")
    )
    manifest = manifest_override or configured_manifest
    return baseline_id, str(manifest) if manifest else None, configured


def _all_turns(spec: BenchmarkSpec):
    return [turn for flow in spec.flows for turn in flow.turns]
