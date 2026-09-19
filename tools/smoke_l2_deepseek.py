#!/usr/bin/env python3
"""Run one public L2 task through DeepSeek and its package-local verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from trievo_bench.model import TokenBudget, client_from_config


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=2048)
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
    messages = [
        {
            "role": "system",
            "content": (
                "Complete the coding task. Return only self-contained Python source code, "
                "with no Markdown fences or commentary."
            ),
        },
        *task["agent_input"]["messages"],
    ]
    if args.reuse_response:
        prior = json.loads(args.reuse_response.read_text(encoding="utf-8"))
        response = prior["agent_response"]
        provider = prior["provider"]
    else:
        budget = TokenBudget(limit=20_000)
        client = client_from_config({}, budget)
        response = client.complete(messages, max_tokens=args.max_tokens)
        provider = {
            "base_url": client.base_url,
            "model": client.model,
            "thinking": client.thinking,
            "calls": budget.calls,
            "reported_or_estimated_tokens": budget.used,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial_output = {
        "schema_version": "l2_deepseek_smoke/1.0",
        "stream_id": task["stream_id"],
        "turn_id": task["turn_id"],
        "source_task_id": task["source_basis"]["source_task_id"],
        "task_sha256": sha256_bytes(task_path.read_bytes()),
        "provider": provider,
        "agent_response": response,
        "agent_response_sha256": sha256_bytes(response.encode("utf-8")),
        "native_verifier_result": None,
    }
    args.output.write_text(
        json.dumps(partial_output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    turn_dir = task_path.parent
    verifier = task["evaluation"]["native_verifier"]
    request = {
        "schema_version": "l2_native_verifier_request/1.0",
        "bindings": {
            "submission": {"kind": "inline", "value": response},
            "native_private_evidence": {
                "kind": "path",
                "path": "evaluation_assets/source_item.json",
            },
        },
    }
    command = [sys.executable, str(turn_dir / verifier["entrypoint"])]
    verifier_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    verifier_output_path = None
    verifier_temp = None
    if args.verifier_image:
        verifier_temp = tempfile.TemporaryDirectory(prefix="l2-deepseek-verifier-")
        verifier_output_path = Path(verifier_temp.name) / "result.json"
        staged_turn = Path(verifier_temp.name) / "turn"
        shutil.copytree(turn_dir, staged_turn)
        command = [
            "docker", "run", "--rm", "-i", "--network", "none",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{staged_turn}:/turn:ro",
            "-v", f"{verifier_temp.name}:/out",
            args.verifier_image,
            "/bin/sh", "-c",
            f"python /turn/{verifier['entrypoint']} > /out/result.json 2> /out/stderr.log",
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
    verifier_text = verifier_output_path.read_text(encoding="utf-8") if verifier_output_path else process.stdout
    verifier_result = json.loads(verifier_text)
    if verifier_temp:
        verifier_temp.cleanup()
    output = {**partial_output, "native_verifier_result": verifier_result}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
