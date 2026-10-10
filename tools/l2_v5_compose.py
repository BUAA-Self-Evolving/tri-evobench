#!/usr/bin/env python3
"""Compose the L2 V5 TaskStream package from the qualified task pool.

Two steps, both deterministic:

* ``plan``    picks source tasks for every Stream from the admitted pool and the
              capability codebook and writes a reviewable stream plan.
* ``compose`` copies the planned pool tasks into self-contained Stream
              directories (``streams/<id>/turnN``) and writes the package
              catalogs that ``trievo_bench.l2_compat`` validates.

Workspace semantics follow the evolving run condition: one Workspace persists
across the Stream; each Turn's files are copied into
``agent_workspace/tasks/<turn_id>/`` by a ``continue_with_patch`` transition, so
earlier Turns' files stay visible.

Schedules (one per Stream; probe = the last task of a capability):
  blocked      A1..An, B1..Bn, C1..Cn
  interleaved  A1 B1 C1 A2 B2 C2 ...
  delayed      A1..A(n-1), B1..B(n-1), C1..C(n-1), Cn, Bn, An
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
from l2_data_pipeline import l2_judge_prompt_v2, l2_user_simulator_prompt  # noqa: E402

TEMPLATES = REPO_ROOT / "tools" / "l2_v5_templates"
PLAN_SCHEMA = "l2_v5_stream_plan/2.0"
TASK_SCHEMA = "l2_taskstream/1.2"
SCHEDULES = ("blocked", "interleaved", "delayed")
POOLS = {
    "intra_benchmark_multi_capability": {
        "pool_type": "same_benchmark", "composition_policy": "three_distinct_capabilities_one_benchmark"},
    "cross_benchmark_multi_capability": {
        "pool_type": "cross_benchmark", "composition_policy": "three_capabilities_three_benchmarks"},
    "auto_research_cpu": {
        "pool_type": "auto_research", "composition_policy": "three_autoresearch_capabilities_cpu_only"},
    "auto_research_cpu_gpu": {
        "pool_type": "auto_research", "composition_policy": "three_autoresearch_capabilities_cpu_and_gpu"},
    "explicit_dependency": {
        "pool_type": "explicit_dependency", "composition_policy": "staged_native_tasks_with_artifact_dependencies"},
}
LEARNER_ABI = {
    "learner_state_abi": "baseline_state/1.0", "learner_state_mount_profile": "baseline_explicit_v1",
    "mount_path": "/baseline_state", "owner": "baseline", "permission": "rw", "serialization_version": "1",
}
PROFILE_FAMILY = {
    "sr_agents": "sr_agents_python", "skilllearnbench": "skilllearn_task", "skillsbench": "skillsbench_task",
    "tau2_bench": "tau2_replay", "airs_bench": "autoresearch", "mlgym": "autoresearch",
}
SUBMISSION_ADAPTER = {
    "final_response": "identity", "workspace_artifact": "identity",
}

VERIFIER_CLI = '''#!/usr/bin/env python3
"""Package-local native verifier entrypoint (l2_native_verifier_request/1.0)."""
import json, os, sys
from pathlib import Path

turn_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(turn_dir / "evaluation_assets"))
task = json.loads((turn_dir / "task.json").read_text(encoding="utf-8"))
evaluation = task["evaluation"]
native = evaluation["native_verifier"]
verifier_id = native["verifier_id"]


def resolve(binding):
    declared = next(item for item in native["input_bindings"] if item["name"] == "submission")
    kind = binding.get("kind")
    if kind == "inline":
        if declared["source"] != "interaction_record" or not isinstance(binding.get("value"), str):
            raise ValueError("inline submission violates the declared carrier")
        return binding["value"]
    if kind == "path":
        if binding.get("path") != declared["path"]:
            raise ValueError("submission path does not match the declared binding")
        workspace = Path(os.environ.get("L2_VERIFIER_WORKSPACE", turn_dir.parent / "agent_workspace")).resolve()
        resolved = (workspace / Path(declared["path"]).relative_to("agent_workspace")).resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError("submission escapes the Workspace")
        return resolved
    raise ValueError("unsupported submission binding")


def error(message, diagnostics=None):
    return {"schema_version": "l2_native_verifier_result/1.0", "verifier_id": verifier_id,
            "status": "error", "score": None, "checks": [], "artifacts": [],
            "public_summary": {"result_category": "error",
                               "observable_deficiencies": ["Native verification could not complete."]},
            "private_diagnostics": diagnostics or {}, "error": message}


try:
    request = json.load(sys.stdin)
    if request.get("schema_version") != "l2_native_verifier_request/1.0":
        raise ValueError("unsupported request schema")
    submission = resolve(request.get("bindings", {}).get("submission", {}))
    from source_verifier import verify
    outcome = verify(submission, turn_dir)
    if isinstance(outcome, tuple):  # legacy (passed, details) carriers
        outcome = {"status": "pass" if outcome[0] else "fail", "score": 1.0 if outcome[0] else 0.0,
                   "raw_metric": None, "details": outcome[1]}
    status = outcome["status"]
    if status not in {"pass", "fail"}:
        raise ValueError(f"unknown verifier status {status!r}")
    score = float(outcome["score"]) if outcome.get("score") is not None else 0.0
    score = min(1.0, max(0.0, score))
    result = {"schema_version": "l2_native_verifier_result/1.0", "verifier_id": verifier_id,
              "status": status, "score": score,
              "checks": [{"check_id": "native_verifier_outcome", "status": status, "score": score,
                          "evidence": ["package-local native verifier completed"]}],
              "artifacts": [],
              "public_summary": {"result_category": status, "observable_deficiencies": [] if status == "pass"
                                 else ["The submitted result did not satisfy the native success condition."]},
              "private_diagnostics": outcome.get("details", {}), "error": None}
    if evaluation.get("evaluation_profile") == "auto_research" and status == "pass":
        profile = evaluation["metric_profile"]
        result["metric"] = {"name": profile["metric_name"], "raw_value": outcome.get("raw_metric"),
                            "raw_direction": profile["raw_direction"], "normalized_value": score}
except Exception as exc:
    result = error(f"{type(exc).__name__}: {exc}")
json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, default=str)
sys.stdout.write("\\n")
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# AutoResearch Turn score (contract §17.6): clipped AIRS normalized score of a
# submission the Judge finds valid; 0 otherwise.
TURN_SCORE_RULE = "native_normalized_score_if_valid"


def autoresearch_reference_scores(record: dict[str, Any]) -> dict[str, Any]:
    """Reference scores reported next to the Turn score; never thresholds.

    Pool records built before the continuous score carry ``performance_bands``
    instead of ``reference_scores``; for those the shuffled-gold reference is
    the AIRS ``known_correct`` fixture score (MLGym has none).
    """
    metric = record["metric_profile"]
    if isinstance(metric.get("reference_scores"), dict):
        return dict(metric["reference_scores"])
    shuffled = None
    if record["source"]["benchmark_id"] == "airs_bench":
        known = record.get("qualification", {}).get("known_correct") or {}
        if known.get("status") == "pass" and known.get("score") is not None:
            shuffled = round(min(1.0, max(0.0, float(known["score"]))), 6)
    return {"shuffled_gold_normalized": shuffled}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def binding(stage: Path, relative: str, schema_version: str) -> dict[str, str]:
    return {"schema_version": schema_version, "path": relative, "sha256": sha256(stage / relative)}


def load_pool(pool_root: Path) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for path in sorted(pool_root.glob("*/*/pool_task.json")):
        record = load_json(path)
        if not record.get("qualification", {}).get("admitted"):
            continue
        record["_dir"] = str(path.parent)
        if record["source_id"] in tasks:
            raise ValueError(f"duplicate pool source_id {record['source_id']}")
        tasks[record["source_id"]] = record
    return tasks


def schedule_positions(schedule: str, n_caps: int, per_cap: int) -> list[tuple[int, int]]:
    """Return (capability_index, k) per position; k is 0-based within the capability."""
    caps = range(n_caps)
    if schedule == "blocked":
        return [(c, k) for c in caps for k in range(per_cap)]
    if schedule == "interleaved":
        return [(c, k) for k in range(per_cap) for c in caps]
    if schedule == "delayed":
        head = [(c, k) for c in caps for k in range(per_cap - 1)]
        return head + [(c, per_cap - 1) for c in reversed(caps)]
    raise ValueError(f"unknown schedule {schedule}")


def role_for(k: int, per_cap: int) -> str:
    if k == per_cap - 1:
        return "probe"
    learning = 2 if per_cap >= 5 else 1
    return "learning" if k < learning else "application"


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def plan(pool_root: Path, codebook_path: Path, spec_path: Path, out: Path) -> None:
    """Fill a stream spec (ids, pool, schedule, capabilities) with source tasks.

    The spec names the capabilities of each Stream; this step only picks the
    concrete tasks. A source task is used at most once per pool; pools are
    aggregated separately, so the same task may appear in two pools.
    """
    pool = load_pool(pool_root)
    codebook = load_json(codebook_path)
    spec = load_json(spec_path)
    known = {item["capability_id"] for item in codebook["capabilities"]}
    by_cap: dict[str, list[str]] = defaultdict(list)
    for source_id, record in sorted(pool.items()):
        by_cap[record["capability_hint"]["capability_id"]].append(source_id)
    rng = random.Random(spec["seed"])
    # Source tasks already shipped in V4/V5 are drawn first so the release keeps
    # continuity with earlier packages; the rest of the pool follows in seeded order.
    preferred = set(spec.get("preferred_source_ids", []))
    for ids in by_cap.values():
        rng.shuffle(ids)
        ids.sort(key=lambda sid: sid not in preferred)
    used_by_pool: dict[str, set[str]] = defaultdict(set)
    streams = []
    for stream in spec["streams"]:
        used = used_by_pool[stream["pool_id"]]
        per_cap = stream.get("tasks_per_capability", 5)
        capabilities = []
        transfers = stream.get("transfer_probes", {})
        for cap in stream["capabilities"]:
            if cap not in known:
                raise ValueError(f"{stream['stream_id']}: capability {cap} is not in the codebook")
            wanted = stream.get("pinned", {}).get(cap, [])
            filters = stream.get("filters", {}).get(cap, {})
            chosen = [sid for sid in wanted if sid in pool]
            needed = per_cap - 1 if cap in transfers else per_cap
            for sid in by_cap.get(cap, []):
                if len(chosen) == needed:
                    break
                if sid in used or sid in chosen:
                    continue
                if pool[sid].get("pool_scope") == "explicit_dependency_only":
                    continue
                runtime = pool[sid]["runtime"]
                if "requires_gpu" in filters and runtime.get("requires_gpu") != filters["requires_gpu"]:
                    continue
                chosen.append(sid)
            if len(chosen) < needed:
                raise ValueError(f"{stream['stream_id']}: capability {cap} has {len(chosen)} < {needed} free tasks")
            used.update(chosen)
            entry = {"capability_id": cap, "source_ids": chosen}
            if cap in transfers:
                target = transfers[cap]
                group = next((g for g in codebook.get("transfer_groups", [])
                              if {cap, target} <= set(g["capability_ids"])), None)
                if group is None:
                    raise ValueError(f"{stream['stream_id']}: {cap} -> {target} is not a declared transfer group")
                probe = next((sid for sid in by_cap.get(target, []) if sid not in used), None)
                if probe is None:
                    raise ValueError(f"{stream['stream_id']}: no free transfer probe task for {target}")
                used.add(probe)
                entry.update(probe_source_id=probe, probe_capability_id=target, transfer_group_id=group["group_id"])
            capabilities.append(entry)
        streams.append({**{k: v for k, v in stream.items()
                           if k not in {"capabilities", "pinned", "filters", "transfer_probes"}},
                        "tasks_per_capability": per_cap, "capabilities": capabilities})
    write_json(out, {"schema_version": PLAN_SCHEMA, "package_id": spec["package_id"], "seed": spec["seed"],
                     "taxonomy_version": codebook["taxonomy_version"], "streams": streams})
    unique = set().union(*used_by_pool.values()) if used_by_pool else set()
    print(f"planned {len(streams)} streams, {len(unique)} unique source tasks -> {out}")


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def runtime_profile(record: dict[str, Any]) -> dict[str, Any]:
    runtime = record["runtime"]
    benchmark = record["source"]["benchmark_id"]
    compute = "gpu" if runtime.get("requires_gpu") else "cpu"
    timeout = int(runtime["turn_timeout_seconds"])
    family = PROFILE_FAMILY[benchmark]
    profile_id = f"{family}_{compute}_{timeout}s_v5"
    digest = runtime["task_image_digest"]
    return {
        **LEARNER_ABI,
        "runtime_profile_id": profile_id, "dependency_class": "source_native", "network": "disabled",
        "task_sandbox_image": runtime["task_image"], "task_sandbox_image_digest": digest,
        "verifier_image_class": "python", "verifier_image_digest": runtime.get("verifier_image_digest") or digest,
        "tool_profile_id": "terminal_filesystem_v1", "turn_timeout_seconds": timeout,
        "requires_gpu": bool(runtime.get("requires_gpu")),
        "gpu_count": int(runtime.get("gpu_count", 1 if runtime.get("requires_gpu") else 0)),
    }


def path_mapping_sentence(turn_id: str, record: dict[str, Any]) -> str:
    roots = record["runtime"].get("upstream_workdirs") or []
    task_dir = f"/workspace/tasks/{turn_id}/"
    if roots:
        listed = " or ".join(f"`{root.rstrip('/')}/`" for root in roots)
        return (f"The files for this task are in `{task_dir}`. Wherever the instructions below refer to "
                f"{listed}, use `{task_dir}` instead, and save the requested outputs there.\n\n")
    return f"The files for this task are in `{task_dir}`; save the requested outputs there.\n\n"


def submission_parts(turn_id: str, record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    sub = record["submission"]
    if sub["mode"] == "final_response":
        contract = {"mode": "final_response", "payload_kind": sub["payload_kind"], "source_binding": "submission"}
        bind = {"name": "submission", "source": "interaction_record", "path": "final_response"}
        return contract, bind, "final_response"
    relative = sub.get("relative_path") or ""
    path = f"agent_workspace/tasks/{turn_id}" + (f"/{relative}" if relative not in {"", "."} else "")
    contract = {"mode": "workspace_artifact", "payload_kind": sub["payload_kind"], "source_binding": "submission"}
    # Source verifiers take the Turn's task directory and resolve the declared
    # artifact inside it (tau2 reads submission/trajectory.json, MLGym RL needs
    # src/config.yaml next to checkpoints/), so the binding is the directory.
    bind = {"name": "submission", "source": "workspace", "path": f"agent_workspace/tasks/{turn_id}"}
    return contract, bind, path


def evaluation_block(stream_id: str, turn_id: str, record: dict[str, Any], turn_dir: Path,
                     rubric_paths: dict[str, str]) -> dict[str, Any]:
    profile = record["evaluation_profile"]
    template = load_json(TEMPLATES / f"evaluation_{profile}.json")
    evaluation = copy.deepcopy(template)
    contract, bind, submission_path = submission_parts(turn_id, record)
    contract.update({
        "adapter": {"owner": "native_verifier", "type": "identity", "entrypoint": None,
                    "version": "l2_v5_pool_carrier/1.0"},
        "audit": {"retain_raw_submission": True, "retain_adapted_submission": False, "hash_algorithm": "sha256"},
        "failure_semantics": {"empty_or_invalid_submission": "task_failure", "adapter_error": "verifier_error"},
    })
    timeout = int(record["runtime"].get("verifier_timeout_seconds", 900))
    evaluation["submission_contract"] = contract
    evaluation["native_verifier"] = {
        "required": True, "verifier_id": f"{stream_id}_{turn_id}_native_verifier",
        "entrypoint": "evaluation_assets/verifier_cli.py", "input_bindings": [bind],
        "request_schema": "l2_native_verifier_request/1.0", "result_schema": "l2_native_verifier_result/1.0",
        "timeout_seconds": timeout, "network": "disabled", "authority": "objective_evidence",
    }
    evaluation["judge_config"]["judge_prompt"] = l2_judge_prompt_v2()
    assets = sorted(p.relative_to(turn_dir).as_posix() for p in (turn_dir / "evaluation_assets").rglob("*") if p.is_file())
    evaluation["reference_context"]["native_assets"] = assets
    evaluation["reference_context"]["submission_mode"] = contract["mode"]
    required = [] if contract["mode"] == "final_response" else [submission_path]
    evaluation["gt"]["workspace_gt"]["required_outputs"] = required
    evaluation["expected_workspace_effect"]["required_outputs"] = required
    if profile == "general":
        evaluation["gt"]["answer_gt"]["submission_channel"] = contract["mode"]
        evaluation["reference_context"]["native_success_threshold"] = 1.0
        evaluation["reference_context"]["native_scoring_method"] = "binary_pass"
    else:
        metric = record["metric_profile"]
        benchmark = record["source"]["benchmark_id"]
        evaluation["metric_profile"] = {
            "profile_id": f"{record['source']['source_task_id']}_v5".replace("/", "_"),
            "metric_name": metric["metric_name"], "raw_direction": metric["raw_direction"],
            "raw_metric_required": True, "judge_value_source": "native_verifier_result.score",
            "judge_value_semantics": "normalized_higher_is_better_0_1",
            "turn_score_rule": TURN_SCORE_RULE, "validity_check_id": "auto_research_validity",
        }
        evaluation["reference_context"].update({
            "raw_metric_name": metric["metric_name"], "raw_metric_direction": metric["raw_direction"],
            "normalization": metric["normalization"],
            "reference_scores": autoresearch_reference_scores(record),
            "native_scoring_method": "upstream_normalized_metric",
        })
        evaluation["judge_config"]["rubric_route"] = {
            "composition": "common_then_domain_then_benchmark",
            "domain_rubric": {"rubric_id": "auto_research", "version": "2.0", "path": rubric_paths["auto_research"]},
            "benchmark_rubric": {"rubric_id": benchmark, "version": "2.0", "path": rubric_paths[benchmark]},
        }
    return evaluation


def copy_pool_task(record: dict[str, Any], turn_dir: Path) -> None:
    source = Path(record["_dir"])
    seed = turn_dir / "workspace_seed"
    if (source / "workspace_seed").is_dir():
        shutil.copytree(source / "workspace_seed", seed, symlinks=True)
    else:
        seed.mkdir(parents=True)
    shutil.copytree(source / "evaluation_assets", turn_dir / "evaluation_assets", symlinks=True)
    fixtures = turn_dir / "evaluation_assets" / "fixtures"
    shutil.copytree(source / "fixtures", fixtures, symlinks=True)
    (turn_dir / "evaluation_assets" / "verifier_cli.py").write_text(VERIFIER_CLI, encoding="utf-8")


def compose(pool_root: Path, plan_path: Path, codebook_path: Path, out: Path, rubric_dir: Path | None) -> None:
    pool = load_pool(pool_root)
    plan_doc = load_json(plan_path)
    codebook = load_json(codebook_path)
    capability_map = {item["capability_id"]: item for item in codebook["capabilities"]}
    plan_sha = sha256(plan_path)
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="l2-v5-compose-", dir=out.parent) as temporary:
        stage = Path(temporary) / out.name
        stage.mkdir()
        rubric_paths = {"auto_research": "prompts/rubrics/auto_research_v2.md",
                        "airs_bench": "prompts/rubrics/airs_bench_v2.md", "mlgym": "prompts/rubrics/mlgym_v2.md"}
        # The runtime reads routed rubrics from the package root; always ship them.
        shutil.copytree(rubric_dir or TEMPLATES / "rubrics", stage / "prompts" / "rubrics")
        missing = [path for path in rubric_paths.values() if not (stage / path).is_file()]
        if missing:
            raise ValueError(f"rubric files missing from the package: {missing}")
        profiles: dict[str, dict[str, Any]] = {}
        pool_streams: dict[str, list[str]] = defaultdict(list)
        manifest_streams, catalog_rows, taxonomy_used = [], {}, {}
        placements = 0
        for stream_plan in plan_doc["streams"]:
            stream_id = stream_plan["stream_id"]
            pool_id = stream_plan["pool_id"]
            pool_info = POOLS[pool_id]
            per_cap = stream_plan["tasks_per_capability"]
            caps = stream_plan["capabilities"]
            schedule = stream_plan["schedule"]
            positions = schedule_positions(schedule, len(caps), per_cap)
            stream_dir = stage / "streams" / stream_id
            (stream_dir / "agent_workspace" / "tasks").mkdir(parents=True)
            (stream_dir / "agent_workspace" / "README.md").write_text(
                "Persistent Stream Workspace. Each Turn's files are added under tasks/<turn_id>/.\n",
                encoding="utf-8")
            (stream_dir / "agent_workspace" / "tasks" / ".keep").write_text("", encoding="utf-8")
            capability_specs = []
            for cap in caps:
                entry = capability_map[cap["capability_id"]]
                spec = {"capability_id": entry["capability_id"], "domain_id": entry["domain_id"],
                        "task_type_ids": entry["task_type_ids"], "source_benchmark": entry["benchmark_id"]}
                capability_specs.append(spec)
                taxonomy_used[entry["capability_id"]] = spec
            summaries, selected_turns = [], []
            seen: dict[str, list[str]] = defaultdict(list)
            last_seen_index: dict[str, int] = {}
            for index, (cap_index, k) in enumerate(positions, start=1):
                cap = caps[cap_index]
                stream_capability = cap["capability_id"]
                role = role_for(k, per_cap)
                transfer = role == "probe" and "probe_source_id" in cap
                capability_id = cap["probe_capability_id"] if transfer else stream_capability
                source_id = cap["probe_source_id"] if transfer else cap["source_ids"][k]
                record = pool[source_id]
                turn_id = f"turn{index}"
                turn_dir = stream_dir / turn_id
                copy_pool_task(record, turn_dir)
                profile = runtime_profile(record)
                profiles[profile["runtime_profile_id"]] = profile
                entry = capability_map[capability_id]
                taxonomy_used[capability_id] = {
                    "capability_id": capability_id, "domain_id": entry["domain_id"],
                    "task_type_ids": entry["task_type_ids"], "source_benchmark": entry["benchmark_id"]}
                prompt = record["prompt"]
                dependency_turns: list[str] = []
                if record.get("dependency"):
                    # Explicit-dependency stage: the inputs live in the earlier stage's
                    # Turn directory, which the prompt names through a placeholder.
                    required = record["dependency"]["requires_source_id"]
                    dep_turn = next((t["turn_id"] for t in selected_turns if t["pool_source_id"] == required), None)
                    if dep_turn is None:
                        raise ValueError(f"{stream_id}/{turn_id}: dependency {required} is not placed earlier")
                    dependency_turns.append(dep_turn)
                    prompt = (prompt.replace(record["dependency"]["placeholder"], f"/workspace/tasks/{dep_turn}/")
                              .replace("{{turn_dir}}", f"/workspace/tasks/{turn_id}/"))
                elif record["submission"]["mode"] == "workspace_artifact" or any((turn_dir / "workspace_seed").iterdir()):
                    prompt = path_mapping_sentence(turn_id, record) + prompt
                parents = list(seen[stream_capability])
                task: dict[str, Any] = {
                    "schema_version": TASK_SCHEMA, "stream_id": stream_id, "turn_id": turn_id, "turn_index": index,
                    "turn_type": "capability_application", "turn_origin": record.get("turn_origin", "source_task"),
                    "sequence_role": role, "interaction_arm": "llm_post_failure",
                    "source_basis": {key: record["source"][key] for key in
                                     ("benchmark_id", "benchmark_revision", "source_hash", "source_path", "source_task_id",
                                      "parent_source_task_ids", "derivation") if key in record["source"]},
                    "taxonomy_ref": {
                        "taxonomy_version": plan_doc["taxonomy_version"], "domain_id": entry["domain_id"],
                        "task_type_ids": entry["task_type_ids"], "capability_id": capability_id,
                        "native_labels": record.get("benchmark_native_labels", {}),
                    },
                    "selection_provenance": {
                        "plan_sha256": plan_sha, "plan_schema_version": PLAN_SCHEMA, "seed": plan_doc["seed"],
                        "composition_policy": pool_info["composition_policy"], "schedule": schedule,
                    },
                    "relation_class": "cross_benchmark_transfer" if transfer
                    else stream_plan.get("relation_class", "native_family_sibling"),
                    "runtime_profile_id": profile["runtime_profile_id"],
                    "capability_relation": {"capability_id": capability_id, "parent_turn_ids": parents,
                                            **({"artifact_dependency_turn_ids": dependency_turns}
                                               if dependency_turns else {})},
                    "agent_input": {
                        "agent_workspace": "agent_workspace", "workspace_persistence": "stream",
                        "messages": [{"role": "user", "content": prompt}], "available_tools": [], "assets": [],
                    },
                    "workspace_transition": {
                        "mode": "continue_with_patch",
                        "operations": [{"op": "copy", "source": "workspace_seed",
                                        "target": f"agent_workspace/tasks/{turn_id}"}],
                        "preserve_unrelated_files": True,
                    },
                    "user_simulator": {
                        "type": "llm_post_failure", "trigger": "interaction_unsuccessful_and_budget_remaining",
                        "prompt_version": "l2_user_simulator/1.0", "simulator_prompt": l2_user_simulator_prompt(),
                        "temperature": 0, "output_schema": "l2_user_simulator_output/1.0",
                        "max_generated_messages": 2,
                    },
                }
                task["evaluation"] = evaluation_block(stream_id, turn_id, record, turn_dir, rubric_paths)
                if role == "probe":
                    gap = index - last_seen_index[stream_capability] - 1
                    interference = sorted({c["capability_id"] for c in caps if c["capability_id"] != stream_capability
                                           and any(positions[j][0] == caps.index(c)
                                                   for j in range(last_seen_index[stream_capability], index - 1))})
                    task["probe_spec"] = {
                        "target_capability_id": capability_id,
                        "expected_transfer_from_turn_ids": parents,
                        "interference_capability_ids": interference,
                        "probe_gap_turns": gap,
                        "claim_scope": {"same_benchmark": "within_benchmark_retention",
                                        "cross_benchmark": "cross_benchmark_shift_retention",
                                        "auto_research": "within_autoresearch_retention",
                                        "explicit_dependency": "within_autoresearch_retention"}[pool_info["pool_type"]],
                        "held_out_source_task_id": record["source"]["source_task_id"],
                    }
                    if transfer:
                        task["probe_spec"].update(
                            claim_scope="cross_benchmark_transfer",
                            transfer_source_capability_id=stream_capability,
                            transfer_group_id=cap["transfer_group_id"])
                write_json(turn_dir / "task.json", task)
                summaries.append({
                    "turn_id": turn_id, "turn_index": index, "turn_type": "capability_application",
                    "turn_origin": record.get("turn_origin", "source_task"), "sequence_role": role,
                    "source_task_id": record["source"]["source_task_id"], "capability_id": capability_id,
                    "runtime_profile_id": profile["runtime_profile_id"],
                })
                selected_turns.append({"turn_id": turn_id, "turn_index": index, "sequence_role": role,
                                       "source_id": f"{record['source']['benchmark_id']}:{record['source']['source_task_id']}",
                                       "pool_source_id": source_id, "source_hash": record["source"]["source_hash"],
                                       "capability_id": capability_id})
                # One catalog row per source identity: an explicit-dependency stage 2
                # shares its row with the native task it runs.
                identity = f"{record['source']['benchmark_id']}:{record['source']['source_task_id']}"
                catalog_rows.setdefault(identity, {
                    "source_id": identity, "source": task["source_basis"], "license": record["source"].get("license"),
                    "prompt": record["prompt"],
                    "benchmark_native_labels": record.get("benchmark_native_labels", {}),
                    "capability_id": capability_id, "runtime": record["runtime"],
                    "evaluation_profile": record["evaluation_profile"], "qualification": record["qualification"],
                })
                seen[stream_capability].append(turn_id)
                last_seen_index[stream_capability] = index
                placements += 1
            turn_count = len(positions)
            stream_profiles = [profiles[s["runtime_profile_id"]] for s in summaries]
            write_json(stream_dir / "stream.json", {
                "schema_version": TASK_SCHEMA, "stream_id": stream_id, "directory": stream_id,
                "profile": "L2 V5 multi-capability stream", "theme": stream_plan.get("theme", ""),
                "turn_count": turn_count, "task_layout": "turn_directories", "agent_workspace": "agent_workspace",
                "workspace_persistence": "stream", "turn_type_counts": {"capability_application": turn_count},
                "fact_thread_count": 0,
                "stream_design": {"pool_id": pool_id, "pool_type": pool_info["pool_type"],
                                  "composition_policy": pool_info["composition_policy"], "schedule": schedule,
                                  "tasks_per_capability": per_cap},
                "capabilities": capability_specs,
                "ordering": {"method": f"fixed_{schedule}", "seed": plan_doc["seed"],
                             "turn_order": [f"turn{i}" for i in range(1, turn_count + 1)],
                             "note": {"blocked": "A1..An, B1..Bn, C1..Cn",
                                      "interleaved": "A1 B1 C1 A2 B2 C2 ...",
                                      "delayed": "A1..A(n-1), B1..B(n-1), C1..C(n-1), Cn, Bn, An"}[schedule]},
                "execution_requirements": {
                    "network": "disabled", "path_scope": "agent_workspace", "max_interactions": 3,
                    "max_tool_calls_per_interaction": 20,
                    "turn_timeout_seconds": max(p["turn_timeout_seconds"] for p in stream_profiles),
                    "max_output_tokens": 12000,
                    "requires_gpu": any(p["requires_gpu"] for p in stream_profiles),
                },
                "turns": summaries, "license_scope": stream_plan.get("license_scope", "research_noncommercial"),
                "selection_provenance": {"plan_schema_version": PLAN_SCHEMA, "plan_sha256": plan_sha,
                                         "taxonomy_version": plan_doc["taxonomy_version"], "seed": plan_doc["seed"]},
            })
            write_json(stage / "selections" / f"{stream_id}.json", {
                "schema_version": "l2_selection_provenance/1.0", "stream_id": stream_id, "pool_id": pool_id,
                "schedule": schedule, "taxonomy_version": plan_doc["taxonomy_version"], "seed": plan_doc["seed"],
                "selected_turns": selected_turns,
            })
            pool_streams[pool_id].append(stream_id)
            manifest_streams.append({"stream_id": stream_id, "directory": stream_id, "turn_count": turn_count,
                                     "path": f"streams/{stream_id}/stream.json"})
        with (stage / "source_tasks.jsonl").open("w", encoding="utf-8") as handle:
            for key in sorted(catalog_rows):
                handle.write(json.dumps(catalog_rows[key], ensure_ascii=False, sort_keys=True) + "\n")
        write_json(stage / "runtime_profiles.json", {
            "schema_version": "l2_runtime_profiles/1.0",
            "profiles": [profiles[key] for key in sorted(profiles)]})
        shutil.copy2(plan_path, stage / "stream_plan.json")
        shutil.copy2(codebook_path, stage / "capability_codebook.json")
        used_pools = [pid for pid in POOLS if pool_streams[pid]]
        write_json(stage / "pools.json", {"schema_version": "l2_pools/1.0", "pools": [{
            "pool_id": pid, "pool_type": POOLS[pid]["pool_type"], "stream_ids": pool_streams[pid],
            "composition_policy": POOLS[pid]["composition_policy"],
            "license_scopes": ["research_noncommercial"], "allows_cross_pool_aggregation": False,
        } for pid in used_pools]})
        write_json(stage / "panels.json", {"schema_version": "l2_panels/1.0", "panels": [{
            "panel_id": f"{pid}_primary", "purpose": "primary", "pool_id": pid, "stream_ids": pool_streams[pid],
            "allows_cross_panel_aggregation": False,
        } for pid in used_pools]})
        write_json(stage / "taxonomy.json", {
            "schema_version": "l2_taxonomy/1.0", "taxonomy_version": plan_doc["taxonomy_version"],
            "capabilities": [taxonomy_used[key] for key in sorted(taxonomy_used)],
            "transfer_groups": [g for g in codebook.get("transfer_groups", [])
                                if len(set(g["capability_ids"]) & set(taxonomy_used)) >= 2]})
        manifest = {
            "schema_version": TASK_SCHEMA, "benchmark": plan_doc["package_id"],
            "aggregation_profile": "l2_evolution_aggregation/1.2", "workspace_persistence": "stream",
            "stream_count": len(manifest_streams), "total_turn_count": placements,
            "unique_source_task_count": len(catalog_rows), "turn_placement_count": placements,
            "streams": manifest_streams,
            "pool_manifest": binding(stage, "pools.json", "l2_pools/1.0"),
            "panel_manifest": binding(stage, "panels.json", "l2_panels/1.0"),
            "taxonomy_manifest": binding(stage, "taxonomy.json", "l2_taxonomy/1.0"),
            "runtime_profiles": binding(stage, "runtime_profiles.json", "l2_runtime_profiles/1.0"),
            "source_catalog": binding(stage, "source_tasks.jsonl", "l2_source_catalog/1.0"),
            "selection_manifest": binding(stage, "stream_plan.json", "l2_stream_plan/1.0"),
        }
        write_json(stage / "manifest.json", manifest)
        if out.exists():
            shutil.rmtree(out)
        shutil.move(str(stage), str(out))
    counts = Counter(s["pool_id"] for s in plan_doc["streams"])
    print(json.dumps({"streams": len(manifest_streams), "placements": placements,
                      "unique_sources": len(catalog_rows), "pools": counts, "profiles": sorted(profiles)}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--codebook", type=Path, required=True)
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compose")
    c.add_argument("--pool", type=Path, required=True)
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--codebook", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--rubrics", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        plan(args.pool, args.codebook, args.spec, args.out)
    else:
        compose(args.pool, args.plan, args.codebook, args.out, args.rubrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
