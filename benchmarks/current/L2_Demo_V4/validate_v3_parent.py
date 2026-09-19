#!/usr/bin/env python3
"""Validate every L2 Contract V1 demo Stream and native red/green fixture."""

from __future__ import annotations

import argparse
import json
import math
import os
import csv
import concurrent.futures
import hashlib
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
STANDALONE = (HERE / "contract" / "schemas").is_dir()
REPO = HERE.parent if HERE.name == "scripts" else HERE.parents[1]
PILOT = REPO / "l2_demo" / "contract_v1_pilot" if HERE.name == "scripts" else HERE
CONTRACT = PILOT / "contract" if STANDALONE else REPO / "docs" / "contracts" / "l2-v1"
SCHEMAS = CONTRACT / "schemas"
JUDGE_PROMPT = (CONTRACT / "prompts" / "l2_judge_prompt.md").read_text(encoding="utf-8")
JUDGE_PROMPT_V2 = (CONTRACT / "prompts" / "l2_judge_prompt_v2.md").read_text(encoding="utf-8")
SIMULATOR_PROMPT = (CONTRACT / "prompts" / "l2_user_simulator_prompt.md").read_text(encoding="utf-8")


def verifier_environment(**overrides: str) -> dict[str, str]:
    """Keep verifier smoke tests from mutating the generated data package."""
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **overrides}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--native-smoke",
        action="store_true",
        help="also run all package-local native red/green fixtures; requires a verifier environment supplied through L2_PILOT_VERIFIER_PYTHON",
    )
    args = parser.parse_args()
    validate_tracked_migration_manifest()
    validate_package_manifest()
    manifest = load_json(PILOT / "manifest.json")
    validate(manifest, "l2-manifest.schema.json")
    assert manifest["aggregation_profile"] == "l2_evolution_aggregation/1.0"
    assert manifest["stream_count"] == len(manifest["streams"])
    assert manifest["total_turn_count"] == sum(item["turn_count"] for item in manifest["streams"])
    assert manifest["stream_count"] == 6
    stream_counts = {item["stream_id"]: item["turn_count"] for item in manifest["streams"]}
    assert stream_counts == {
        "airs_sick_textual_entailment_accuracy_001": 20,
        "mlgym_titanic_tabular_classification_001": 20,
        "skilllearn_stock_data_visualization_001": 50,
        "skillsbench_data_to_d3_001": 50,
        "sra_bigcodebench_pandas_dataframe_viz_001": 100,
        "tau2_telecom_mobile_data_roaming_001": 50,
    }

    tools = load_json(PILOT / "tools.json")
    validate(tools, "l2-tools.schema.json")
    tool_names = set(tools["tools"])
    task_count = 0
    for stream_ref in manifest["streams"]:
        stream_dir = PILOT / "streams" / stream_ref["directory"]
        stream = load_json(PILOT / stream_ref["path"])
        validate(stream, "l2-stream.schema.json")
        assert stream["stream_id"] == stream_ref["stream_id"]
        assert stream["turn_count"] == stream_ref["turn_count"] == len(stream["turns"])
        assert sum(stream["turn_type_counts"].values()) == stream["turn_count"]
        assert stream["ordering"]["turn_order"] == [item["turn_id"] for item in stream["turns"]]
        validate_sequence_roles(stream)
        if stream["stream_id"] == "sra_bigcodebench_pandas_dataframe_viz_001":
            selection = load_json(stream_dir / "selection.json")
            source_items = load_json(stream_dir / "source_items.json")
            assert selection["target_turn_count"] == stream["turn_count"] == 100
            assert selection["ordering"]["method"] == stream["ordering"]["method"]
            assert selection["ordering"]["seed"] == stream["ordering"]["seed"]
            assert [item["instance_id"] for item in selection["selected_turns"]] == [
                item["source_task_id"] for item in stream["turns"]
            ]
            assert [
                item["turn_index"] for item in stream["turns"] if item["sequence_role"] == "probe"
            ] == list(range(10, 101, 10))
            assert source_items["schema_version"] == "l2_sra_materialized_source_items/1.0"
            assert source_items["source_revision"] == selection["source"]["revision"]
            assert [item["instance_id"] for item in source_items["items"]] == [
                item["instance_id"] for item in selection["selected_turns"]
            ]
            for source_item, selected_turn in zip(
                source_items["items"], selection["selected_turns"], strict=True
            ):
                assert sha256_text(source_item["question"]) == selected_turn["question_sha256"]
                assert sha256_text(source_item["eval_data"]["code_prompt"]) == selected_turn[
                    "code_prompt_sha256"
                ]
                assert sha256_text(source_item["eval_data"]["test"]) == selected_turn["tests_sha256"]
        else:
            validate_v3_selection(stream_dir, stream)
        validate_stream_tasks(stream_dir, stream, tool_names)
        task_count += stream["turn_count"]

    assert_no_symlinks(PILOT)
    if args.native_smoke:
        verifier_run_count = run_native_red_green_smokes(manifest)
        print(
            f"PASS: {manifest['stream_count']} Streams, "
            f"{task_count} Turns, schemas/prompts/transitions, and {verifier_run_count} native verifier fixture runs"
        )
    else:
        print(
            f"PASS: {manifest['stream_count']} Streams, {task_count} Turns, "
            "package hashes + schemas/prompts/transitions (native fixture smoke not requested)"
        )


def validate_sequence_roles(stream: dict) -> None:
    roles = {turn["sequence_role"] for turn in stream["turns"]}
    assert "learning" in roles
    if stream["turn_count"] >= 10:
        assert "probe" in roles
    assert roles <= {"learning", "application", "probe", "replay", "noise"}


def validate_v3_selection(stream_dir: Path, stream: dict) -> None:
    selection_path = stream_dir / "selection.json"
    assert selection_path.is_file(), selection_path
    selection = load_json(selection_path)
    assert selection["schema_version"] == "l2_v3_stream_selection/1.0"
    assert selection["stream_id"] == stream["stream_id"]
    assert selection["target_turn_count"] == stream["turn_count"]
    records = selection["selected_turns"]
    assert len(records) == stream["turn_count"]
    for index, (record, turn) in enumerate(zip(records, stream["turns"], strict=True), start=1):
        assert record["turn_index"] == index
        assert record["turn_id"] == turn["turn_id"] == f"turn{index}"
        for field in ("turn_type", "turn_origin", "sequence_role", "source_task_id"):
            assert record[field] == turn[field]
        task = load_json(stream_dir / record["turn_id"] / "task.json")
        assert task["source_basis"]["source_revision"] == selection["source"]["revision"]
        assert task["source_basis"]["source_task_id"] == record["source_task_id"]
        assert task["source_basis"]["parent_source_task_ids"] == record[
            "parent_source_task_ids"
        ]
    assert [record["turn_index"] for record in records if record["sequence_role"] == "probe"] == list(
        range(10, stream["turn_count"] + 1, 10)
    )

    stream_id = stream["stream_id"]
    source_count = sum(record["turn_origin"] == "source_task" for record in records)
    derived_count = len(records) - source_count
    expected_origin_counts = {
        "airs_sick_textual_entailment_accuracy_001": (0, 20),
        "mlgym_titanic_tabular_classification_001": (1, 19),
        "skilllearn_stock_data_visualization_001": (5, 45),
        "skillsbench_data_to_d3_001": (1, 49),
        "tau2_telecom_mobile_data_roaming_001": (50, 0),
    }
    assert (source_count, derived_count) == expected_origin_counts[stream_id]

    if stream_id == "tau2_telecom_mobile_data_roaming_001":
        assert len({record["source_task_id"] for record in records}) == 50
        for record in records:
            source_item = load_json(
                stream_dir / record["turn_id"] / "evaluation_assets" / "source_item.json"
            )
            assert source_item["id"] == record["source_task_id"]
            assert canonical_json_sha256(source_item) == record["item_sha256"]
            assert source_item["evaluation_criteria"]["reward_basis"] == ["ENV_ASSERTION"]
        return

    if stream_id == "skilllearn_stock_data_visualization_001":
        variants = set()
        for record in records:
            transform = record["transform"]
            if transform is None:
                continue
            key = (record["parent_source_task_id"], transform["record_offset"])
            assert key not in variants
            variants.add(key)
            manifest = load_json(
                stream_dir / record["turn_id"] / "evaluation_assets" / "derived_transform.json"
            )
            assert manifest["parent_source_task_id"] == record["parent_source_task_id"]
            assert manifest["transformation"]["record_offset"] == transform["record_offset"]
        assert len(variants) == 45
        return

    if stream_id == "skillsbench_data_to_d3_001":
        variants = set()
        for record in records:
            key = (record["output_format"], record["record_rotation"])
            assert key not in variants
            variants.add(key)
            if record["turn_origin"] == "derived_evolution_turn":
                manifest = load_json(
                    stream_dir / record["turn_id"] / "evaluation_assets" / "derived_transform.json"
                )
                assert manifest["output_format"] == record["output_format"]
                assert manifest["record_rotation"] == record["record_rotation"]
        assert len(variants) == 50
        return

    if stream_id == "airs_sick_textual_entailment_accuracy_001":
        all_source_indices = []
        for record in records:
            source_indices = record["source_row_indices"]
            assert len(source_indices) == record["row_count"]
            assert sha256_text(json.dumps(source_indices, separators=(",", ":"))) == record[
                "source_row_indices_sha256"
            ]
            all_source_indices.extend(source_indices)
            turn_dir = stream_dir / record["turn_id"]
            test_path = turn_dir / "assets" / "data" / "test.csv"
            labels_path = turn_dir / "evaluation_assets" / "test_labels.csv"
            test_rows = read_csv_rows(test_path)
            label_rows = read_csv_rows(labels_path)
            assert len(test_rows) == len(label_rows) == record["row_count"]
            assert "label" not in test_rows[0]
            assert list(label_rows[0]) == ["label"]
            assert sha256_file(test_path) == record["public_test_sha256"]
            assert sha256_file(labels_path) == record["private_labels_sha256"]
            assert not any(path.name == "test_labels.csv" for path in (turn_dir / "assets").rglob("*"))
            task = load_json(turn_dir / "task.json")
            assert math.isclose(
                task["evaluation"]["metric_profile"]["performance_bands"]["partial_min_inclusive"],
                record["partial_min_inclusive"],
                rel_tol=0,
                abs_tol=1e-15,
            )
        assert len(all_source_indices) == len(set(all_source_indices)) == 4906
        assert set(all_source_indices) == set(range(4906))
        return

    assert stream_id == "mlgym_titanic_tabular_classification_001"
    full_ids = set(records[0]["passenger_ids"])
    assert len(full_ids) == records[0]["row_count"] == 418
    derived_ids = []
    for record in records:
        turn_dir = stream_dir / record["turn_id"]
        test_path = turn_dir / "assets" / "data" / "test.csv"
        truth_path = turn_dir / "evaluation_assets" / "ground_truth.csv"
        test_rows = read_csv_rows(test_path)
        truth_rows = read_csv_rows(truth_path)
        test_ids = [int(row["PassengerId"]) for row in test_rows]
        truth_ids = [int(row["PassengerId"]) for row in truth_rows]
        assert test_ids == record["passenger_ids"]
        assert set(test_ids) == set(truth_ids)
        assert len(test_ids) == record["row_count"]
        assert "Survived" not in test_rows[0]
        assert sha256_file(test_path) == record["public_test_sha256"]
        assert sha256_file(truth_path) == record["private_ground_truth_sha256"]
        assert not any(path.name == "ground_truth.csv" for path in (turn_dir / "assets").rglob("*"))
        task = load_json(turn_dir / "task.json")
        assert math.isclose(
            task["evaluation"]["metric_profile"]["performance_bands"]["partial_min_inclusive"],
            record["partial_min_inclusive"],
            rel_tol=0,
            abs_tol=1e-15,
        )
        if record["turn_origin"] == "derived_evolution_turn":
            assert Counter(int(row["Survived"]) for row in truth_rows) == Counter(
                {0: 14, 1: 8}
            )
            derived_ids.extend(test_ids)
            assert (turn_dir / "evaluation_assets" / "derived_transform.json").is_file()
    assert len(derived_ids) == len(set(derived_ids)) == 418
    assert set(derived_ids) == full_ids


def validate_package_manifest() -> None:
    path = PILOT / "PACKAGE_MANIFEST.json"
    if not path.is_file():
        return
    package = load_json(path)
    assert package["schema_version"] == "l2_demo_package_manifest/1.0"
    assert package["package_name"] == "L2_Demo_V3"
    assert package["stream_count"] == 6
    assert package["turn_count"] == 290
    declared = {item["path"]: item for item in package["files"]}
    actual = {
        item.relative_to(PILOT).as_posix(): item
        for item in PILOT.rglob("*")
        if item.is_file()
        and item != path
        and "__pycache__" not in item.parts
        and item.suffix != ".pyc"
    }
    assert set(declared) == set(actual)
    assert package["file_count"] == len(declared)
    assert package["total_size_bytes"] == sum(item["size_bytes"] for item in declared.values())
    tree_digest = hashlib.sha256()
    for relative, physical in actual.items():
        assert declared[relative]["size_bytes"] == physical.stat().st_size
        assert declared[relative]["sha256"] == sha256_file(physical)
    for relative in sorted(declared):
        item = declared[relative]
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(str(item["size_bytes"]).encode("ascii"))
        tree_digest.update(b"\0")
        tree_digest.update(item["sha256"].encode("ascii"))
        tree_digest.update(b"\0")
    assert package["tree_sha256"] == tree_digest.hexdigest()


def validate_tracked_migration_manifest() -> None:
    if STANDALONE:
        return
    migration_root = REPO / "l2_demo"
    manifest = load_json(migration_root / "manifest.json")
    stream_counts = manifest["turn_count_by_stream"]
    assert manifest["stream_count"] == len(manifest["streams"]) == len(stream_counts)
    assert manifest["total_turn_count"] == sum(stream_counts.values())
    assert manifest["total_turn_count"] == sum(
        stream_ref["turn_count"] for stream_ref in manifest["streams"]
    )
    source_count = 0
    derived_count = 0
    for stream_ref in manifest["streams"]:
        stream_id = stream_ref["stream_id"]
        stream = load_json(migration_root / stream_ref["path"])
        assert stream["stream_id"] == stream_id
        assert stream["turn_count"] == stream_ref["turn_count"] == stream_counts[stream_id]
        origins: dict[str, int] = {}
        for turn in stream["turns"]:
            origins[turn["turn_origin"]] = origins.get(turn["turn_origin"], 0) + 1
        source_count += origins.get("source_task", 0)
        derived_count += origins.get("derived_evolution_turn", 0)
        extensions = stream["l2_extensions"]
        assert extensions["source_task_turn_count"] == origins.get("source_task", 0)
        assert extensions["derived_evolution_turn_count"] == origins.get(
            "derived_evolution_turn", 0
        )
    assert manifest["source_task_turn_count"] == source_count
    assert manifest["derived_evolution_turn_count"] == derived_count


def validate_stream_tasks(stream_dir: Path, stream: dict, tool_names: set[str]) -> None:
    materialized_workspace_paths = {
        path.relative_to(stream_dir).as_posix()
        for path in (stream_dir / "agent_workspace").rglob("*")
    }
    materialized_asset_hashes: dict[str, str] = {}
    seen_turns: set[str] = set()
    for expected_index, turn_ref in enumerate(stream["turns"], start=1):
        turn_id = turn_ref["turn_id"]
        assert turn_id == f"turn{expected_index}"
        assert turn_ref["turn_index"] == expected_index
        turn_dir = stream_dir / turn_id
        task = load_json(turn_dir / "task.json")
        validate(task, "l2-task.schema.json")
        assert task["stream_id"] == stream["stream_id"]
        assert task["turn_id"] == turn_id
        assert task["turn_index"] == expected_index
        assert task["sequence_role"] == turn_ref["sequence_role"]
        assert set(task["capability_relation"]["parent_turn_ids"]) <= seen_turns
        if task["sequence_role"] in {"probe", "replay"}:
            assert task["capability_relation"]["parent_turn_ids"]
            assert task["capability_relation"]["relation_type"] not in {"probe", "replay"}
        assert set(task["agent_input"]["available_tools"]) <= tool_names
        available_tools = set(task["agent_input"]["available_tools"])
        if stream["stream_id"].startswith("sra_"):
            assert available_tools == set()
            assert task["agent_input"]["assets"] == []
            assert task["evaluation"]["reference_context"]["submission_mode"] == "final_response"
            assert task["evaluation"]["expected_workspace_effect"] == {"required_outputs": []}
        elif stream["stream_id"].startswith(("airs_", "mlgym_")):
            assert available_tools == {"terminal", "filesystem"}
            assert task["evaluation"]["reference_context"]["submission_mode"] == "workspace_artifact"
        elif stream["stream_id"].startswith(("skilllearn_", "skillsbench_")):
            assert {"terminal", "filesystem", "browser"} <= available_tools
            assert task["evaluation"]["reference_context"]["submission_mode"] == "workspace_artifact"
            assert "agent_workspace/output/index.html" in task["evaluation"][
                "expected_workspace_effect"
            ]["required_outputs"]
        elif stream["stream_id"].startswith("tau2_"):
            assert task["evaluation"]["reference_context"]["submission_mode"] == "final_response"
            expected_calls = task["evaluation"]["expected_tool_call"]
            assert expected_calls["semantics"] == "reference_trajectory_not_hard_requirement"
            assert expected_calls["hard_requirement"] is False
            assert expected_calls["reward_basis"] == ["ENV_ASSERTION"]
        profile = task["evaluation"]["evaluation_profile"]
        auto_research = profile == "auto_research"
        assert profile == (
            "auto_research"
            if stream["stream_id"].startswith(("airs_", "mlgym_"))
            else "general"
        )
        assert task["evaluation"]["judge_config"]["judge_prompt"] == (
            JUDGE_PROMPT_V2 if auto_research else JUDGE_PROMPT
        )
        assert task["user_simulator"]["simulator_prompt"] == SIMULATOR_PROMPT
        assert math.isclose(
            sum(check["weight"] for check in task["evaluation"]["checks"]),
            1.0,
            abs_tol=1e-12,
        )
        checks = task["evaluation"]["checks"]
        assert [check["check_id"] for check in checks] == (
            ["auto_research_overall"]
            if auto_research
            else [
                "task_completion",
                "requirement_and_output_compliance",
                "evidence_and_side_effect_integrity",
            ]
        )
        assert all("score_source" not in check for check in checks)
        assert task["evaluation"]["success_policy"]["native_verifier_requirement"] == "valid_result_before_judge"
        if auto_research:
            bands = task["evaluation"]["metric_profile"]["performance_bands"]
            assert bands["partial_min_inclusive"] <= bands["full_min_inclusive"]
            route = task["evaluation"]["judge_config"]["rubric_route"]
            for rubric in (route["domain_rubric"], route["benchmark_rubric"]):
                assert rubric is not None
                assert (PILOT / rubric["path"]).is_file()
            assert task["evaluation"]["success_policy"]["low_metric_is_retryable"] is False
            assert task["user_simulator"]["trigger"] == "submission_invalid_or_unverifiable_and_budget_remaining"
        submission = task["evaluation"]["submission_contract"]
        if stream["stream_id"].startswith("tau2_"):
            assert submission["mode"] == "final_response"
            assert submission["payload_kind"] == "json_tool_trajectory"
        verifier_bindings = {
            binding["name"]: binding for binding in task["evaluation"]["native_verifier"]["input_bindings"]
        }
        assert submission["source_binding"] in verifier_bindings
        source_binding = verifier_bindings[submission["source_binding"]]
        if submission["mode"] == "workspace_artifact":
            assert source_binding["source"] == "workspace"
            assert submission["adapter"]["type"] == "identity"
        elif submission["mode"] in {"final_response", "tool_trajectory"}:
            assert source_binding == {
                "name": submission["source_binding"],
                "source": "interaction_record",
                "path": "final_response",
            }
            assert submission["adapter"]["type"] == "source_specific"
            assert (turn_dir / submission["adapter"]["entrypoint"]).is_file()
        apply_transition(
            turn_dir,
            task["workspace_transition"],
            materialized_workspace_paths,
            materialized_asset_hashes,
        )
        for asset in task["agent_input"]["assets"]:
            assert asset["path"] in materialized_workspace_paths
            physical = stream_dir / asset["path"]
            actual_hash = (
                hash_workspace_asset(physical, asset["kind"])
                if physical.exists()
                else materialized_asset_hashes[asset["path"]]
            )
            assert asset["sha256"] == actual_hash
        verifier = task["evaluation"]["native_verifier"]
        assert (turn_dir / verifier["entrypoint"]).is_file()
        for binding in verifier["input_bindings"]:
            if binding["source"] == "evaluation_assets":
                assert (turn_dir / binding["path"]).exists()
        public_blob = json.dumps(task["agent_input"], ensure_ascii=False)
        assert "evaluation_assets/" not in public_blob
        seen_turns.add(turn_id)


def apply_transition(
    turn_dir: Path,
    transition: dict,
    workspace_paths: set[str],
    workspace_asset_hashes: dict[str, str],
) -> None:
    for operation in transition["operations"]:
        target = operation["target"]
        if operation["op"] in {"copy", "create"}:
            assert target not in workspace_paths
        if operation["op"] == "copy":
            source = turn_dir / operation["source"]
            assert source.exists()
            workspace_paths.add(target)
            workspace_asset_hashes[target] = hash_workspace_asset(
                source, "directory" if source.is_dir() else "file"
            )
            if source.is_dir():
                for child in source.rglob("*"):
                    suffix = child.relative_to(source).as_posix()
                    workspace_paths.add(f"{target}/{suffix}")
        elif operation["op"] == "create":
            workspace_paths.add(target)
        elif operation["op"] == "apply_patch":
            assert target in workspace_paths
        else:
            raise AssertionError(f"unsupported transition operation: {operation['op']}")


def run_native_red_green_smokes(manifest: dict) -> int:
    default_python = REPO / "l2_demo" / "runtime" / ".venv" / "bin" / "python"
    if STANDALONE:
        default_python = Path(sys.executable)
    verifier_python = Path(os.environ.get("L2_PILOT_VERIFIER_PYTHON", default_python))
    assert verifier_python.is_file(), f"missing verifier Python: {verifier_python}"
    result_schema = load_json(SCHEMAS / "l2-native-verifier-result.schema.json")
    run_count = 0
    fixture_failures: list[tuple[str, int, str, str, dict]] = []
    for stream_ref in manifest["streams"]:
        stream_dir = PILOT / "streams" / stream_ref["directory"]
        if stream_ref["stream_id"].startswith("sra_"):
            sra_runs, sra_failures = run_sra_red_green_smokes(
                stream_dir, stream_ref["turn_count"], verifier_python, result_schema
            )
            run_count += sra_runs
            fixture_failures.extend(sra_failures)
            continue
        if stream_ref["stream_id"].startswith(("skilllearn_", "skillsbench_")):
            file_runs, file_failures = run_file_artifact_red_green_smokes(
                stream_dir, stream_ref["turn_count"], verifier_python
            )
            run_count += file_runs
            fixture_failures.extend(file_failures)
            continue
        with tempfile.TemporaryDirectory(prefix="l2-v1-validate-workspace-") as temp:
            workspace = Path(temp) / "agent_workspace"
            shutil.copytree(stream_dir / "agent_workspace", workspace)
            for index in range(1, stream_ref["turn_count"] + 1):
                turn_dir = stream_dir / f"turn{index}"
                assets = turn_dir / "evaluation_assets"
                task = load_json(turn_dir / "task.json")
                apply_transition_to_workspace(turn_dir, task["workspace_transition"], workspace)
                if stream_ref["stream_id"].startswith(("skilllearn_", "skillsbench_")):
                    process = subprocess.run(
                        [str(verifier_python), str(assets / "red_green_smoke.py")],
                        text=True,
                        capture_output=True,
                        timeout=480,
                        check=True,
                        env=verifier_environment(),
                    )
                    assert "PASS: file-artifact native verifier" in process.stdout
                    run_count += 2
                    continue
                if stream_ref["stream_id"].startswith("tau2_"):
                    fixture_names = ("known_correct_response.json", "known_broken_response.json")
                elif stream_ref["stream_id"].startswith("sra_"):
                    fixture_names = ("known_correct_response.py", "known_broken_response.py")
                else:
                    fixture_names = ("known_correct.py", "known_broken.py")
                fixture_cases = [
                    ("pass", fixture_names[0]),
                    ("fail", fixture_names[1]),
                ]
                if task["evaluation"].get("evaluation_profile") == "auto_research":
                    fixture_cases[1:1] = [
                        ("pass", "__partial_boundary_artifact__"),
                        ("pass", "__valid_low_artifact__"),
                    ]
                for expected, fixture_name in fixture_cases:
                    declared_bindings = task["evaluation"]["native_verifier"]["input_bindings"]
                    submission_binding = next(
                        binding for binding in declared_bindings if binding["name"] == "submission"
                    )
                    if (
                        task["evaluation"].get("evaluation_profile") == "auto_research"
                        and submission_binding["source"] == "workspace"
                    ):
                        artifact = workspace / Path(submission_binding["path"]).relative_to("agent_workspace")
                        artifact.parent.mkdir(parents=True, exist_ok=True)
                        artifact.unlink(missing_ok=True)
                        if fixture_name in {"__partial_boundary_artifact__", "__valid_low_artifact__"}:
                            data_dir = workspace / "episodes" / f"E{index:03d}" / "data"
                            if stream_ref["stream_id"].startswith("airs_"):
                                with (data_dir / "test.csv").open(newline="", encoding="utf-8") as handle:
                                    row_count = sum(1 for _ in csv.DictReader(handle))
                                label_rows = read_csv_rows(assets / "test_labels.csv")
                                counts = Counter(int(row["label"]) for row in label_rows)
                                ordered = sorted(counts, key=lambda label: (-counts[label], label))
                                with artifact.open("w", newline="", encoding="utf-8") as handle:
                                    writer = csv.writer(handle)
                                    writer.writerow(["label"])
                                    label = ordered[0] if fixture_name == "__partial_boundary_artifact__" else ordered[-1]
                                    writer.writerows([[label]] * row_count)
                            else:
                                with (data_dir / "test.csv").open(newline="", encoding="utf-8") as handle:
                                    passenger_ids = [row["PassengerId"] for row in csv.DictReader(handle)]
                                truth_rows = read_csv_rows(assets / "ground_truth.csv")
                                counts = Counter(int(row["Survived"]) for row in truth_rows)
                                ordered = sorted(counts, key=lambda label: (-counts[label], label))
                                with artifact.open("w", newline="", encoding="utf-8") as handle:
                                    writer = csv.writer(handle)
                                    writer.writerow(["PassengerId", "Survived"])
                                    survived = ordered[0] if fixture_name == "__partial_boundary_artifact__" else ordered[-1]
                                    writer.writerows([[passenger_id, survived] for passenger_id in passenger_ids])
                        elif expected == "pass":
                            process = subprocess.run(
                                [str(verifier_python), str(assets / fixture_name)],
                                cwd=workspace,
                                text=True,
                                capture_output=True,
                                timeout=300,
                                check=True,
                                env=verifier_environment(),
                            )
                            generated = workspace / "submission.csv"
                            assert generated.is_file(), (turn_dir, process.stdout, process.stderr)
                            generated.replace(artifact)
                        else:
                            data_dir = workspace / "episodes" / f"E{index:03d}" / "data"
                            if stream_ref["stream_id"].startswith("airs_"):
                                with (data_dir / "test.csv").open(newline="", encoding="utf-8") as handle:
                                    row_count = sum(1 for _ in csv.DictReader(handle))
                                with artifact.open("w", newline="", encoding="utf-8") as handle:
                                    writer = csv.writer(handle)
                                    writer.writerow(["label"])
                                    writer.writerows([[999]] * row_count)
                            else:
                                with (data_dir / "test.csv").open(newline="", encoding="utf-8") as handle:
                                    passenger_ids = [row["PassengerId"] for row in csv.DictReader(handle)]
                                with artifact.open("w", newline="", encoding="utf-8") as handle:
                                    writer = csv.writer(handle)
                                    writer.writerow(["PassengerId", "Survived"])
                                    writer.writerows([[passenger_ids[0], 0]] * len(passenger_ids))
                    bindings = {}
                    for binding in declared_bindings:
                        if binding["source"] == "interaction_record":
                            bindings[binding["name"]] = {
                                "kind": "inline",
                                "value": (assets / fixture_name).read_text(encoding="utf-8"),
                            }
                        else:
                            bindings[binding["name"]] = {
                                "kind": "path",
                                "path": binding["path"],
                            }
                    request = {
                        "schema_version": "l2_native_verifier_request/1.0",
                        "bindings": bindings,
                    }
                    validate(request, "l2-native-verifier-request.schema.json")
                    assert set(bindings) == {binding["name"] for binding in declared_bindings}
                    process = subprocess.run(
                        [str(verifier_python), str(assets / "verifier_cli.py")],
                        input=json.dumps(request),
                        text=True,
                        capture_output=True,
                        timeout=360,
                        check=True,
                        env=verifier_environment(L2_VERIFIER_WORKSPACE=str(workspace)),
                    )
                    result = json.loads(process.stdout)
                    Draft202012Validator(result_schema).validate(result)
                    if result["status"] != expected:
                        fixture_failures.append((
                            stream_ref["stream_id"], index, fixture_name, expected, result
                        ))
                        run_count += 1
                        continue
                    assert 0.0 <= result["score"] <= 1.0
                    if task["evaluation"].get("evaluation_profile") == "auto_research":
                        if expected == "pass":
                            assert result["metric"]["normalized_value"] == result["score"]
                            assert result["metric"]["name"] == task["evaluation"]["metric_profile"]["metric_name"]
                        else:
                            assert "metric" not in result
                        if fixture_name == "__valid_low_artifact__":
                            partial = task["evaluation"]["metric_profile"]["performance_bands"]["partial_min_inclusive"]
                            assert result["status"] == "pass"
                            assert result["score"] < partial
                        if fixture_name == "__partial_boundary_artifact__":
                            partial = task["evaluation"]["metric_profile"]["performance_bands"]["partial_min_inclusive"]
                            assert result["status"] == "pass"
                            assert math.isclose(result["score"], partial, rel_tol=0, abs_tol=1e-15)
                            assert result["score"] >= partial
                        if fixture_name == "known_correct.py":
                            full = task["evaluation"]["metric_profile"]["performance_bands"]["full_min_inclusive"]
                            assert result["score"] >= full
                    run_count += 1
                if task["evaluation"].get("evaluation_profile") == "auto_research":
                    carrier_bindings = {
                        binding["name"]: (
                            {"kind": "inline", "value": "print('carrier bypass')"}
                            if binding["name"] == "submission"
                            else {"kind": "path", "path": binding["path"]}
                        )
                        for binding in declared_bindings
                    }
                    carrier_request = {
                        "schema_version": "l2_native_verifier_request/1.0",
                        "bindings": carrier_bindings,
                    }
                    validate(carrier_request, "l2-native-verifier-request.schema.json")
                    process = subprocess.run(
                        [str(verifier_python), str(assets / "verifier_cli.py")],
                        input=json.dumps(carrier_request),
                        text=True,
                        capture_output=True,
                        timeout=60,
                        check=True,
                        env=verifier_environment(L2_VERIFIER_WORKSPACE=str(workspace)),
                    )
                    carrier_result = json.loads(process.stdout)
                    Draft202012Validator(result_schema).validate(carrier_result)
                    assert carrier_result["status"] == "error"
                    assert "declared submission carrier" in carrier_result["error"]
                    run_count += 1
    assert not fixture_failures, fixture_failures
    return run_count


def run_file_artifact_red_green_smokes(
    stream_dir: Path,
    turn_count: int,
    verifier_python: Path,
) -> tuple[int, list[tuple[str, int, str, str, dict]]]:
    """Run independent browser-artifact red/green fixtures concurrently."""

    stream_id = stream_dir.name

    def run_turn(index: int) -> list[tuple[str, int, str, str, dict]]:
        smoke = stream_dir / f"turn{index}" / "evaluation_assets" / "red_green_smoke.py"
        process = subprocess.run(
            [str(verifier_python), str(smoke)],
            text=True,
            capture_output=True,
            timeout=480,
            check=False,
            env=verifier_environment(),
        )
        if process.returncode == 0 and "PASS: file-artifact native verifier" in process.stdout:
            return []
        return [(
            stream_id,
            index,
            "red_green_smoke.py",
            "pass_then_fail",
            {
                "returncode": process.returncode,
                "stdout_tail": process.stdout[-4000:],
                "stderr_tail": process.stderr[-4000:],
            },
        )]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        per_turn_failures = list(executor.map(run_turn, range(1, turn_count + 1)))
    return turn_count * 2, [failure for failures in per_turn_failures for failure in failures]


def run_sra_red_green_smokes(
    stream_dir: Path,
    turn_count: int,
    verifier_python: Path,
    result_schema: dict,
) -> tuple[int, list[tuple[str, int, str, str, dict]]]:
    """Run independent response-only SRA fixtures concurrently."""

    stream_id = stream_dir.name

    def run_turn(index: int) -> list[tuple[str, int, str, str, dict]]:
        turn_dir = stream_dir / f"turn{index}"
        assets = turn_dir / "evaluation_assets"
        task = load_json(turn_dir / "task.json")
        declared_bindings = task["evaluation"]["native_verifier"]["input_bindings"]
        failures = []
        for expected, fixture_name in (
            ("pass", "known_correct_response.py"),
            ("fail", "known_broken_response.py"),
        ):
            bindings = {
                binding["name"]: (
                    {
                        "kind": "inline",
                        "value": (assets / fixture_name).read_text(encoding="utf-8"),
                    }
                    if binding["source"] == "interaction_record"
                    else {"kind": "path", "path": binding["path"]}
                )
                for binding in declared_bindings
            }
            request = {
                "schema_version": "l2_native_verifier_request/1.0",
                "bindings": bindings,
            }
            validate(request, "l2-native-verifier-request.schema.json")
            process = subprocess.run(
                [str(verifier_python), str(assets / "verifier_cli.py")],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=360,
                check=True,
                env=verifier_environment(
                    L2_VERIFIER_WORKSPACE=str(stream_dir / "agent_workspace")
                ),
            )
            result = json.loads(process.stdout)
            Draft202012Validator(result_schema).validate(result)
            if result["status"] != expected:
                failures.append((stream_id, index, fixture_name, expected, result))
                continue
            assert 0.0 <= result["score"] <= 1.0
        return failures

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        per_turn_failures = list(executor.map(run_turn, range(1, turn_count + 1)))
    return turn_count * 2, [failure for failures in per_turn_failures for failure in failures]


def apply_transition_to_workspace(turn_dir: Path, transition: dict, workspace: Path) -> None:
    for operation in transition["operations"]:
        target_relative = Path(operation["target"]).relative_to("agent_workspace")
        target = workspace / target_relative
        if operation["op"] == "copy":
            source = turn_dir / operation["source"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        elif operation["op"] == "create":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(operation["content"], encoding="utf-8")
        else:
            raise AssertionError("red/green validator does not apply_patch fixtures")


def hash_workspace_asset(path: Path, kind: str) -> str:
    import hashlib

    if kind == "file":
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def assert_no_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        assert not path.is_symlink(), f"release contains symlink: {path}"


def validate(instance: object, schema_name: str) -> None:
    schema = load_json(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(instance)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    main()
