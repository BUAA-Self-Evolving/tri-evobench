from __future__ import annotations

import json
import hashlib
import tempfile
from pathlib import Path

import pytest

from trievo_bench.l2_compat import L2SpecError, inspect_l2_package


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite_binding(package: Path, key: str) -> None:
    manifest = _read_json(package / "manifest.json")
    path = package / manifest[key]["path"]
    manifest[key]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_json(package / "manifest.json", manifest)


def _minimal_v5(root: Path, *, stream_patch: dict | None = None, task_patch: dict | None = None) -> Path:
    package = root / "L2_Demo_V5"
    stream = package / "streams" / "s"
    (stream / "agent_workspace").mkdir(parents=True)
    turn_ids = [f"turn{i}" for i in range(1, 13)]
    caps = ["cap_a", "cap_b", "cap_c"]
    roles = ["learning", "learning", "learning", "application", "application", "application",
             "application", "application", "application", "probe", "probe", "probe"]
    summaries = []
    for index, turn_id in enumerate(turn_ids, start=1):
        cap = caps[(index - 1) % 3]
        role = roles[index - 1]
        summaries.append({
            "turn_id": turn_id,
            "turn_index": index,
            "sequence_role": role,
            "capability_id": cap,
            "runtime_profile_id": "python_text_v1",
        })
        task = {
            "schema_version": "l2_taskstream/1.2",
            "stream_id": "s",
            "turn_id": turn_id,
            "turn_index": index,
            "sequence_role": role,
            "source_basis": {
                "benchmark_id": "skilllearnbench",
                "benchmark_revision": "rev",
                "source_task_id": f"{cap}-{index}",
                "source_path": f"tasks/{cap}/{index}",
                "source_hash": "0" * 64,
            },
            "taxonomy_ref": {
                "taxonomy_version": "tri-evobench-l2-v5-codebook/1.0",
                "domain_id": "software_engineering",
                "task_type_ids": ["analysis"],
                "capability_id": cap,
                "native_labels": {},
            },
            "relation_class": "native_family_sibling",
            "runtime_profile_id": "python_text_v1",
            "capability_relation": {
                "capability_id": cap,
                "parent_turn_ids": [],
            },
            "agent_input": {"messages": [{"role": "user", "content": "write TOKEN"}]},
            "workspace_transition": {"mode": "task_local_seed", "operations": []},
            "evaluation": {"native_verifier": {"required": True}},
        }
        if role == "probe":
            task["probe_spec"] = {
                "target_capability_id": cap,
                "expected_transfer_from_turn_ids": [
                    item["turn_id"] for item in summaries if item["capability_id"] == cap
                ][:-1],
                "interference_capability_ids": sorted(set(caps) - {cap}),
                "claim_scope": "within_benchmark_retention",
                "held_out_source_task_id": task["source_basis"]["source_task_id"],
            }
        if task_patch and turn_id == "turn10":
            task.update(task_patch)
        _write_json(stream / turn_id / "task.json", task)
    stream_manifest = {
        "schema_version": "l2_taskstream/1.2",
        "stream_id": "s",
        "directory": "s",
        "profile": "v5",
        "theme": "test",
        "turn_count": 12,
        "task_layout": "turn_directories",
        "agent_workspace": "agent_workspace",
        "workspace_persistence": "task_local",
        "turn_type_counts": {"capability_application": 12},
        "fact_thread_count": 0,
        "stream_design": {
            "pool_id": "intra_benchmark_multi_capability",
            "pool_type": "same_benchmark",
            "composition_policy": "three_distinct_capabilities_one_benchmark",
        },
        "capabilities": [
            {"capability_id": cap, "domain_id": "software_engineering",
             "task_type_ids": ["analysis"], "source_benchmark": "skilllearnbench"}
            for cap in caps
        ],
        "ordering": {"method": "fixed", "seed": 7, "turn_order": turn_ids, "note": "test"},
        "execution_requirements": {
            "network": "disabled",
            "path_scope": "agent_workspace",
            "max_interactions": 3,
            "max_tool_calls_per_interaction": 4,
            "turn_timeout_seconds": 30,
            "max_output_tokens": 1000,
        },
        "turns": summaries,
        "selection_provenance": {"selector": "test", "seed": 7},
    }
    if stream_patch:
        stream_manifest.update(stream_patch)
    _write_json(stream / "stream.json", stream_manifest)
    _write_json(package / "runtime_profiles.json", {
        "schema_version": "l2_runtime_profiles/1.0",
        "profiles": [{
            "runtime_profile_id": "python_text_v1",
            "task_sandbox_image_digest": "sha256:" + "1" * 64,
            "verifier_image_digest": "sha256:" + "2" * 64,
            "verifier_image_class": "python",
            "tool_profile_id": "terminal_filesystem_v1",
            "dependency_class": "python_stdlib",
            "network": "disabled",
            "turn_timeout_seconds": 30,
            "learner_state_mount_profile": "baseline_explicit_v1",
            "learner_state_abi": "baseline_state/1.0",
            "serialization_version": "1",
            "mount_path": "/baseline_state",
            "owner": "baseline",
            "permission": "rw",
        }],
    })
    _write_json(package / "pools.json", {
        "schema_version": "l2_pools/1.0",
        "pools": [{
            "pool_id": "intra_benchmark_multi_capability",
            "pool_type": "same_benchmark",
            "stream_ids": ["s"],
            "composition_policy": "three_distinct_capabilities_one_benchmark",
            "license_scopes": ["permissive"],
            "allows_cross_pool_aggregation": False,
        }],
    })
    _write_json(package / "panels.json", {
        "schema_version": "l2_panels/1.0",
        "panels": [{
            "panel_id": "intra_primary",
            "purpose": "primary",
            "pool_id": "intra_benchmark_multi_capability",
            "stream_ids": ["s"],
            "allows_cross_panel_aggregation": False,
        }],
    })
    _write_json(package / "taxonomy.json", {
        "schema_version": "l2_taxonomy/1.0",
        "taxonomy_version": "tri-evobench-l2-v5-codebook/1.0",
        "capabilities": {
            cap: {"domain_id": "software_engineering", "task_type_ids": ["analysis"]}
            for cap in caps
        },
    })
    bindings = {}
    for name in ("pools.json", "panels.json", "taxonomy.json", "runtime_profiles.json"):
        digest = hashlib.sha256((package / name).read_bytes()).hexdigest()
        key = name.removesuffix(".json")
        if name == "pools.json":
            key = "pool_manifest"
        elif name == "panels.json":
            key = "panel_manifest"
        elif name == "taxonomy.json":
            key = "taxonomy_manifest"
        else:
            key = "runtime_profiles"
        schema_versions = {
            "pool_manifest": "l2_pools/1.0",
            "panel_manifest": "l2_panels/1.0",
            "taxonomy_manifest": "l2_taxonomy/1.0",
            "runtime_profiles": "l2_runtime_profiles/1.0",
        }
        bindings[key] = {
            "schema_version": schema_versions[key],
            "path": name,
            "sha256": digest,
        }
    _write_json(package / "manifest.json", {
        "schema_version": "l2_taskstream/1.2",
        "benchmark": "test",
        "aggregation_profile": "l2_evolution_aggregation/1.2",
        "stream_count": 1,
        "total_turn_count": 12,
        "unique_source_task_count": 12,
        "turn_placement_count": 12,
        "streams": [{"stream_id": "s", "directory": "s", "turn_count": 12, "path": "streams/s/stream.json"}],
        **bindings,
    })
    return package


def test_v5_package_passes_extended_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        inventory = inspect_l2_package(_minimal_v5(Path(tmp)))
    assert inventory[0].stream_id == "s"
    assert inventory[0].episode_count == 12


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"turn_count": 21}, "turn_count"),
        ({"workspace_persistence": "stream"}, "workspace_persistence"),
        ({"capabilities": [{"capability_id": "a", "domain_id": "d", "task_type_ids": ["t"], "source_benchmark": "x"}]}, "at least 3"),
    ],
)
def test_v5_stream_field_failures(patch: dict, message: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(L2SpecError, match=message):
            inspect_l2_package(_minimal_v5(Path(tmp), stream_patch=patch))


def test_v5_probe_spec_rejects_future_transfer_reference() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp), task_patch={
            "probe_spec": {
                "target_capability_id": "cap_a",
                "expected_transfer_from_turn_ids": ["turn12"],
                "interference_capability_ids": ["cap_b"],
                "claim_scope": "within_benchmark_retention",
                "held_out_source_task_id": "x",
            }
        })
        with pytest.raises(L2SpecError, match="future"):
            inspect_l2_package(package)


@pytest.mark.parametrize(
    "binding",
    ["pool_manifest", "panel_manifest", "taxonomy_manifest", "runtime_profiles"],
)
def test_v5_manifest_requires_every_bound_catalog(binding: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        manifest = _read_json(package / "manifest.json")
        manifest.pop(binding)
        _write_json(package / "manifest.json", manifest)
        with pytest.raises(L2SpecError, match=binding):
            inspect_l2_package(package)


def test_v5_manifest_rejects_catalog_hash_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        manifest = _read_json(package / "manifest.json")
        manifest["pool_manifest"]["sha256"] = "f" * 64
        _write_json(package / "manifest.json", manifest)
        with pytest.raises(L2SpecError, match="pool.*SHA-256|pool.*hash"):
            inspect_l2_package(package)


def test_v5_intra_pool_rejects_multiple_source_benchmarks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        stream_path = package / "streams/s/stream.json"
        stream = _read_json(stream_path)
        stream["capabilities"][2]["source_benchmark"] = "tau2"
        _write_json(stream_path, stream)
        with pytest.raises(L2SpecError, match="same benchmark"):
            inspect_l2_package(package)


def test_v5_cross_pool_requires_exactly_three_benchmarks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        stream_path = package / "streams/s/stream.json"
        stream = _read_json(stream_path)
        stream["stream_design"] = {
            "pool_id": "cross_benchmark_multi_capability",
            "pool_type": "cross_benchmark",
            "composition_policy": "three_capabilities_three_benchmarks",
        }
        stream["capabilities"][1]["source_benchmark"] = "tau2"
        _write_json(stream_path, stream)
        pools_path = package / "pools.json"
        pools = _read_json(pools_path)
        pools["pools"][0].update({
            "pool_id": "cross_benchmark_multi_capability",
            "pool_type": "cross_benchmark",
            "composition_policy": "three_capabilities_three_benchmarks",
        })
        _write_json(pools_path, pools)
        panels_path = package / "panels.json"
        panels = _read_json(panels_path)
        panels["panels"][0]["pool_id"] = "cross_benchmark_multi_capability"
        _write_json(panels_path, panels)
        manifest = _read_json(package / "manifest.json")
        manifest["pool_manifest"]["sha256"] = hashlib.sha256(pools_path.read_bytes()).hexdigest()
        manifest["panel_manifest"]["sha256"] = hashlib.sha256(panels_path.read_bytes()).hexdigest()
        _write_json(package / "manifest.json", manifest)
        with pytest.raises(L2SpecError, match="3.*benchmark|three.*benchmark"):
            inspect_l2_package(package)


def test_v5_task_runtime_profile_must_exist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        task_path = package / "streams/s/turn1/task.json"
        task = _read_json(task_path)
        task["runtime_profile_id"] = "missing"
        _write_json(task_path, task)
        with pytest.raises(L2SpecError, match="runtime profile"):
            inspect_l2_package(package)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_sandbox_image_digest", "latest"),
        ("verifier_image_digest", "latest"),
        ("network", "enabled"),
        ("learner_state_abi", ""),
    ],
)
def test_v5_runtime_profile_fails_closed(field: str, value: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        profiles_path = package / "runtime_profiles.json"
        profiles = _read_json(profiles_path)
        profiles["profiles"][0][field] = value
        _write_json(profiles_path, profiles)
        manifest = _read_json(package / "manifest.json")
        manifest["runtime_profiles"]["sha256"] = hashlib.sha256(profiles_path.read_bytes()).hexdigest()
        _write_json(package / "manifest.json", manifest)
        with pytest.raises(L2SpecError, match=field.replace("_", " ") + "|" + field):
            inspect_l2_package(package)


def test_v5_rejects_duplicate_source_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        first = _read_json(package / "streams/s/turn1/task.json")
        second_path = package / "streams/s/turn2/task.json"
        second = _read_json(second_path)
        second["source_basis"] = first["source_basis"]
        _write_json(second_path, second)
        with pytest.raises(L2SpecError, match="duplicate source"):
            inspect_l2_package(package)


def test_v5_probe_target_must_match_turn_capability() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        task_path = package / "streams/s/turn10/task.json"
        task = _read_json(task_path)
        task["probe_spec"]["target_capability_id"] = "cap_b"
        _write_json(task_path, task)
        with pytest.raises(L2SpecError, match="probe target"):
            inspect_l2_package(package)


@pytest.mark.parametrize("field", ["held_out_source_task_id", "interference_capability_ids"])
def test_v5_probe_binding_fails_closed(field: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        task_path = package / "streams/s/turn10/task.json"
        task = _read_json(task_path)
        if field == "held_out_source_task_id":
            task["probe_spec"][field] = "wrong-id"
        else:
            task["probe_spec"].pop(field)
        _write_json(task_path, task)
        with pytest.raises(L2SpecError, match="held-out|interference"):
            inspect_l2_package(package)


def test_v5_task_capability_must_exist_in_bound_taxonomy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        taxonomy_path = package / "taxonomy.json"
        taxonomy = _read_json(taxonomy_path)
        taxonomy["capabilities"].pop("cap_a")
        _write_json(taxonomy_path, taxonomy)
        manifest = _read_json(package / "manifest.json")
        manifest["taxonomy_manifest"]["sha256"] = hashlib.sha256(taxonomy_path.read_bytes()).hexdigest()
        _write_json(package / "manifest.json", manifest)
        with pytest.raises(L2SpecError, match="taxonomy|capability"):
            inspect_l2_package(package)


def test_v5_panel_pool_must_exist_and_match_selected_stream() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _minimal_v5(Path(tmp))
        panels_path = package / "panels.json"
        panels = _read_json(panels_path)
        panels["panels"][0]["pool_id"] = "missing_pool"
        _write_json(panels_path, panels)
        manifest = _read_json(package / "manifest.json")
        manifest["panel_manifest"]["sha256"] = hashlib.sha256(panels_path.read_bytes()).hexdigest()
        _write_json(package / "manifest.json", manifest)
        with pytest.raises(L2SpecError, match="pool"):
            inspect_l2_package(package)


def test_l2_demo_v5_tasks_embed_full_judge_and_simulator_prompts() -> None:
    package = Path("benchmarks/current/L2_Demo_V5")
    if not package.is_dir():
        pytest.skip("L2_Demo_V5 package is not present")
    judge_prompt = (package / "contract/prompts/l2_judge_prompt_v2.md").read_text(encoding="utf-8")
    simulator_prompt = (package / "contract/prompts/l2_user_simulator_prompt.md").read_text(encoding="utf-8")
    task_paths = sorted((package / "streams").glob("*/*/task.json"))

    assert task_paths
    for path in task_paths:
        task = _read_json(path)
        assert task["evaluation"]["judge_config"]["judge_prompt"] == judge_prompt
        assert task["user_simulator"]["simulator_prompt"] == simulator_prompt
