"""Compatibility and isolated launcher for materialized L2 stream packages.

L2 packages deliberately separate agent-visible episode data from benchmark-private
evaluation assets.  This module preserves that boundary: it validates both views
without copying private files into an agent prompt, and delegates execution to the
package's own runtime inside a disposable Ubuntu container.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class L2SpecError(ValueError):
    """Raised when a materialized L2 package violates its public contract."""


@dataclass(frozen=True)
class L2StreamInfo:
    stream_id: str
    episode_count: int
    mode: str
    workspace_snapshot: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise L2SpecError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise L2SpecError(f"JSON root must be an object: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise L2SpecError(f"invalid JSONL: {path}") from exc
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise L2SpecError(f"invalid JSONL: {path}:{number}") from exc
        if not isinstance(value, dict):
            raise L2SpecError(f"JSONL row must be an object: {path}:{number}")
        rows.append(value)
    return rows


def _under(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise L2SpecError(f"path escapes package root: {relative}")
    return candidate


def inspect_l2_package(package_dir: str | Path) -> list[L2StreamInfo]:
    """Validate public/private L2 boundaries and return an inventory.

    Validation intentionally reads private evaluation metadata only in this
    benchmark-runtime process.  Agent-facing fields are checked for private-path
    leakage but never expanded or returned as model input.
    """
    root = Path(package_dir).resolve()
    streams_root = root / "streams"
    if not streams_root.is_dir():
        raise L2SpecError(f"missing streams directory: {streams_root}")

    v5_contract = _load_v5_contract(root)
    v5_source_identities: set[tuple[str, str, str]] = set()
    # A source task may appear once per pool; pools are aggregated separately.
    v5_pool_identities: set[tuple[str, tuple[str, str, str]]] = set()
    v5_turn_count = 0

    result: list[L2StreamInfo] = []
    for stream_dir in sorted(path for path in streams_root.iterdir() if path.is_dir()):
        manifest_path = stream_dir / "stream.json"
        if not manifest_path.is_file():
            continue
        manifest = _load_json(manifest_path)
        stream_id = manifest.get("stream_id")
        if stream_id != stream_dir.name:
            raise L2SpecError(f"{stream_dir.name}: stream_id must match its directory")
        # Versioned TaskStream releases keep each
        # turn self-contained (turn*/task.json) instead of the legacy
        # stream.json -> episodes/*/episode.json indirection.  Validate its
        # public shape here so the package can be inventoried by the same CLI
        # without exposing evaluation assets to an agent.
        taskstream_version = manifest.get("schema_version")
        if taskstream_version in {"l2_taskstream/1.0", "l2_taskstream/1.2"}:
            if taskstream_version == "l2_taskstream/1.2" and v5_contract is None:
                raise L2SpecError("l2_taskstream/1.2 stream requires a 1.2 package manifest")
            if taskstream_version == "l2_taskstream/1.0" and v5_contract is not None:
                raise L2SpecError("l2_taskstream/1.2 package requires 1.2 stream documents")
            workspace_name = manifest.get("agent_workspace")
            ordering = manifest.get("ordering")
            turns = manifest.get("turns")
            if not isinstance(workspace_name, str) or not workspace_name:
                raise L2SpecError(f"{stream_id}: agent_workspace is required")
            workspace_seed = _under(stream_dir, workspace_name)
            if not workspace_seed.is_dir():
                raise L2SpecError(f"{stream_id}: agent_workspace seed is missing")
            if not isinstance(ordering, dict) or not isinstance(ordering.get("turn_order"), list):
                raise L2SpecError(f"{stream_id}: ordering.turn_order is required")
            if not isinstance(turns, list) or len(turns) != len(ordering["turn_order"]):
                raise L2SpecError(f"{stream_id}: turns must match turn_order")
            _validate_taskstream_execution_requirements(stream_id, manifest)
            if v5_contract is not None:
                _validate_v5_stream(stream_id, manifest, v5_contract)
                v5_contract.setdefault("stream_capabilities", {})[stream_id] = manifest.get("capabilities", [])
            for turn_index, item in enumerate(ordering["turn_order"], start=1):
                if not isinstance(item, str):
                    raise L2SpecError(f"{stream_id}: turn ids must be text")
                task_path = _under(stream_dir, item + "/task.json")
                task = _load_json(task_path)
                summary = turns[turn_index - 1]
                if not isinstance(summary, dict):
                    raise L2SpecError(f"{stream_id}/{item}: invalid turn summary")
                sequence_role = task.get("sequence_role")
                identity = (item, turn_index, sequence_role)
                if (
                    task.get("stream_id") != stream_id
                    or (task.get("turn_id"), task.get("turn_index"), sequence_role) != identity
                    or (
                        summary.get("turn_id"),
                        summary.get("turn_index"),
                        summary.get("sequence_role"),
                    ) != identity
                ):
                    raise L2SpecError(f"{stream_id}/{item}: identity mismatch")
                if sequence_role not in {"learning", "application", "probe", "replay", "noise"}:
                    raise L2SpecError(f"{stream_id}/{item}: invalid sequence_role")
                agent_input = task.get("agent_input")
                if not isinstance(agent_input, dict) or not isinstance(agent_input.get("messages"), list):
                    raise L2SpecError(f"{stream_id}/{item}: agent_input.messages is required")
                visible = json.dumps(agent_input, ensure_ascii=False)
                if "evaluation_assets" in visible or "test_labels" in visible or "known_correct" in visible:
                    raise L2SpecError(f"{stream_id}/{item}: private evaluation path leaked into agent_input")
                evaluation = task.get("evaluation")
                if not isinstance(evaluation, dict) or evaluation.get("native_verifier", {}).get("required") is not True:
                    raise L2SpecError(f"{stream_id}/{item}: native verifier declaration is required")
                if v5_contract is not None:
                    source_identity = _validate_v5_task(
                        stream_id=stream_id,
                        task=task,
                        summary=summary,
                        turn_index=turn_index,
                        turn_order=ordering["turn_order"],
                        tasks_by_id={
                            turn_id: _load_json(_under(stream_dir, turn_id + "/task.json"))
                            for turn_id in ordering["turn_order"]
                        },
                        v5_contract=v5_contract,
                    )
                    pool_id = next(
                        (pid for pid, pool in v5_contract["pool_map"].items()
                         if stream_id in pool.get("stream_ids", [])),
                        None,
                    )
                    if (pool_id, source_identity) in v5_pool_identities:
                        raise L2SpecError(f"{stream_id}/{item}: duplicate source identity within pool {pool_id}")
                    v5_pool_identities.add((pool_id, source_identity))
                    v5_source_identities.add(source_identity)
                    _validate_v5_source_selection(stream_id, item, task, summary, v5_contract)
                _validate_taskstream_transition(
                    stream_dir,
                    task_path.parent,
                    workspace_seed,
                    task,
                    task_local=v5_contract is not None
                    and manifest.get("workspace_persistence") == "task_local",
                )
                v5_turn_count += int(v5_contract is not None)
            result.append(L2StreamInfo(str(stream_id), len(turns), "taskstream", workspace_name))
            continue
        if manifest.get("level") != "L2":
            raise L2SpecError(f"{stream_id}: level must be L2")
        workspace = manifest.get("workspace")
        ordering = manifest.get("ordering")
        episodes = manifest.get("episodes")
        runtime = manifest.get("runtime")
        if not isinstance(workspace, dict) or not isinstance(workspace.get("initial_snapshot"), str):
            raise L2SpecError(f"{stream_id}: workspace.initial_snapshot is required")
        snapshot = _under(stream_dir, str(workspace["initial_snapshot"]))
        if not snapshot.is_dir():
            raise L2SpecError(f"{stream_id}: missing workspace snapshot {snapshot}")
        if not isinstance(ordering, dict) or not isinstance(ordering.get("episode_order"), list):
            raise L2SpecError(f"{stream_id}: ordering.episode_order is required")
        if not isinstance(episodes, list) or len(episodes) != len(ordering["episode_order"]):
            raise L2SpecError(f"{stream_id}: episodes must match episode_order")
        if not isinstance(runtime, dict) or runtime.get("episode_result_policy") != "latest_interaction":
            raise L2SpecError(f"{stream_id}: runtime must declare latest_interaction policy")

        for expected_id, relative in zip(ordering["episode_order"], episodes):
            if not isinstance(relative, str):
                raise L2SpecError(f"{stream_id}: episode path must be text")
            episode_path = _under(stream_dir, relative)
            episode = _load_json(episode_path)
            if episode.get("episode_id") != expected_id or episode.get("stream_id") != stream_id:
                raise L2SpecError(f"{stream_id}/{expected_id}: identity mismatch")
            agent_view = episode.get("agent_view")
            if not isinstance(agent_view, dict):
                raise L2SpecError(f"{stream_id}/{expected_id}: agent_view is required")
            visible = json.dumps(agent_view, ensure_ascii=False)
            if "evaluation.json" in visible or "evaluation_assets" in visible:
                raise L2SpecError(f"{stream_id}/{expected_id}: private evaluation path leaked into agent_view")
            policy = episode.get("interaction_policy")
            if not isinstance(policy, dict) or not isinstance(policy.get("max_interactions"), int):
                raise L2SpecError(f"{stream_id}/{expected_id}: interaction_policy.max_interactions is required")
            if not 1 <= int(policy["max_interactions"]) <= 3:
                raise L2SpecError(f"{stream_id}/{expected_id}: max_interactions must be 1..3")
            evaluation_ref = episode.get("evaluation_ref")
            if not isinstance(evaluation_ref, str):
                raise L2SpecError(f"{stream_id}/{expected_id}: evaluation_ref is required")
            evaluation = _load_json(_under(episode_path.parent, evaluation_ref))
            if evaluation.get("visibility") != "benchmark_private":
                raise L2SpecError(f"{stream_id}/{expected_id}: evaluation must be benchmark_private")
            scoring = evaluation.get("scoring")
            if not isinstance(scoring, dict) or scoring.get("episode_score_policy") != "latest_interaction":
                raise L2SpecError(f"{stream_id}/{expected_id}: scorer must use latest_interaction")

        mode = "api" if runtime.get("runner") else "browser-smoke"
        result.append(L2StreamInfo(str(stream_id), len(episodes), mode, str(workspace["initial_snapshot"])))
    if not result:
        raise L2SpecError(f"no L2 streams found in {streams_root}")
    _validate_l2_panels(root, {item.stream_id for item in result})
    if v5_contract is not None:
        _validate_v5_package_counts(
            root,
            v5_contract,
            result,
            unique_source_task_count=len(v5_source_identities),
            turn_placement_count=v5_turn_count,
        )
    return result


def _validate_v5_source_selection(
    stream_id: str,
    turn_id: str,
    task: dict[str, Any],
    summary: dict[str, Any],
    contract: dict[str, Any],
) -> None:
    source = task["source_basis"]
    source_id = f"{source['benchmark_id']}:{source['source_task_id']}"
    source_map = contract.get("source_map")
    if isinstance(source_map, dict):
        catalog = source_map.get(source_id)
        if not isinstance(catalog, dict) or catalog.get("source") != source:
            raise L2SpecError(f"{stream_id}/{turn_id}: task source does not match source catalog")
    selection_map = contract.get("selection_map")
    if isinstance(selection_map, dict):
        selected = selection_map.get((stream_id, turn_id))
        expected = {
            "turn_id": turn_id,
            "turn_index": task.get("turn_index"),
            "sequence_role": task.get("sequence_role"),
            "source_id": source_id,
            "source_hash": source.get("source_hash"),
            "capability_id": task.get("taxonomy_ref", {}).get("capability_id"),
        }
        if not isinstance(selected, dict) or any(selected.get(key) != value for key, value in expected.items()):
            raise L2SpecError(f"{stream_id}/{turn_id}: selection provenance does not match task")
        if summary.get("source_task_id") != source.get("source_task_id"):
            raise L2SpecError(f"{stream_id}/{turn_id}: stream summary source does not match task")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_v5_contract(root: Path) -> dict[str, Any] | None:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != "l2_taskstream/1.2":
        return None
    if manifest.get("aggregation_profile") != "l2_evolution_aggregation/1.2":
        raise L2SpecError("1.2 manifest requires aggregation_profile l2_evolution_aggregation/1.2")
    bindings = {
        "pool_manifest": "pools.json",
        "panel_manifest": "panels.json",
        "taxonomy_manifest": "taxonomy.json",
        "runtime_profiles": "runtime_profiles.json",
    }
    loaded: dict[str, Any] = {"manifest": manifest}
    for key, default_path in bindings.items():
        binding = manifest.get(key)
        if not isinstance(binding, dict):
            raise L2SpecError(f"1.2 manifest requires {key}")
        relative = binding.get("path", default_path)
        if not isinstance(relative, str) or not relative:
            raise L2SpecError(f"{key}.path must be non-empty")
        path = _under(root, relative)
        if not path.is_file():
            raise L2SpecError(f"{key} path is missing: {relative}")
        digest = binding.get("sha256")
        if not isinstance(digest, str) or digest != _sha256(path):
            label = key.removesuffix("_manifest").replace("_", " ")
            raise L2SpecError(f"{label} SHA-256 binding mismatch")
        loaded[key] = _load_json(path)

    optional_bindings = {
        "source_catalog": "source_tasks.jsonl",
        "selection_manifest": "stream_plan.json",
    }
    for key, default_path in optional_bindings.items():
        binding = manifest.get(key)
        if binding is None:
            continue
        if not isinstance(binding, dict):
            raise L2SpecError(f"1.2 manifest requires {key} binding to be an object")
        relative = binding.get("path", default_path)
        if not isinstance(relative, str) or not relative:
            raise L2SpecError(f"{key}.path must be non-empty")
        path = _under(root, relative)
        if not path.is_file():
            raise L2SpecError(f"{key} path is missing: {relative}")
        digest = binding.get("sha256")
        if not isinstance(digest, str) or digest != _sha256(path):
            raise L2SpecError(f"{key} SHA-256 binding mismatch")
        loaded[key] = _load_jsonl(path) if key == "source_catalog" else _load_json(path)

    if "source_catalog" in loaded:
        source_map: dict[str, dict[str, Any]] = {}
        for row in loaded["source_catalog"]:
            source_id = row.get("source_id")
            if not isinstance(source_id, str) or not source_id or source_id in source_map:
                raise L2SpecError("source_catalog source_id values must be unique non-empty strings")
            source_map[source_id] = row
        loaded["source_map"] = source_map
    selection_dir = root / "selections"
    if selection_dir.is_dir():
        selection_map: dict[tuple[str, str], dict[str, Any]] = {}
        for path in sorted(selection_dir.glob("*.json")):
            document = _load_json(path)
            stream_id = document.get("stream_id")
            selected = document.get("selected_turns")
            if not isinstance(stream_id, str) or not isinstance(selected, list):
                raise L2SpecError(f"{path.name}: invalid selection provenance")
            for item in selected:
                if not isinstance(item, dict) or not isinstance(item.get("turn_id"), str):
                    raise L2SpecError(f"{path.name}: invalid selected turn")
                key = (stream_id, item["turn_id"])
                if key in selection_map:
                    raise L2SpecError(f"duplicate selection provenance for {stream_id}/{item['turn_id']}")
                selection_map[key] = item
        loaded["selection_map"] = selection_map

    pools = loaded["pool_manifest"]
    if pools.get("schema_version") != "l2_pools/1.0":
        raise L2SpecError("pools.json must declare l2_pools/1.0")
    pool_items = pools.get("pools")
    if not isinstance(pool_items, list) or not pool_items:
        raise L2SpecError("pools.json must declare non-empty pools")
    pool_map: dict[str, dict[str, Any]] = {}
    for pool in pool_items:
        if not isinstance(pool, dict):
            raise L2SpecError("pool entries must be objects")
        pool_id = pool.get("pool_id")
        if not isinstance(pool_id, str) or not pool_id or pool_id in pool_map:
            raise L2SpecError("pool_id values must be non-empty and unique")
        if pool.get("allows_cross_pool_aggregation") is not False:
            raise L2SpecError(f"{pool_id}: cross-pool aggregation must be false")
        pool_map[pool_id] = pool
    loaded["pool_map"] = pool_map

    profiles = loaded["runtime_profiles"]
    if profiles.get("schema_version") != "l2_runtime_profiles/1.0":
        raise L2SpecError("runtime_profiles.json must declare l2_runtime_profiles/1.0")
    profile_map: dict[str, dict[str, Any]] = {}
    for profile in profiles.get("profiles", []):
        if not isinstance(profile, dict):
            raise L2SpecError("runtime profile entries must be objects")
        profile_id = profile.get("runtime_profile_id")
        if not isinstance(profile_id, str) or not profile_id or profile_id in profile_map:
            raise L2SpecError("runtime_profile_id values must be non-empty and unique")
        digest = profile.get("task_sandbox_image_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
            raise L2SpecError(f"{profile_id}: invalid task_sandbox_image_digest")
        verifier_digest = profile.get("verifier_image_digest")
        if (
            not isinstance(verifier_digest, str)
            or not verifier_digest.startswith("sha256:")
            or len(verifier_digest) != 71
        ):
            raise L2SpecError(f"{profile_id}: invalid verifier_image_digest")
        if profile.get("network") != "disabled":
            raise L2SpecError(f"{profile_id}: network must be disabled")
        if not isinstance(profile.get("learner_state_abi"), str) or not profile["learner_state_abi"]:
            raise L2SpecError(f"{profile_id}: learner_state_abi is required")
        for field in ("serialization_version", "mount_path", "owner", "permission"):
            if not isinstance(profile.get(field), str) or not profile[field]:
                raise L2SpecError(f"{profile_id}: {field} is required")
        profile_map[profile_id] = profile
    if not profile_map:
        raise L2SpecError("runtime_profiles.json must declare non-empty profiles")
    loaded["profile_map"] = profile_map

    taxonomy = loaded["taxonomy_manifest"]
    if taxonomy.get("schema_version") != "l2_taxonomy/1.0":
        raise L2SpecError("taxonomy.json must declare l2_taxonomy/1.0")
    raw_capabilities = taxonomy.get("capabilities")
    if isinstance(raw_capabilities, dict):
        capability_map = raw_capabilities
    elif isinstance(raw_capabilities, list):
        capability_map = {
            str(item.get("capability_id")): item
            for item in raw_capabilities
            if isinstance(item, dict) and isinstance(item.get("capability_id"), str)
        }
    else:
        capability_map = {}
    if not capability_map:
        raise L2SpecError("taxonomy.json must declare non-empty capabilities")
    loaded["capability_map"] = capability_map

    panels = loaded["panel_manifest"].get("panels")
    if not isinstance(panels, list) or not panels:
        raise L2SpecError("panels.json must declare non-empty panels")
    for panel in panels:
        if not isinstance(panel, dict):
            raise L2SpecError("panel entries must be objects")
        pool_id = panel.get("pool_id")
        pool = pool_map.get(pool_id)
        if pool is None:
            raise L2SpecError(f"panel references unknown pool {pool_id!r}")
        selected = panel.get("stream_ids")
        if not isinstance(selected, list) or not set(selected).issubset(set(pool.get("stream_ids", []))):
            raise L2SpecError("panel stream selection must be contained by its pool")
        license_scope = panel.get("license_scope")
        if license_scope is not None and license_scope not in pool.get("license_scopes", []):
            raise L2SpecError("panel license scope must be declared by its pool")
    return loaded


def _validate_v5_stream(
    stream_id: str,
    stream: dict[str, Any],
    v5_contract: dict[str, Any],
) -> None:
    turn_count = stream.get("turn_count")
    if not isinstance(turn_count, int) or isinstance(turn_count, bool) or not 9 <= turn_count <= 20:
        raise L2SpecError(f"{stream_id}: turn_count must be between 9 and 20")
    if stream.get("workspace_persistence") not in {"stream", "task_local"}:
        raise L2SpecError(f"{stream_id}: workspace_persistence must be stream or task_local")
    design = stream.get("stream_design")
    if not isinstance(design, dict):
        raise L2SpecError(f"{stream_id}: stream_design is required")
    pool_id = design.get("pool_id")
    pool = v5_contract["pool_map"].get(pool_id)
    if pool is None:
        raise L2SpecError(f"{stream_id}: unknown pool_id {pool_id!r}")
    if stream_id not in pool.get("stream_ids", []):
        raise L2SpecError(f"{stream_id}: stream is not declared by pool {pool_id}")
    capabilities = stream.get("capabilities")
    if not isinstance(capabilities, list) or len(capabilities) < 3:
        raise L2SpecError(f"{stream_id}: at least 3 capabilities are required")
    capability_ids = [item.get("capability_id") for item in capabilities if isinstance(item, dict)]
    if len(capability_ids) != len(capabilities) or len(set(capability_ids)) != len(capability_ids):
        raise L2SpecError(f"{stream_id}: capability IDs must be unique")
    benchmarks = {item.get("source_benchmark") for item in capabilities}
    pool_type = design.get("pool_type")
    if pool_type != pool.get("pool_type"):
        raise L2SpecError(f"{stream_id}: pool_type does not match pools.json")
    if pool_type == "same_benchmark" and len(benchmarks) != 1:
        raise L2SpecError(f"{stream_id}: same benchmark pool requires one source benchmark")
    if pool_type == "cross_benchmark" and len(benchmarks) != 3:
        raise L2SpecError(f"{stream_id}: cross pool requires exactly 3 benchmarks")
    if pool_type == "auto_research" and not benchmarks <= {"airs_bench", "mlgym"}:
        raise L2SpecError(f"{stream_id}: auto_research pool only admits AIRS-Bench and MLGym")
    if pool_type not in {"same_benchmark", "cross_benchmark", "auto_research"}:
        raise L2SpecError(f"{stream_id}: unsupported pool_type {pool_type!r}")
    if not isinstance(stream.get("selection_provenance"), dict):
        raise L2SpecError(f"{stream_id}: selection_provenance is required")


def _transfer_related(
    task: dict[str, Any],
    probe: dict[str, Any],
    source_capability: Any,
    target_capability: str,
    v5_contract: dict[str, Any],
) -> bool:
    """A cross-benchmark transfer probe may cite Turns of a capability in the same transfer group."""
    if task.get("relation_class") != "cross_benchmark_transfer":
        return False
    if probe.get("transfer_source_capability_id") != source_capability:
        return False
    group_id = probe.get("transfer_group_id")
    groups = v5_contract["taxonomy_manifest"].get("transfer_groups", [])
    return any(
        isinstance(group, dict)
        and group.get("group_id") == group_id
        and {source_capability, target_capability} <= set(group.get("capability_ids", []))
        for group in groups
    )


def _validate_v5_task(
    *,
    stream_id: str,
    task: dict[str, Any],
    summary: dict[str, Any],
    turn_index: int,
    turn_order: list[Any],
    tasks_by_id: dict[str, dict[str, Any]],
    v5_contract: dict[str, Any],
) -> tuple[str, str, str]:
    if task.get("schema_version") != "l2_taskstream/1.2":
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: task schema_version must be 1.2")
    source = task.get("source_basis")
    required_source = ("benchmark_id", "benchmark_revision", "source_task_id", "source_path", "source_hash")
    if not isinstance(source, dict) or any(not isinstance(source.get(key), str) or not source[key] for key in required_source):
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: complete source_basis is required")
    if len(source["source_hash"]) != 64:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: source_hash must be SHA-256")
    taxonomy = task.get("taxonomy_ref")
    if not isinstance(taxonomy, dict):
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: taxonomy_ref is required")
    capability_id = taxonomy.get("capability_id")
    capability = v5_contract["capability_map"].get(capability_id)
    if not isinstance(capability, dict):
        raise L2SpecError(
            f"{stream_id}/{task.get('turn_id')}: capability is absent from bound taxonomy"
        )
    taxonomy_version = v5_contract["taxonomy_manifest"].get("taxonomy_version")
    if taxonomy.get("taxonomy_version") != taxonomy_version:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: taxonomy version mismatch")
    if (
        taxonomy.get("domain_id") != capability.get("domain_id")
        or taxonomy.get("task_type_ids") != capability.get("task_type_ids")
    ):
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: taxonomy fields do not match codebook")
    profile_id = task.get("runtime_profile_id")
    if profile_id not in v5_contract["profile_map"]:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: unknown runtime profile {profile_id!r}")
    if summary.get("runtime_profile_id") != profile_id or summary.get("capability_id") != capability_id:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: summary taxonomy/runtime identity mismatch")
    relation = task.get("capability_relation")
    if not isinstance(relation, dict) or relation.get("capability_id") != capability_id:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: capability relation mismatch")
    if task.get("relation_class") not in {
        "native_family_sibling", "reviewed_shared_procedure", "cross_benchmark_transfer",
    }:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: invalid relation_class")
    role = task.get("sequence_role")
    probe = task.get("probe_spec")
    if role == "probe":
        if not isinstance(probe, dict):
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe_spec is required")
        if probe.get("target_capability_id") != capability_id:
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe target does not match capability")
        parents = probe.get("expected_transfer_from_turn_ids")
        if not isinstance(parents, list) or not parents:
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe transfer references are required")
        earlier = set(turn_order[: turn_index - 1])
        for parent in parents:
            if parent not in earlier:
                raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe references a future Turn")
            parent_task = tasks_by_id.get(str(parent), {})
            parent_capability = parent_task.get("taxonomy_ref", {}).get("capability_id")
            if parent_capability != capability_id and not _transfer_related(
                task, probe, parent_capability, capability_id, v5_contract
            ):
                raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe references another capability")
        if len(set(parents)) != len(parents):
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe transfer references must be unique")
        interference = probe.get("interference_capability_ids")
        stream_capabilities = {
            item.get("capability_id") for item in v5_contract.get("stream_capabilities", {}).get(stream_id, [])
            if isinstance(item, dict)
        }
        # Interference lists the other capabilities placed between the last
        # same-capability Turn and the probe; blocked schedules have none.
        if (
            not isinstance(interference, list)
            or len(set(interference)) != len(interference)
            or capability_id in interference
            or not set(interference).issubset(stream_capabilities)
        ):
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: invalid probe interference capabilities")
        gap = probe.get("probe_gap_turns")
        if gap is not None and (not isinstance(gap, int) or isinstance(gap, bool) or gap < 0):
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe_gap_turns must be a non-negative integer")
        if probe.get("held_out_source_task_id") != source["source_task_id"]:
            raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe held-out source does not match task")
    elif probe is not None:
        raise L2SpecError(f"{stream_id}/{task.get('turn_id')}: probe_spec is only allowed on probe Turns")
    return (source["benchmark_id"], source["benchmark_revision"], source["source_task_id"])


def _validate_v5_package_counts(
    root: Path,
    v5_contract: dict[str, Any],
    inventory: list[L2StreamInfo],
    *,
    unique_source_task_count: int,
    turn_placement_count: int,
) -> None:
    manifest = v5_contract["manifest"]
    expected = {
        "stream_count": len(inventory),
        "total_turn_count": turn_placement_count,
        "unique_source_task_count": unique_source_task_count,
        "turn_placement_count": turn_placement_count,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise L2SpecError(f"manifest {field} does not match package contents")
    stream_ids = {item.stream_id for item in inventory}
    declared = manifest.get("streams")
    if not isinstance(declared, list) or {item.get("stream_id") for item in declared if isinstance(item, dict)} != stream_ids:
        raise L2SpecError("manifest streams do not match package contents")
    pool_streams = {
        stream_id
        for pool in v5_contract["pool_map"].values()
        for stream_id in pool.get("stream_ids", [])
    }
    if pool_streams != stream_ids:
        raise L2SpecError("pool stream selection does not match package contents")


def _load_l2_panels(root: Path) -> tuple[dict[str, Any], str] | None:
    path = root / "panels.json"
    if not path.is_file():
        return None
    panels = _load_json(path)
    if panels.get("schema_version") != "l2_panels/1.0":
        raise L2SpecError("panels.json must declare schema_version l2_panels/1.0")
    manifest = _load_json(root / "manifest.json")
    if manifest.get("schema_version") not in {"l2_taskstream/1.1", "l2_taskstream/1.2"}:
        raise L2SpecError("panel-aware packages must declare l2_taskstream/1.1 or 1.2")
    binding = manifest.get("panel_manifest")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != "l2_panels/1.0"
        or binding.get("path") != "panels.json"
        or binding.get("sha256") != digest
    ):
        raise L2SpecError("manifest panel binding does not match panels.json")
    return panels, digest


def _validate_l2_panels(root: Path, stream_ids: set[str]) -> None:
    loaded = _load_l2_panels(root)
    if loaded is None:
        return
    panels, _ = loaded
    definitions = panels.get("panels")
    if not isinstance(definitions, list) or not definitions:
        raise L2SpecError("panels.json must declare a non-empty panels list")
    seen_panel_ids: set[str] = set()
    for panel in definitions:
        if not isinstance(panel, dict):
            raise L2SpecError("panels.json panel entries must be objects")
        panel_id = panel.get("panel_id")
        selected = panel.get("stream_ids")
        if not isinstance(panel_id, str) or not panel_id or panel_id in seen_panel_ids:
            raise L2SpecError("panels.json panel_id values must be non-empty and unique")
        seen_panel_ids.add(panel_id)
        if not isinstance(selected, list) or not selected or not all(
            isinstance(item, str) and item for item in selected
        ):
            raise L2SpecError(f"panels.json/{panel_id}: stream_ids must be non-empty strings")
        if len(set(selected)) != len(selected):
            raise L2SpecError(f"panels.json/{panel_id}: duplicate stream id")
        if panel.get("allows_cross_panel_aggregation") is not False:
            raise L2SpecError(f"panels.json/{panel_id}: cross-panel aggregation must be false")
        missing = sorted(set(selected) - stream_ids)
        if missing:
            raise L2SpecError(f"panels.json/{panel_id}: missing stream(s): {missing}")


def _validate_runtime_panel_binding(
    root: Path,
    stream_id: str,
    panel_id: str | None,
    panel_manifest_sha256: str | None,
    selected_stream_ids: tuple[str, ...],
) -> None:
    loaded = _load_l2_panels(root)
    if loaded is None:
        if panel_id is not None or panel_manifest_sha256 is not None or selected_stream_ids:
            raise L2SpecError("package has no panels.json for supplied panel metadata")
        return
    if panel_id is None or panel_manifest_sha256 is None or not selected_stream_ids:
        raise L2SpecError("panel-aware packages require complete panel metadata")
    panels, digest = loaded
    if panel_manifest_sha256 != digest:
        raise L2SpecError("runtime panel SHA-256 does not match panels.json")
    matches = [
        item for item in panels["panels"]
        if isinstance(item, dict) and item.get("panel_id") == panel_id
    ]
    if len(matches) != 1:
        raise L2SpecError(f"unknown or duplicate panel_id: {panel_id}")
    declared = matches[0].get("stream_ids")
    if list(selected_stream_ids) != declared:
        raise L2SpecError("runtime panel stream selection does not match panels.json")
    if stream_id not in selected_stream_ids:
        raise L2SpecError(f"stream {stream_id} is not a member of panel {panel_id}")


def _validate_taskstream_execution_requirements(
    stream_id: str,
    manifest: dict[str, Any],
) -> None:
    requirements = manifest.get("execution_requirements")
    if not isinstance(requirements, dict):
        raise L2SpecError(f"{stream_id}: execution_requirements is required")
    if requirements.get("network") != "disabled" or requirements.get("path_scope") != "agent_workspace":
        raise L2SpecError(f"{stream_id}: unsupported network or path_scope requirement")
    for name in (
        "max_interactions",
        "max_tool_calls_per_interaction",
        "turn_timeout_seconds",
        "max_output_tokens",
    ):
        value = requirements.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise L2SpecError(f"{stream_id}: execution_requirements.{name} must be positive")
    if requirements["max_interactions"] > 3:
        raise L2SpecError(f"{stream_id}: max_interactions exceeds Runtime support")


def _validate_taskstream_transition(
    stream_dir: Path,
    turn_dir: Path,
    workspace_seed: Path,
    task: dict[str, Any],
    *,
    task_local: bool = False,
) -> None:
    transition = task.get("workspace_transition")
    allowed_modes = {"task_local_seed"} if task_local else {"continue", "continue_with_patch"}
    if not isinstance(transition, dict) or transition.get("mode") not in allowed_modes:
        raise L2SpecError(f"{task.get('stream_id')}/{task.get('turn_id')}: unsupported transition")
    operations = transition.get("operations", [])
    if not isinstance(operations, list):
        raise L2SpecError(f"{task.get('stream_id')}/{task.get('turn_id')}: operations must be a list")
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") != "copy":
            raise L2SpecError(
                f"{task.get('stream_id')}/{task.get('turn_id')}: Runtime supports copy operations only"
            )
        source = _under(turn_dir, str(operation.get("source", "")))
        if not source.exists():
            raise L2SpecError(f"missing transition source: {source}")
        target = str(operation.get("target", ""))
        if not target.startswith("agent_workspace/"):
            raise L2SpecError(f"transition target must be agent_workspace-relative: {target}")
        _under(workspace_seed, target.removeprefix("agent_workspace/"))


def run_l2_in_docker(
    *,
    package_dir: str | Path,
    stream_id: str,
    output_root: str | Path,
    image: str,
    model: str | None,
    api_base: str | None,
    reasoning_effort: str,
    allow_generated_code: bool,
) -> int:
    """Run one L2 Stream in a constrained, disposable Docker container.

    The package source is read-only.  The only host write mount is ``output_root``.
    The API key, when needed, is inherited by name rather than serialized to a
    command line or a run artifact.  L2 generated-code verifiers still need their
    explicit opt-in because Docker is containment, not proof of safe execution.
    """
    root = Path(package_dir).resolve()
    streams = {item.stream_id: item for item in inspect_l2_package(root)}
    if stream_id not in streams:
        raise L2SpecError(f"unknown stream: {stream_id}")
    info = streams[stream_id]
    if not allow_generated_code:
        raise L2SpecError("L2 execution requires --allow-generated-code")
    if info.mode == "api" and not os.environ.get("L2_API_KEY"):
        raise L2SpecError("L2_API_KEY must be set in the environment for an API Stream")

    destination = Path(output_root).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        "docker", "run", "--rm", "--name", f"trievo-l2-{uuid.uuid4().hex[:12]}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
        "-v", f"{root}:/opt/l2_demo:ro", "-v", f"{destination}:/output",
        "-e", "PYTHONPATH=/opt/l2_demo/runtime/src", "-e", "MPLBACKEND=Agg",
    ]
    if info.mode == "api":
        command.extend(["-e", "L2_API_KEY"])
        command.extend(["-e", f"L2_API_BASE={api_base or os.environ.get('L2_API_BASE', 'https://api.deepseek.com/v1')}"])
        command.extend(["-e", f"L2_MODEL={model or os.environ.get('L2_MODEL', 'deepseek-v4-flash')}"])
    command.extend([image, "python", "/opt/l2_demo/demo.py"])
    if info.mode == "api":
        command.extend([
            "run", stream_id, "--model", model or os.environ.get("L2_MODEL", "deepseek-v4-flash"),
            "--reasoning-effort", reasoning_effort, "--output-root", "/output",
            "--allow-unsafe-code-execution",
        ])
        if api_base:
            command.extend(["--api-base", api_base])
    else:
        command.extend(["smoke", stream_id])
    return subprocess.run(command, check=False).returncode


def main() -> None:
    # Windows consoles commonly default to GBK.  Reports and verifier details
    # legitimately contain Unicode (for example, bullet characters), so keep
    # the CLI protocol UTF-8 and do not turn a completed run into a teardown
    # failure while printing JSON.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Validate or run a materialized L2 demo package.")
    parser.add_argument("--package", required=True, help="extracted l2_demo directory")
    parser.add_argument("--stream", help="stream id; omit with --inspect")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", default="runs/l2")
    parser.add_argument("--image", default="trievo-l2:0.1")
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--allow-generated-code", action="store_true")
    parser.add_argument(
        "--baseline",
        help=(
            "run L2 through a registered agent baseline "
            "(hermes, skillopt, trace2skill, prime-agent, or adaptive-auto-harness)"
        ),
    )
    parser.add_argument("--baseline-config", help="JSON/YAML overrides for the agent baseline manifest")
    parser.add_argument("--max-total-tokens", type=int, default=2_000_000)
    parser.add_argument("--browser-image", default="trievo-l2-browser:0.1")
    parser.add_argument("--resume-checkpoint", help="post-turn checkpoint directory; rerun only subsequent TaskStream turns")
    parser.add_argument("--retry-interrupted-run", help="exact failed run_id authorized for one interrupted TaskStream retry")
    parser.add_argument("--retry-authorization", type=Path, help="consumed one-shot authorization ledger for --retry-interrupted-run")
    parser.add_argument("--retry-job", help="matrix job id bound to the consumed retry authorization")
    parser.add_argument("--stop-after-turn", help="stop after completing this TaskStream turn")
    parser.add_argument("--panel-id", help="L2 panel id for panel-aware matrix runs")
    parser.add_argument("--panel-manifest-sha256", help="SHA-256 of package panels.json")
    parser.add_argument("--panel-streams", help="comma-separated stream ids selected by the panel")
    parser.add_argument("--pool-id", help="L2 V5 pool id for 1.2 runtime runs")
    parser.add_argument("--pool-manifest-sha256", help="SHA-256 of package pools.json")
    parser.add_argument("--package-manifest-sha256", help="SHA-256 of package manifest.json")
    parser.add_argument("--runtime-profiles-sha256", help="SHA-256 of package runtime_profiles.json")
    parser.add_argument(
        "--condition",
        choices=["reset", "frozen", "evolving"],
        default="evolving",
        help="matched reference-run condition; the legacy default remains evolving",
    )
    parser.add_argument(
        "--evaluation-error-policy",
        choices=["stop", "continue"],
        default="stop",
        help="choose whether an evaluator control-plane error stops the run or skips the Turn",
    )
    args = parser.parse_args()
    inventory = inspect_l2_package(args.package)
    if args.inspect:
        print(json.dumps([asdict(item) for item in inventory], ensure_ascii=False, indent=2))
        return
    if not args.stream:
        raise SystemExit("--stream is required unless --inspect is used")
    panel_streams = tuple(item for item in (args.panel_streams or "").split(",") if item)
    try:
        _validate_runtime_panel_binding(
            Path(args.package).resolve(), args.stream, args.panel_id,
            args.panel_manifest_sha256, panel_streams,
        )
    except L2SpecError as exc:
        raise SystemExit(str(exc)) from exc
    if args.baseline:
        if not args.allow_generated_code:
            raise SystemExit("L2 real-agent execution requires --allow-generated-code")
        if args.retry_interrupted_run and args.retry_authorization is None:
            raise SystemExit("--retry-interrupted-run requires --retry-authorization")
        if args.retry_interrupted_run and (not args.resume_checkpoint or not args.stop_after_turn):
            raise SystemExit("--retry-interrupted-run requires --resume-checkpoint and --stop-after-turn")
        if args.retry_interrupted_run and not args.retry_job:
            raise SystemExit("--retry-interrupted-run requires --retry-job")
        from .l2_agent import L2AgentRunOptions, load_baseline_overrides, run_l2_agent_stream

        report = run_l2_agent_stream(
            L2AgentRunOptions(
                package_dir=Path(args.package),
                stream_id=args.stream,
                output_root=Path(args.output),
                baseline=args.baseline,
                baseline_overrides=load_baseline_overrides(args.baseline_config),
                max_total_tokens=args.max_total_tokens,
                verifier_image=args.image,
                browser_verifier_image=args.browser_image,
                repo_root=Path(__file__).resolve().parents[1],
                resume_checkpoint=Path(args.resume_checkpoint) if args.resume_checkpoint else None,
                condition=args.condition,
                evaluation_error_policy=args.evaluation_error_policy,
                retry_interrupted_run_id=args.retry_interrupted_run,
                stop_after_turn=args.stop_after_turn,
                retry_authorization=args.retry_authorization,
                retry_job_id=args.retry_job,
                panel_id=args.panel_id,
                panel_manifest_sha256=args.panel_manifest_sha256,
                selected_stream_ids=panel_streams,
                pool_id=args.pool_id,
                pool_manifest_sha256=args.pool_manifest_sha256,
                package_manifest_sha256=args.package_manifest_sha256,
                runtime_profiles_sha256=args.runtime_profiles_sha256,
            )
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    raise SystemExit(run_l2_in_docker(
        package_dir=args.package,
        stream_id=args.stream,
        output_root=args.output,
        image=args.image,
        model=args.model,
        api_base=args.api_base,
        reasoning_effort=args.reasoning_effort,
        allow_generated_code=args.allow_generated_code,
    ))


if __name__ == "__main__":
    main()
