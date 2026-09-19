#!/usr/bin/env python3
"""Smoke every derived L2 Stream from a blank workspace through Turn 2."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def apply_transition(turn_dir: Path, transition: dict, workspace: Path) -> None:
    for operation in transition["operations"]:
        target = workspace / Path(operation["target"]).relative_to("agent_workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        if operation["op"] == "copy":
            source = turn_dir / operation["source"]
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        elif operation["op"] == "create":
            target.write_text(operation["content"], encoding="utf-8")
        else:
            raise AssertionError(f"unsupported isolation-smoke operation: {operation['op']}")


def submission_binding(task: dict) -> dict:
    contract = task["evaluation"]["submission_contract"]
    if contract["mode"] == "workspace_artifact":
        declared = next(
            item for item in task["evaluation"]["native_verifier"]["input_bindings"]
            if item["name"] == contract["source_binding"]
        )
        return {"kind": "path", "path": declared["path"]}
    payload = "{}" if contract["payload_kind"] == "json_tool_trajectory" else "def task_func(*args, **kwargs):\n    return None\n"
    return {"kind": "inline", "value": payload}


def run_carrier(turn_dir: Path, workspace: Path, image: str) -> dict:
    task = load_json(turn_dir / "task.json")
    bindings = {}
    for binding in task["evaluation"]["native_verifier"]["input_bindings"]:
        if binding["name"] == task["evaluation"]["submission_contract"]["source_binding"]:
            bindings[binding["name"]] = submission_binding(task)
        else:
            bindings[binding["name"]] = {"kind": "path", "path": binding["path"]}
    request = {"schema_version": "l2_native_verifier_request/1.0", "bindings": bindings}
    verifier = task["evaluation"]["native_verifier"]
    with tempfile.TemporaryDirectory(prefix="l2-shard-carrier-") as output_dir:
        staged_turn = Path(output_dir) / "turn"
        shutil.copytree(turn_dir, staged_turn)
        process = subprocess.run(
            [
                "docker", "run", "--rm", "-i", "--network", "none",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", "L2_VERIFIER_WORKSPACE=/workspace",
                "-v", f"{staged_turn}:/turn:ro",
                "-v", f"{workspace}:/workspace",
                "-v", f"{output_dir}:/out",
                image,
                "/bin/sh", "-c",
                f"python /turn/{verifier['entrypoint']} > /out/result.json 2> /out/stderr.log",
            ],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=verifier["timeout_seconds"] + 30,
            check=False,
        )
        result_path = Path(output_dir) / "result.json"
        stderr_path = Path(output_dir) / "stderr.log"
        result = load_json(result_path) if result_path.is_file() and result_path.stat().st_size else None
        return {
            "returncode": process.returncode,
            "status": result.get("status") if result else None,
            "score": result.get("score") if result else None,
            "verifier_id": result.get("verifier_id") if result else None,
            "stderr_tail": stderr_path.read_text(encoding="utf-8", errors="replace")[-1000:]
            if stderr_path.is_file() else "",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = args.package.resolve()
    manifest = load_json(package / "manifest.json")
    records = []
    for stream_ref in manifest["streams"]:
        stream_id = stream_ref["stream_id"]
        if "__shard_" not in stream_id:
            continue
        stream_dir = package / "streams" / stream_ref["directory"]
        image = "trievo-l2-browser:0.1" if stream_id.startswith(("skilllearn_", "skillsbench_")) else "trievo-l2:0.1"
        with tempfile.TemporaryDirectory(prefix="l2-shard-workspace-") as temp_dir:
            workspace = Path(temp_dir) / "agent_workspace"
            shutil.copytree(stream_dir / "agent_workspace", workspace)
            stream_record = {"stream_id": stream_id, "turns": []}
            for index in (1, 2):
                turn_dir = stream_dir / f"turn{index}"
                task = load_json(turn_dir / "task.json")
                if index == 1:
                    next_task = load_json(stream_dir / "turn2" / "task.json")
                    future_targets = [
                        workspace / Path(item["target"]).relative_to("agent_workspace")
                        for item in next_task["workspace_transition"]["operations"]
                        if item["op"] in {"copy", "create"}
                    ]
                    assert all(not target.exists() for target in future_targets)
                apply_transition(turn_dir, task["workspace_transition"], workspace)
                carrier = run_carrier(turn_dir, workspace, image)
                assert carrier["returncode"] == 0, (stream_id, index, carrier)
                assert carrier["status"] in {"pass", "fail", "error"}, (stream_id, index, carrier)
                stream_record["turns"].append({"turn_id": f"turn{index}", **carrier})
            records.append(stream_record)
    assert len(records) == 24
    output = {
        "schema_version": "l2_shard_isolation_smoke/1.0",
        "package": package.name,
        "stream_count": len(records),
        "turns_smoked": sum(len(item["turns"]) for item in records),
        "streams": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: {len(records)} independent shards, {output['turns_smoked']} carrier smokes")


if __name__ == "__main__":
    main()
