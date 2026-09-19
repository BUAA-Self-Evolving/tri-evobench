#!/usr/bin/env python3
"""Run one public L2 task through DeepSeek and its package-local verifier."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from trievo_bench.model import TokenBudget, client_from_config
from trievo_bench.docker_volume import DockerVolumeMirror


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def strip_code_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        return "\n".join(lines[1:-1]).strip() + "\n"
    return value


def workspace_inventory(root: Path) -> list[dict[str, object]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return rows


def package_root_for(task_path: Path) -> Path:
    for parent in task_path.parents:
        if (parent / "manifest.json").is_file() and (parent / "pools.json").is_file():
            return parent
    raise ValueError(f"cannot resolve package root for {task_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--allow-generated-code", action="store_true")
    parser.add_argument(
        "--verifier-image",
        help="run the verifier offline in this Docker image instead of the current Python",
    )
    parser.add_argument(
        "--reuse-response",
        type=Path,
        help="reuse agent_response from an existing smoke result without another API call",
    )
    args = parser.parse_args()

    task_path = args.task.resolve()
    task = json.loads(task_path.read_text(encoding="utf-8"))
    package_root = package_root_for(task_path)
    contract = task["evaluation"]["submission_contract"]
    mode = contract["mode"]
    payload_kind = contract["payload_kind"]
    if mode == "workspace_artifact":
        system_prompt = (
            "Complete the task in an offline Linux sandbox. Return only a POSIX shell script, "
            "without Markdown fences. The task assets are already in /root. The script must "
            "create the requested final artifacts under /root and must not use the network."
        )
    elif payload_kind == "json_trajectory":
        system_prompt = (
            "Solve the task by returning only one JSON object with assistant_message and "
            "tool_calls. Each tool call must contain name and arguments. Preserve call order."
        )
    else:
        system_prompt = (
            "Complete the coding task. Return only self-contained Python source code, "
            "with no Markdown fences or commentary."
        )
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        *task["agent_input"]["messages"],
    ]
    if args.reuse_response:
        prior = json.loads(args.reuse_response.read_text(encoding="utf-8"))
        response = prior["agent_response"]
        provider = prior["provider"]
        response_source = {
            "mode": "reused_saved_response",
            "live_api_calls": 0,
            "source_result_sha256": sha256_file(args.reuse_response),
        }
    else:
        budget = TokenBudget(limit=20_000)
        client = client_from_config({}, budget)
        response = strip_code_fence(client.complete(messages, max_tokens=args.max_tokens))
        provider = {
            "base_url": client.base_url,
            "model": client.model,
            "thinking": client.thinking,
            "calls": budget.calls,
            "reported_or_estimated_tokens": budget.used,
        }
        response_source = {
            "mode": "live_api",
            "live_api_calls": budget.calls,
            "source_result_sha256": None,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bindings = {
        name: sha256_file(package_root / binding["path"])
        for name, binding in {
            "package_manifest": {"path": "manifest.json"},
            "pool_manifest": {"path": "pools.json"},
            "panel_manifest": {"path": "panels.json"},
            "runtime_profiles": {"path": "runtime_profiles.json"},
        }.items()
    }
    partial_output = {
        "schema_version": "l2_deepseek_smoke/1.0",
        "stream_id": task["stream_id"],
        "turn_id": task["turn_id"],
        "source_task_id": task["source_basis"]["source_task_id"],
        "task_sha256": sha256_bytes(task_path.read_bytes()),
        "provider": provider,
        "response_source": response_source,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "bindings": bindings,
        "runtime_profile_id": task["runtime_profile_id"],
        "submission_contract": {"mode": mode, "payload_kind": payload_kind},
        "agent_response": response,
        "agent_response_sha256": sha256_bytes(response.encode("utf-8")),
        "execution": None,
        "submission": None,
        "native_verifier_result": None,
        "failure_classification": None,
    }
    args.output.write_text(
        json.dumps(partial_output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    turn_dir = task_path.parent
    verifier = task["evaluation"]["native_verifier"]
    workspace = None
    execution = {"kind": "inline_response", "returncode": 0, "network": "not_applicable"}
    submission_binding: dict[str, object] = {"kind": "inline", "value": response}
    smoke_temp = tempfile.TemporaryDirectory(prefix="l2-deepseek-smoke-")
    smoke_root = Path(smoke_temp.name)
    mirrors: list[DockerVolumeMirror] = []

    def mirrored(path: Path, role: str) -> DockerVolumeMirror:
        mirror = DockerVolumeMirror.create(path, role=role)
        mirrors.append(mirror)
        atexit.register(mirror.close)
        return mirror

    workspace_volume = None
    if mode == "workspace_artifact":
        if not args.allow_generated_code:
            raise SystemExit("workspace smoke requires --allow-generated-code")
        if not args.verifier_image:
            raise SystemExit("workspace smoke requires --verifier-image")
        workspace = smoke_root / "workspace"
        seed = turn_dir / "workspace_seed"
        shutil.copytree(seed, workspace) if seed.is_dir() else workspace.mkdir()
        script_root = smoke_root / "generated"
        script_root.mkdir()
        script = script_root / "agent_response.sh"
        script.write_text(response, encoding="utf-8")
        workspace_volume = mirrored(workspace, "l2-deepseek-workspace")
        script_volume = mirrored(script_root, "l2-deepseek-script")
        execution_command = [
            "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--mount", workspace_volume.mount_spec("/root"),
            "--mount", script_volume.mount_spec("/generated", read_only=True),
            "-w", "/root", args.verifier_image, "/bin/sh", "/generated/agent_response.sh",
        ]
        executed = subprocess.run(
            execution_command, text=True, capture_output=True,
            timeout=task["evaluation"]["native_verifier"]["timeout_seconds"], check=False,
        )
        workspace_volume.sync_to_host()
        execution = {
            "kind": "offline_generated_shell", "returncode": executed.returncode,
            "network": "none", "stdout_tail": executed.stdout[-4000:],
            "stderr_tail": executed.stderr[-4000:],
        }
        submission_binding = {"kind": "path", "path": "agent_workspace"}
    request = {
        "schema_version": "l2_native_verifier_request/1.0",
        "bindings": {"submission": submission_binding},
    }
    command = [sys.executable, str(turn_dir / verifier["entrypoint"])]
    verifier_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    verifier_output_path = None
    verifier_temp = None
    verifier_volume = None
    if args.verifier_image:
        verifier_temp = tempfile.TemporaryDirectory(prefix="l2-deepseek-verifier-")
        verifier_output_path = Path(verifier_temp.name) / "result.json"
        staged_turn = Path(verifier_temp.name) / "turn"
        shutil.copytree(turn_dir, staged_turn)
        verifier_volume = mirrored(Path(verifier_temp.name), "l2-deepseek-verifier")
        command = [
            "docker", "run", "--rm", "-i", "--network", "none",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "L2_VERIFIER_WORKSPACE=/submission",
            "--mount", verifier_volume.mount_spec("/smoke"),
            *(
                ["--mount", workspace_volume.mount_spec("/submission", read_only=True)]
                if workspace_volume is not None else []
            ),
            args.verifier_image,
            "/bin/sh", "-c",
            f"python /smoke/turn/{verifier['entrypoint']} > /smoke/result.json 2> /smoke/stderr.log",
        ]
        verifier_env = None
    process = subprocess.run(
        command,
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=verifier["timeout_seconds"] + 30,
        check=True,
        env=verifier_env,
    )
    if verifier_volume is not None:
        verifier_volume.sync_to_host()
    verifier_text = verifier_output_path.read_text(encoding="utf-8") if verifier_output_path else process.stdout
    verifier_result = json.loads(verifier_text)
    if verifier_temp:
        verifier_temp.cleanup()
    submission = (
        {"kind": "workspace", "files": workspace_inventory(workspace)}
        if workspace else
        {"kind": "inline", "sha256": sha256_bytes(response.encode("utf-8")), "size": len(response.encode("utf-8"))}
    )
    failure_classification = (
        "none" if verifier_result.get("status") == "pass" else
        "model_failure" if verifier_result.get("status") == "fail" else
        "verifier_or_infrastructure_error"
    )
    checkpoint = {
        "schema_version": "l2_deepseek_smoke_checkpoint/1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "stream_id": task["stream_id"], "turn_id": task["turn_id"],
        "bindings": bindings, "agent_response_sha256": partial_output["agent_response_sha256"],
        "submission": submission, "verifier_status": verifier_result.get("status"),
        "learner_state": {"present": False, "reason": "single-turn API transport smoke"},
    }
    checkpoint_path = args.output.with_suffix(args.output.suffix + ".checkpoint.json")
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output = {
        **partial_output, "execution": execution, "submission": submission,
        "native_verifier_result": verifier_result,
        "failure_classification": failure_classification,
        "checkpoint": {"path": str(checkpoint_path.resolve()), "sha256": sha256_file(checkpoint_path)},
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for mirror in reversed(mirrors):
        mirror.close()
    smoke_temp.cleanup()
    print(json.dumps({
        "output": str(args.output.resolve()),
        "stream_id": task["stream_id"],
        "turn_id": task["turn_id"],
        "model": provider["model"],
        "calls": provider["calls"],
        "tokens": provider["reported_or_estimated_tokens"],
        "verifier_status": verifier_result["status"],
        "verifier_score": verifier_result["score"],
    }))


if __name__ == "__main__":
    main()
