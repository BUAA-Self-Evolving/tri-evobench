#!/usr/bin/env python3
"""Tiny process-baseline adapter for deterministic L2 V5 runtime smoke."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "1.0"


workspace = Path(".")
state_dir = Path(".")


def _reply(request_id: int, result: dict[str, Any]) -> None:
    print(json.dumps({"id": request_id, "result": result}, ensure_ascii=True), flush=True)


def _handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    global workspace, state_dir
    if method == "initialize":
        return {"protocol_version": PROTOCOL_VERSION, "adapter": "l2-v5-scripted-smoke"}
    if method == "start_flow":
        workspace = Path(str(params["workspace"]))
        state_dir = Path(str(params["state_dir"]))
        state_dir.mkdir(parents=True, exist_ok=True)
        return {"flow_id": params.get("flow_id"), "state_dir": str(state_dir)}
    if method == "start_turn":
        (state_dir / "last_turn.txt").write_text(str(params["turn_id"]), encoding="utf-8")
        return {"started": True}
    if method == "interact":
        turn_id = str(params["turn_id"])
        (workspace / f"scripted-{turn_id}.txt").write_text("completed\n", encoding="utf-8")
        return {
            "text": f"Completed {turn_id} with deterministic L2 V5 smoke evidence.",
            "usage": {"total_tokens": 1, "api_calls": 0},
            "tool_calls": [{"tool": "scripted", "status": "ok"}],
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
