#!/usr/bin/env python3
"""Validate a diversified L2 package without fixed stream or role counts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_legacy_helpers(root: Path):
    path = root / "validate_v3_parent.py"
    spec = importlib.util.spec_from_file_location("validate_v3_parent", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import validation helpers: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_package_manifest(root: Path, manifest: dict) -> None:
    path = root / "PACKAGE_MANIFEST.json"
    package = load_json(path)
    assert package["schema_version"] == "l2_demo_package_manifest/1.0"
    assert package["package_name"] == root.name
    assert package["stream_count"] == manifest["stream_count"]
    assert package["turn_count"] == manifest["total_turn_count"]
    declared = {item["path"]: item for item in package["files"]}
    actual = {
        item.relative_to(root).as_posix(): item
        for item in root.rglob("*")
        if item.is_file()
        and item != path
        and "__pycache__" not in item.parts
        and item.suffix != ".pyc"
    }
    assert set(declared) == set(actual)
    assert package["file_count"] == len(declared)
    assert package["total_size_bytes"] == sum(item["size_bytes"] for item in declared.values())
    tree_digest = hashlib.sha256()
    for relative in sorted(declared):
        physical = actual[relative]
        record = declared[relative]
        assert record["size_bytes"] == physical.stat().st_size
        assert record["sha256"] == sha256_file(physical)
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(str(record["size_bytes"]).encode("ascii"))
        tree_digest.update(b"\0")
        tree_digest.update(record["sha256"].encode("ascii"))
        tree_digest.update(b"\0")
    assert package["tree_sha256"] == tree_digest.hexdigest()


def validate_shard(root: Path, stream_ref: dict, stream: dict) -> None:
    stream_dir = root / "streams" / stream_ref["directory"]
    selection = load_json(stream_dir / "selection.json")
    derivation = selection["derivation"]
    lineage = selection["turn_lineage"]
    assert selection["stream_id"] == stream["stream_id"]
    assert selection["target_turn_count"] == stream["turn_count"]
    assert derivation["kind"] == "independent_stream_from_parent_turn_subset"
    assert derivation["parent_stream_version"] == "L2_Demo_V3"
    assert derivation["runtime_dependency_on_parent"] is False
    assert derivation["preserves_parent_order"] is True
    assert len(lineage) == len(stream["turns"])
    learning = []
    probes = []
    seen = set()
    for index, (turn_ref, record, link) in enumerate(
        zip(stream["turns"], selection["selected_turns"], lineage, strict=True), start=1
    ):
        turn_id = f"turn{index}"
        assert turn_ref["turn_id"] == record["turn_id"] == link["turn_id"] == turn_id
        assert turn_ref["turn_index"] == record["turn_index"] == index
        assert turn_ref["sequence_role"] == record["sequence_role"]
        record_source_task_id = record.get("source_task_id") or record.get("instance_id")
        assert turn_ref["source_task_id"] == record_source_task_id == link.get("source_task_id")
        assert record["parent_turn_id"] == link["parent_turn_id"]
        task = load_json(stream_dir / turn_id / "task.json")
        assert task["stream_id"] == stream["stream_id"]
        assert task["turn_id"] == turn_id and task["turn_index"] == index
        assert task["sequence_role"] == turn_ref["sequence_role"]
        parents = task["capability_relation"]["parent_turn_ids"]
        assert set(parents) <= seen
        if task["sequence_role"] == "learning":
            learning.append(index)
        if task["sequence_role"] == "probe":
            probes.append(index)
            assert parents
            assert "API" in task["capability_relation"]["expected_transfer"] or "workflow" in task["capability_relation"]["expected_transfer"]
        seen.add(turn_id)
    policy = selection["sequence_role_policy"]
    assert learning == policy["learning"]
    assert probes == policy["probe"]
    assert len(learning) >= 1 and len(probes) >= 2
    assert any(probe - learning[0] >= (8 if stream["turn_count"] == 20 else 6) for probe in probes)
    assert any(probe % 5 != 0 for probe in probes)


def validate_lineage_groups(root: Path, manifest: dict) -> None:
    parents = {
        item["stream_id"]: item
        for item in manifest["streams"]
        if "__shard_" not in item["stream_id"]
    }
    groups: dict[str, list[dict]] = {}
    schedules: dict[str, set[tuple[tuple[int, ...], tuple[int, ...]]]] = {}
    for stream_ref in manifest["streams"]:
        if "__shard_" not in stream_ref["stream_id"]:
            continue
        selection = load_json(root / "streams" / stream_ref["directory"] / "selection.json")
        parent_id = selection["derivation"]["parent_stream_id"]
        groups.setdefault(parent_id, []).append(selection)
        policy = selection["sequence_role_policy"]
        schedules.setdefault(parent_id, set()).add((tuple(policy["learning"]), tuple(policy["probe"])))
    assert set(groups) == set(parents)
    for parent_id, selections in groups.items():
        expected = {f"turn{index}" for index in range(1, parents[parent_id]["turn_count"] + 1)}
        observed: list[str] = []
        for selection in selections:
            observed.extend(item["parent_turn_id"] for item in selection["turn_lineage"])
        assert len(observed) == len(set(observed))
        assert set(observed) == expected
        assert len(schedules[parent_id]) > 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--native-smoke", action="store_true")
    args = parser.parse_args()
    root = args.package.resolve()
    helpers = load_legacy_helpers(root)
    manifest = load_json(root / "manifest.json")
    Draft202012Validator(load_json(root / "contract/schemas/l2-manifest.schema.json")).validate(manifest)
    assert manifest["stream_count"] == len(manifest["streams"]) == 30
    assert manifest["total_turn_count"] == sum(item["turn_count"] for item in manifest["streams"]) == 580
    assert Counter(item["turn_count"] for item in manifest["streams"]) == Counter({10: 19, 20: 7, 50: 3, 100: 1})
    tool_names = set(load_json(root / "tools.json")["tools"])
    task_count = 0
    for stream_ref in manifest["streams"]:
        stream = load_json(root / stream_ref["path"])
        helpers.validate(stream, "l2-stream.schema.json")
        assert stream["stream_id"] == stream_ref["stream_id"]
        assert stream["turn_count"] == stream_ref["turn_count"] == len(stream["turns"])
        assert stream["ordering"]["turn_order"] == [item["turn_id"] for item in stream["turns"]]
        helpers.validate_sequence_roles(stream)
        helpers.validate_stream_tasks(root / "streams" / stream_ref["directory"], stream, tool_names)
        if "__shard_" in stream["stream_id"]:
            validate_shard(root, stream_ref, stream)
        task_count += stream["turn_count"]
    validate_lineage_groups(root, manifest)
    validate_package_manifest(root, manifest)
    helpers.assert_no_symlinks(root)
    if args.native_smoke:
        runs = helpers.run_native_red_green_smokes(manifest)
        print(f"PASS: {manifest['stream_count']} Streams, {task_count} Turns, {runs} native fixture runs")
    else:
        print(f"PASS: {manifest['stream_count']} Streams, {task_count} Turns, schemas, lineage, roles, paths, and package hashes")


if __name__ == "__main__":
    main()
