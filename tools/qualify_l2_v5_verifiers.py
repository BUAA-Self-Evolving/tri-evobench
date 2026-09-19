#!/usr/bin/env python3
"""Run source-native red/green qualification for an L2 Demo V5 package."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _turn_key(path: Path) -> int:
    name = path.name
    if name.startswith("turn") and name[4:].isdigit():
        return int(name[4:])
    return 10**9


def _docker_shell(
    *,
    image: str,
    script: str,
    mounts: list[tuple[Path, str, str]],
    timeout: int,
) -> tuple[int, dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="l2-v5-docker-out-") as temp:
        out_dir = Path(temp)
        command = ["docker", "run", "--rm", "--network", "none"]
        for host, container, mode in mounts:
            suffix = f":{mode}" if mode else ""
            command.extend(["-v", f"{host.resolve()}:{container}{suffix}"])
        command.extend([
            "-v",
            f"{out_dir}:/out",
            "--entrypoint",
            "/bin/sh",
            image,
            "-lc",
            script,
        ])
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        files = {
            item.name: item.read_text(encoding="utf-8", errors="replace")
            for item in out_dir.iterdir()
            if item.is_file()
        }
        if completed.returncode != 0 and "docker.stderr" not in files:
            files["docker.stderr"] = completed.stderr[-4000:]
        return completed.returncode, files


def _copy_workspace_seed(turn_dir: Path, workspace: Path) -> None:
    seed = turn_dir / "workspace_seed"
    if not seed.is_dir():
        return
    for child in seed.iterdir():
        target = workspace / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _run_verifier(
    *,
    image: str,
    turn_dir: Path,
    binding: dict[str, Any],
    timeout: int,
    workspace: Path | None = None,
) -> dict[str, Any]:
    request = {
        "schema_version": "l2_native_verifier_request/1.0",
        "bindings": {"submission": binding},
    }
    with tempfile.TemporaryDirectory(prefix="l2-v5-verifier-request-") as temp:
        request_dir = Path(temp)
        (request_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
        mounts = [(turn_dir, "/turn", "ro"), (request_dir, "/request", "ro")]
        prefix = ""
        if workspace is not None:
            mounts.append((workspace, "/workspace", ""))
            prefix = "export L2_VERIFIER_WORKSPACE=/workspace; "
        _, files = _docker_shell(
            image=image,
            mounts=mounts,
            timeout=timeout,
            script=(
                f"{prefix}"
                "python /turn/evaluation_assets/verifier_cli.py "
                "< /request/request.json > /out/result.json 2> /out/stderr.log; "
                "echo $? > /out/rc"
            ),
        )
    inner_rc = int(files.get("rc", "999").strip() or "999")
    if inner_rc != 0 or "result.json" not in files:
        return {
            "status": "transport_error",
            "score": None,
            "error": files.get("stderr.log", files.get("docker.stderr", ""))[-4000:],
            "inner_returncode": inner_rc,
        }
    try:
        return json.loads(files["result.json"])
    except json.JSONDecodeError as exc:
        return {
            "status": "invalid_json",
            "score": None,
            "error": str(exc),
            "stdout": files["result.json"][-4000:],
            "stderr": files.get("stderr.log", "")[-4000:],
        }


def _run_oracle(*, image: str, turn_dir: Path, workspace: Path, timeout: int) -> dict[str, Any]:
    oracle_files = turn_dir / "evaluation_assets" / "native_oracle_files"
    mounts = [(workspace, "/root", ""), (turn_dir, "/turn", "ro")]
    if oracle_files.is_dir():
        mounts.extend([(oracle_files, "/oracle", "ro"), (oracle_files, "/solution", "ro")])
    _, files = _docker_shell(
        image=image,
        mounts=mounts,
        timeout=timeout,
        script=(
            "export ORACLE_DIR=/oracle; "
            "bash /turn/evaluation_assets/oracle_solution.sh "
            "> /out/oracle.stdout 2> /out/oracle.stderr; "
            "echo $? > /out/rc"
        ),
    )
    return {
        "returncode": int(files.get("rc", "999").strip() or "999"),
        "stdout_tail": files.get("oracle.stdout", "")[-4000:],
        "stderr_tail": files.get("oracle.stderr", "")[-4000:],
    }


def _inline_payloads(turn_dir: Path) -> tuple[str, str]:
    assets = turn_dir / "evaluation_assets"
    correct = assets / "known_correct_response.py"
    broken = assets / "known_broken_response.py"
    if not correct.is_file():
        correct = assets / "known_correct_response.json"
        broken = assets / "known_broken_response.json"
    return correct.read_text(encoding="utf-8"), broken.read_text(encoding="utf-8")


def qualify(package: Path, image: str, *, all_turns: bool = False) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen_capabilities: set[str] = set()
    for stream_dir in sorted((package / "streams").iterdir()):
        if not stream_dir.is_dir():
            continue
        for turn_dir in sorted(stream_dir.glob("turn*"), key=_turn_key):
            task = _load_json(turn_dir / "task.json")
            capability_id = task["taxonomy_ref"]["capability_id"]
            if not all_turns and capability_id in seen_capabilities:
                continue
            seen_capabilities.add(capability_id)
            native = task["evaluation"]["native_verifier"]
            timeout = int(native.get("timeout_seconds", 900)) + 60
            binding_decl = native["input_bindings"][0]
            oracle = None
            if binding_decl["source"] == "interaction_record":
                correct, broken = _inline_payloads(turn_dir)
                known_correct = _run_verifier(
                    image=image,
                    turn_dir=turn_dir,
                    binding={"kind": "inline", "value": correct},
                    timeout=timeout,
                )
                deliberately_broken = _run_verifier(
                    image=image,
                    turn_dir=turn_dir,
                    binding={"kind": "inline", "value": broken},
                    timeout=timeout,
                )
            else:
                with tempfile.TemporaryDirectory(prefix="l2-v5-known-correct-") as temp:
                    workspace = Path(temp) / "workspace"
                    workspace.mkdir()
                    _copy_workspace_seed(turn_dir, workspace)
                    oracle = _run_oracle(
                        image=image, turn_dir=turn_dir, workspace=workspace, timeout=timeout
                    )
                    known_correct = _run_verifier(
                        image=image,
                        turn_dir=turn_dir,
                        workspace=workspace,
                        binding={"kind": "path", "path": "agent_workspace"},
                        timeout=timeout,
                    )
                with tempfile.TemporaryDirectory(prefix="l2-v5-known-broken-") as temp:
                    workspace = Path(temp) / "workspace"
                    workspace.mkdir()
                    _copy_workspace_seed(turn_dir, workspace)
                    deliberately_broken = _run_verifier(
                        image=image,
                        turn_dir=turn_dir,
                        workspace=workspace,
                        binding={"kind": "path", "path": "agent_workspace"},
                        timeout=timeout,
                    )
            rows.append({
                "capability_id": capability_id,
                "benchmark_id": task["source_basis"]["benchmark_id"],
                "stream_id": task["stream_id"],
                "turn_id": task["turn_id"],
                "source_task_id": task["source_basis"]["source_task_id"],
                "known_correct_status": known_correct.get("status"),
                "known_correct_score": known_correct.get("score"),
                "known_correct_error": known_correct.get("error"),
                "broken_status": deliberately_broken.get("status"),
                "broken_score": deliberately_broken.get("score"),
                "broken_error": deliberately_broken.get("error"),
                "oracle": oracle,
            })
    passing = [
        row for row in rows
        if row["known_correct_status"] == "pass" and row["broken_status"] == "fail"
    ]
    return {
        "schema_version": "l2_v5_verifier_qualification/1.0",
        "package": str(package.resolve()),
        "image": image,
        "mode": "all_turns" if all_turns else "one_per_capability",
        "capability_count": len({row["capability_id"] for row in rows}),
        "result_count": len(rows),
        "summary": {"pass_fail": len(passing), "total": len(rows)},
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--all-turns", action="store_true")
    args = parser.parse_args()
    report = qualify(args.package, args.image, all_turns=args.all_turns)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "pass_fail": report["summary"]["pass_fail"],
        "total": report["summary"]["total"],
    }, ensure_ascii=False))
    return 0 if report["summary"]["pass_fail"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
