#!/usr/bin/env python3
"""Tiny process-baseline adapter for deterministic L2 V5 runtime smoke."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "1.0"


workspace = Path(".")
state_dir = Path(".")
flow_id = ""
turn_id = ""
turn_metadata: dict[str, Any] = {}


def _reply(request_id: int, result: dict[str, Any]) -> None:
    print(json.dumps({"id": request_id, "result": result}, ensure_ascii=True), flush=True)


def _turn_dir() -> Path:
    package = os.environ.get("TRIEVO_L2_V5_PACKAGE", "benchmarks/current/L2_Demo_V5")
    return Path(package).resolve() / "streams" / flow_id / turn_id


def _known_correct_reply(params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    current = _turn_dir()
    assets = current / "evaluation_assets"
    for name in ("known_correct_response.py", "known_correct_response.json"):
        answer = assets / name
        if answer.is_file():
            return answer.read_text(encoding="utf-8"), [{"tool": "fixture_read", "status": "ok"}]
    task_runtime = params.get("sandbox", {}).get("task_runtime", {})
    image = str(task_runtime.get("image_digest", ""))
    container = str(task_runtime.get("container_name", ""))
    if not image.startswith("sha256:") or not re.fullmatch(
        r"trievo-l2-task-[0-9a-f]{12}", container
    ):
        raise RuntimeError("known-correct workspace fixture requires a pinned task sandbox")
    try:
        for source, target in (
            (assets, "/eval"),
            (assets / "native_oracle_files", "/solution"),
        ):
            copied = subprocess.run(
                ["docker", "cp", f"{source.resolve()}/.", f"{container}:{target}"],
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if copied.returncode != 0:
                raise RuntimeError("fixture asset injection failed: " + copied.stderr[-2000:])
        completed = subprocess.run(
            [
                "docker", "exec", "-w", "/workspace", container, "/bin/bash", "-lc",
                "sed -i 's#/root#/workspace#g' /eval/oracle_solution.sh "
                "&& /bin/bash /eval/oracle_solution.sh",
            ],
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("known-correct workspace oracle failed: " + completed.stderr[-2000:])
    finally:
        subprocess.run(
            ["docker", "exec", container, "rm", "-rf", "/eval", "/solution"],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    return "Known-correct workspace fixture materialized.", [{
        "tool": "offline_native_oracle", "status": "ok", "network": "none",
        "image_digest": image,
    }]


def _handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    global workspace, state_dir, flow_id, turn_id, turn_metadata
    if method == "initialize":
        return {"protocol_version": PROTOCOL_VERSION, "adapter": "l2-v5-scripted-smoke"}
    if method == "start_flow":
        flow_id = str(params.get("flow_id", ""))
        workspace = Path(str(params["workspace"]))
        state_dir = Path(str(params["state_dir"]))
        state_dir.mkdir(parents=True, exist_ok=True)
        return {"flow_id": params.get("flow_id"), "state_dir": str(state_dir)}
    if method == "start_turn":
        turn_id = str(params["turn_id"])
        turn_metadata = dict(params.get("metadata", {}))
        (state_dir / "last_turn.txt").write_text(turn_id, encoding="utf-8")
        return {"started": True}
    if method == "interact":
        (workspace / f"scripted-{turn_id}.txt").write_text("completed\n", encoding="utf-8")
        text, tool_calls = _known_correct_reply(params)
        return {
            "text": text,
            "usage": {"total_tokens": 1, "api_calls": 0},
            "tool_calls": tool_calls,
            "events": [{"type": "scripted.interact"}],
        }
    if method == "evolve":
        counter_path = state_dir / "evolve_count.txt"
        current = int(counter_path.read_text(encoding="utf-8")) if counter_path.is_file() else 0
        if params.get("update_enabled"):
            counter_path.write_text(str(current + 1), encoding="utf-8")
        return {
            "completed": True,
            "events": [{"type": "scripted.evolve", "update_enabled": bool(params.get("update_enabled"))}],
            "usage": {"total_tokens": 0, "api_calls": 0},
        }
    if method == "end_flow":
        return {"ended": True}
    return {"ok": True}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        request_id = int(request["id"])
        try:
            _reply(request_id, _handle(str(request["method"]), dict(request.get("params", {}))))
        except Exception as exc:  # pragma: no cover - smoke diagnostics
            print(json.dumps({"id": request_id, "error": {"message": str(exc)}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
