#!/usr/bin/env python3
"""Build selected native Dockerfile recipes; never change qualification status.

--package accepts a pool directory or a composed package. For a composed
package whose Turns do not embed recipes, supply the recipe pool with --pool.
The resulting source-to-image manifest is printed, or written with --out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from l2_v5_native_sources import tree_sha256


def select_recipes(package: Path, pool: Path | None = None,
                   streams: list[str] | None = None,
                   sources: list[str] | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """Resolve source identities from selected Turns or pool records."""
    records = {}
    root = pool or package
    paths = [root / "pool_task.json"] if (root / "pool_task.json").is_file() else sorted(root.rglob("pool_task.json"))
    for path in paths:
        record = json.loads(path.read_text())
        sid = record["source_id"]
        if sid in records:
            raise ValueError("duplicate pool source: " + sid)
        if record.get("environment", {}).get("kind") == "dockerfile":
            records[sid] = (path.parent, record)
    turns = sorted((package / "streams").glob("*/turn*/task.json"))
    selected = set()
    matched_streams = set()
    if turns:
        for path in turns:
            task = json.loads(path.read_text())
            stream = task["stream_id"]
            if streams and stream not in streams:
                continue
            matched_streams.add(stream)
            basis = task["source_basis"]
            sid = basis["benchmark_id"] + ":" + basis["source_task_id"]
            if sources and sid not in sources:
                continue
            if task.get("environment", {}).get("kind") == "dockerfile":
                records.setdefault(sid, (path.parent, {**task, "source_id": sid}))
            if sid in records:
                selected.add(sid)
            elif basis["benchmark_id"] in {"skilllearnbench", "skillsbench"}:
                raise ValueError("missing native recipe; supply --pool: " + sid)
    else:
        if streams:
            raise ValueError("--stream requires a composed package with Turns")
        selected = set(records)
        if sources:
            selected &= set(sources)
    if streams and set(streams) - matched_streams:
        raise ValueError("unknown streams: " + ", ".join(sorted(set(streams) - matched_streams)))
    if sources and set(sources) - selected:
        raise ValueError("sources not selected or missing recipes: " + ", ".join(sorted(set(sources) - selected)))
    if not selected:
        raise ValueError("no Dockerfile recipes selected")
    return [records[sid] for sid in sorted(selected)]


def recipe_paths(task: Path, environment: dict[str, Any]) -> tuple[Path, Path]:
    """Check the materialized recipe and context against their recorded hashes."""
    task = task.resolve()
    context = (task / environment["context_path"]).resolve()
    recipe = (task / environment["recipe_path"]).resolve()
    if not context.is_relative_to(task) or not recipe.is_relative_to(context):
        raise ValueError("recipe/context escapes task directory")
    if hashlib.sha256(recipe.read_bytes()).hexdigest() != environment["recipe_sha256"]:
        raise ValueError("Dockerfile hash mismatch: " + str(recipe))
    if tree_sha256(context) != environment["context_sha256"]:
        raise ValueError("build context hash mismatch: " + str(context))
    return recipe, context


def build_selected(selected: list[tuple[Path, dict[str, Any]]], *,
                   build_args: list[str], timeout: int, dry_run: bool = False) -> dict[str, Any]:
    """Build each distinct context once and report its actual Docker image ID."""
    results, built = [], {}
    for task, record in selected:
        environment = record["environment"]
        recipe, context = recipe_paths(task, environment)
        key = environment["context_sha256"]
        extra_contexts = {}
        for name, value in environment.get("build_contexts", {}).items():
            path = (task / value["path"]).resolve()
            if not path.is_relative_to(task.resolve()) or not path.is_dir():
                raise ValueError("named build context escapes task directory: " + name)
            if tree_sha256(path) != value["sha256"]:
                raise ValueError("named build context hash mismatch: " + name)
            extra_contexts[name] = path
        # Include overrides in the tag so two build configurations do not collide.
        identity = hashlib.sha256(json.dumps(
            [key, build_args, environment.get("build_contexts", {})], sort_keys=True).encode()).hexdigest()
        tag = "trievo-task:" + identity[:24]
        command = ["docker", "build", "-t", tag, "-f", str(recipe)]
        for argument in build_args:
            command += ["--build-arg", argument]
        for name, path in sorted(extra_contexts.items()):
            command += ["--build-context", name + "=" + str(path)]
        command.append(str(context))
        image_id = None
        if not dry_run:
            if identity not in built:
                subprocess.run(command, check=True, timeout=timeout, stdout=sys.stderr)
                image_id = subprocess.check_output(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                    text=True, timeout=60).strip()
                if not image_id.startswith("sha256:") or len(image_id) != 71:
                    raise ValueError("Docker did not return a pinned image ID")
                built[identity] = image_id
            image_id = built[identity]
        results.append({"source_id": record["source_id"], "task_image": tag,
                        "task_image_digest": image_id, "verifier_image_digest": image_id,
                        "recipe_sha256": environment["recipe_sha256"],
                        "context_sha256": key, "command": command})
    return {"schema_version": "l2_task_images/1.0", "dry_run": dry_run,
            "qualification_changed": False, "images": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--pool", type=Path)
    parser.add_argument("--stream", nargs="+", default=[])
    parser.add_argument("--source", nargs="+", default=[])
    parser.add_argument("--build-arg", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--docker-host")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.docker_host:
        os.environ["DOCKER_HOST"] = args.docker_host
    selected = select_recipes(args.package, args.pool, args.stream, args.source)
    result = build_selected(selected, build_args=args.build_arg,
                            timeout=args.timeout, dry_run=args.dry_run)
    output = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
