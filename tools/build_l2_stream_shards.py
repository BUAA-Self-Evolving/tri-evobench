#!/usr/bin/env python3
"""Build a deterministic diversified L2 package from frozen L2 Demo V3."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import zipfile
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.repair_l2_runtime_contracts import repair as repair_runtime_contracts

DEFAULT_DESIGN = HERE / "l2_stream_shard_design.json"
VALIDATOR = HERE / "validate_l2_stream_shards.py"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_records(root: Path, excluded: set[Path] | None = None) -> tuple[list[dict], str]:
    excluded = excluded or set()
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path in excluded or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        records.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    digest = hashlib.sha256()
    for item in records:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["size_bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\0")
    return records, digest.hexdigest()


def copy_root(source: Path, output: Path) -> None:
    output.mkdir(parents=True)
    for item in source.iterdir():
        if item.name in {"streams", "PACKAGE_MANIFEST.json"}:
            continue
        target = output / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    shutil.copytree(source / "streams", output / "streams")
    shutil.copy2(source / "validate_pilot.py", output / "validate_v3_parent.py")
    shutil.copy2(VALIDATOR, output / "validate_pilot.py")


def role_for(index: int, schedule: dict) -> str:
    if index in schedule["learning"]:
        return "learning"
    if index in schedule["probe"]:
        return "probe"
    return "application"


def capability_relation(capability_id: str, index: int, schedule: dict) -> dict:
    earlier_learning = [item for item in schedule["learning"] if item < index]
    role = role_for(index, schedule)
    if role == "learning" and not earlier_learning:
        return {
            "capability_id": capability_id,
            "relation_type": "foundation",
            "parent_turn_ids": [],
            "expected_transfer": f"Establish the {capability_id} API and artifact workflow for this independent Stream.",
        }
    parents = earlier_learning if role == "probe" else earlier_learning[-1:]
    relation = "reinforcement" if role == "learning" else ("delayed_transfer" if role == "probe" else "application")
    return {
        "capability_id": capability_id,
        "relation_type": relation,
        "parent_turn_ids": [f"turn{item}" for item in parents],
        "expected_transfer": (
            f"Reuse the {capability_id} API and artifact workflow established in "
            f"{', '.join(f'turn{item}' for item in parents)} while solving this distinct source item."
        ),
    }


def hash_workspace_asset(path: Path, kind: str) -> str:
    if kind == "file":
        return sha256_file(path)
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def rebind_episode_paths(turn_dir: Path, parent_index: int, local_index: int) -> None:
    old = f"E{parent_index:03d}"
    new = f"E{local_index:03d}"
    if old == new:
        return
    for path in turn_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old in content:
            path.write_text(content.replace(old, new), encoding="utf-8")


def patch_task(
    path: Path,
    stream_id: str,
    local_index: int,
    schedule: dict,
    capability_id: str,
    parent_stream_dir: Path,
) -> None:
    task = load_json(path)
    turn_dir = path.parent
    if local_index == 1:
        seed = turn_dir.parent / "agent_workspace"
        introduced = {
            operation["target"]
            for operation in task["workspace_transition"]["operations"]
            if operation["op"] in {"copy", "create"}
        }
        for asset in task["agent_input"]["assets"]:
            target = asset["path"]
            seed_path = seed / Path(target).relative_to("agent_workspace")
            if target in introduced or seed_path.exists():
                continue
            source_path = turn_dir / "assets" / Path(target).name
            if not source_path.exists():
                matches = []
                for candidate in sorted(parent_stream_dir.glob(f"turn*/assets/{Path(target).name}")):
                    if hash_workspace_asset(candidate, asset["kind"]) == asset["sha256"]:
                        matches.append(candidate)
                if not matches:
                    raise FileNotFoundError(
                        f"cannot materialize first-Turn asset {target} from {parent_stream_dir}"
                    )
                if matches[0].is_dir():
                    shutil.copytree(matches[0], source_path)
                else:
                    shutil.copy2(matches[0], source_path)
            task["workspace_transition"]["operations"].append({
                "op": "copy",
                "source": source_path.relative_to(turn_dir).as_posix(),
                "target": target,
            })
            task["workspace_transition"]["mode"] = "continue_with_patch"
    task["stream_id"] = stream_id
    task["turn_id"] = f"turn{local_index}"
    task["turn_index"] = local_index
    task["sequence_role"] = role_for(local_index, schedule)
    task["capability_relation"] = capability_relation(capability_id, local_index, schedule)
    verifier = task["evaluation"]["native_verifier"]
    verifier["verifier_id"] = f"{stream_id}_turn{local_index}_native_verifier"
    source_by_target = {
        operation["target"]: operation["source"]
        for operation in task["workspace_transition"]["operations"]
        if operation["op"] == "copy"
    }
    for asset in task["agent_input"]["assets"]:
        source = source_by_target.get(asset["path"])
        if source:
            asset["sha256"] = hash_workspace_asset(turn_dir / source, asset["kind"])
    write_json(path, task)


def build_shard(output: Path, parent_stream_id: str, shard_index: int, shard_count: int, shard_size: int, schedule: dict) -> dict:
    streams = output / "streams"
    parent_dir = streams / parent_stream_id
    shard_id = f"{parent_stream_id}__shard_{shard_index:02d}_of_{shard_count:02d}"
    shard_dir = streams / shard_id
    shard_dir.mkdir()
    first_parent_index = (shard_index - 1) * shard_size + 1
    parent_indices = list(range(first_parent_index, first_parent_index + shard_size))
    for item in parent_dir.iterdir():
        if item.is_dir() and item.name.startswith("turn"):
            continue
        target = shard_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    parent_stream = load_json(parent_dir / "stream.json")
    selected_parent_refs = [parent_stream["turns"][index - 1] for index in parent_indices]
    local_refs = []
    for local_index, (parent_index, parent_ref) in enumerate(zip(parent_indices, selected_parent_refs, strict=True), start=1):
        shutil.copytree(parent_dir / f"turn{parent_index}", shard_dir / f"turn{local_index}")
        rebind_episode_paths(shard_dir / f"turn{local_index}", parent_index, local_index)
        patch_task(
            shard_dir / f"turn{local_index}" / "task.json",
            shard_id,
            local_index,
            schedule,
            parent_stream["capability"]["capability_id"],
            parent_dir,
        )
        local_ref = dict(parent_ref)
        local_ref.update({"turn_id": f"turn{local_index}", "turn_index": local_index, "sequence_role": role_for(local_index, schedule)})
        local_refs.append(local_ref)
    stream = dict(parent_stream)
    stream.update({
        "stream_id": shard_id,
        "directory": shard_id,
        "theme": parent_stream["theme"] + " This independent shard starts from a blank Stream workspace.",
        "turn_count": shard_size,
        "ordering": {
            "method": "fixed",
            "seed": None,
            "turn_order": [f"turn{index}" for index in range(1, shard_size + 1)],
            "note": f"Preserves parent Turns {first_parent_index}-{parent_indices[-1]} and renumbers them locally.",
        },
        "turns": local_refs,
    })
    stream["turn_type_counts"] = dict(Counter(item["turn_type"] for item in local_refs))
    write_json(shard_dir / "stream.json", stream)
    selection = load_json(parent_dir / "selection.json")
    parent_records = selection["selected_turns"][first_parent_index - 1:parent_indices[-1]]
    selected_records = []
    lineage = []
    for local_index, (parent_index, record) in enumerate(zip(parent_indices, parent_records, strict=True), start=1):
        local_record = dict(record)
        local_record.update({
            "turn_id": f"turn{local_index}",
            "turn_index": local_index,
            "sequence_role": role_for(local_index, schedule),
            "parent_turn_id": f"turn{parent_index}",
            "parent_turn_index": parent_index,
        })
        selected_records.append(local_record)
        lineage.append({
            "turn_id": f"turn{local_index}",
            "parent_turn_id": f"turn{parent_index}",
            "source_task_id": record.get("source_task_id") or record.get("instance_id"),
        })
    selection.update({
        "stream_id": shard_id,
        "target_turn_count": shard_size,
        "sequence_role_policy": {
            "learning": schedule["learning"],
            "probe": schedule["probe"],
            "application": "all remaining turn indices",
            "probe_basis": schedule["rationale"],
            "replay_and_noise": "not used in this release",
        },
        "selected_turns": selected_records,
        "derivation": {
            "kind": "independent_stream_from_parent_turn_subset",
            "parent_stream_id": parent_stream_id,
            "parent_stream_version": "L2_Demo_V3",
            "source_turn_range": [first_parent_index, parent_indices[-1]],
            "shard_index": shard_index,
            "shard_count": shard_count,
            "preserves_parent_order": True,
            "runtime_dependency_on_parent": False,
            "schedule_rationale": schedule["rationale"],
        },
        "turn_lineage": lineage,
    })
    if "ordering" in selection:
        selection["ordering"] = {"method": "fixed", "seed": None}
    write_json(shard_dir / "selection.json", selection)
    source_items_path = shard_dir / "source_items.json"
    if source_items_path.is_file():
        source_items = load_json(source_items_path)
        source_items["items"] = source_items["items"][first_parent_index - 1:parent_indices[-1]]
        write_json(source_items_path, source_items)
    (shard_dir / "README.md").write_text(
        f"# {shard_id}\n\nIndependent {shard_size}-Turn shard derived from `{parent_stream_id}` "
        f"parent Turns {first_parent_index}-{parent_indices[-1]}. Runtime dependency on the parent is false. "
        "See `selection.json` for exact lineage and role design.\n",
        encoding="utf-8",
    )
    return {
        "stream_id": shard_id,
        "directory": shard_id,
        "turn_count": shard_size,
        "path": f"streams/{shard_id}/stream.json",
    }


def build_package(source: Path, output: Path, design_path: Path) -> None:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    design = load_json(design_path)
    source_manifest = load_json(source / "manifest.json")
    source_refs = {item["stream_id"]: item for item in source_manifest["streams"]}
    assert set(source_refs) == {item["parent_stream_id"] for item in design["parents"]}
    copy_root(source, output)
    parent_hashes = {}
    shard_refs = []
    expanded_design = []
    for parent in design["parents"]:
        parent_id = parent["parent_stream_id"]
        parent_dir = source / "streams" / parent_id
        records, tree_hash = tree_records(parent_dir)
        parent_hashes[parent_id] = {"file_count": len(records), "tree_sha256": tree_hash}
        shard_size = parent["shard_size"]
        expected_count = source_refs[parent_id]["turn_count"] // shard_size
        assert expected_count == len(parent["schedules"])
        for shard_index, schedule in enumerate(parent["schedules"], start=1):
            shard_refs.append(build_shard(output, parent_id, shard_index, expected_count, shard_size, schedule))
            expanded_design.append({
                "stream_id": shard_refs[-1]["stream_id"],
                "parent_stream_id": parent_id,
                "source_turn_ids": [
                    f"turn{index}" for index in range((shard_index - 1) * shard_size + 1, shard_index * shard_size + 1)
                ],
                "turn_order": [f"turn{index}" for index in range(1, shard_size + 1)],
                "learning_turn_indices": schedule["learning"],
                "probe_turn_indices": schedule["probe"],
                "schedule_rationale": schedule["rationale"],
            })
    manifest = dict(source_manifest)
    manifest.update({
        "benchmark": "L2-TaskStream-Contract-V1-Demo-V4-Diversified",
        "aggregation_profile": "l2_evolution_aggregation/1.1",
        "stream_count": len(source_manifest["streams"]) + len(shard_refs),
        "total_turn_count": source_manifest["total_turn_count"] + sum(item["turn_count"] for item in shard_refs),
        "streams": source_manifest["streams"] + shard_refs,
    })
    write_json(output / "manifest.json", manifest)
    write_json(output / "parent_baseline.json", {
        "schema_version": "l2_parent_baseline/1.0",
        "parent_package": source.name,
        "parent_package_manifest_sha256": sha256_file(source / "PACKAGE_MANIFEST.json"),
        "streams": parent_hashes,
    })
    write_json(output / "shard_design.json", {
        "schema_version": "l2_stream_shard_design_expanded/1.0",
        "source_design_sha256": sha256_file(design_path),
        "streams": expanded_design,
    })
    write_json(output / "panels.json", {
        "schema_version": "l2_panels/1.0",
        "short_stream_main_panel": [item["stream_id"] for item in shard_refs],
        "parent_stream_diagnostic": [item["stream_id"] for item in source_manifest["streams"]],
        "unique_parent_turn_count": source_manifest["total_turn_count"],
        "turn_placement_count": manifest["total_turn_count"],
        "aggregation_rule": "Report panels separately; do not combine their denominators or overall means.",
    })
    write_json(output / "contract" / "schemas" / "l2-panels.schema.json", {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://self-evolving-benchmark.local/schemas/l2-panels.schema.json",
        "title": "L2 Panels V1",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "short_stream_main_panel", "parent_stream_diagnostic", "aggregation_rule"],
        "properties": {
            "schema_version": {"const": "l2_panels/1.0"},
            "short_stream_main_panel": {"$ref": "#/$defs/panel_streams"},
            "parent_stream_diagnostic": {"$ref": "#/$defs/panel_streams"},
            "unique_parent_turn_count": {"type": "integer", "minimum": 1},
            "turn_placement_count": {"type": "integer", "minimum": 1},
            "aggregation_rule": {"type": "string", "minLength": 1},
        },
        "$defs": {
            "panel_streams": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            },
        },
    })
    (output / "README.md").write_text(
        "# L2 Demo V4 - diversified independent Streams\n\n"
        "This package preserves all six frozen L2 Demo V3 parent Streams and adds 24 independent 10/20-Turn Streams. "
        "The short-stream main panel and parent diagnostic panel must be reported separately because they reuse the same 290 parent Turns. "
        "There are 30 Streams and 580 Turn placements, not 580 unique source tasks.\n\n"
        "Each shard starts from its own copied blank `agent_workspace`, carries all task and private verifier assets, and has no runtime dependency on its parent. "
        "See `shard_design.json`, each shard's `selection.json`, and `panels.json`.\n\n"
        "Validate after fresh extraction with `python validate_pilot.py`. Add `--native-smoke` for all red/green fixtures.\n",
        encoding="utf-8",
    )
    repair_runtime_contracts(output, check=False)
    package_manifest_path = output / "PACKAGE_MANIFEST.json"
    records, tree_hash = tree_records(output, {package_manifest_path})
    write_json(package_manifest_path, {
        "schema_version": "l2_demo_package_manifest/1.0",
        "package_name": output.name,
        "contract_schema_version": "l2_taskstream/1.0",
        "stream_count": manifest["stream_count"],
        "turn_count": manifest["total_turn_count"],
        "file_count": len(records),
        "total_size_bytes": sum(item["size_bytes"] for item in records),
        "tree_sha256": tree_hash,
        "files": records,
    })


def build_zip(output: Path) -> Path:
    zip_path = output.with_suffix(".zip")
    if zip_path.exists():
        raise FileExistsError(f"refusing to overwrite existing ZIP: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(item for item in output.rglob("*") if item.is_file()):
            relative = Path(output.name) / path.relative_to(output)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 9, 18, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    zip_path.with_suffix(".zip.sha256").write_text(f"{sha256_file(zip_path)}  {zip_path.name}\n", encoding="ascii")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    parser.add_argument("--skip-zip", action="store_true")
    args = parser.parse_args()
    build_package(args.source.resolve(), args.output.resolve(), args.design.resolve())
    if not args.skip_zip:
        build_zip(args.output.resolve())
    print(f"PASS: generated {args.output.resolve()}")


if __name__ == "__main__":
    main()
