from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


class NativeBridge:
    baseline_id = ""
    integration = ""

    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.turn_id: str | None = None
        self.session_id: str | None = None
        self.home: Path | None = None
        self.evolved = True

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("protocol_version") != "1.0":
            raise ValueError("unsupported baseline runtime protocol")
        if params.get("baseline_id") != self.baseline_id:
            raise ValueError("unexpected baseline id")
        if params.get("evolution", {}).get("policy", "disabled") != "disabled":
            raise ValueError(f"{self.baseline_id} supports only evolution.policy=disabled")
        self.options = dict(params.get("options", {}))
        return {
            "protocol_version": "1.0", "framework": self.baseline_id,
            "integration": self.integration, "session_policy": "fresh_per_turn",
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self.close_session()
        self.workspace = Path(params["workspace"]).resolve()
        self.state_dir = Path(params["state_dir"]).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "learning").mkdir(exist_ok=True)
        self.turn_id = None
        self.session_id = None
        self.evolved = True
        return {"flow_id": params.get("flow_id"), "session_policy": "fresh_per_turn"}

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.state_dir is None:
            raise RuntimeError("flow has not started")
        if not self.evolved:
            raise RuntimeError("previous turn has not crossed the evolution barrier")
        self.close_session()
        self.home = self.state_dir / "sessions"
        if self.home.exists():
            shutil.rmtree(self.home)
        self.home.mkdir(mode=0o700)
        self.turn_id = str(params["turn_id"])
        self.session_id = None
        self.evolved = False
        return {"turn_id": self.turn_id, "fresh_session": True}

    def require_turn(self, params: dict[str, Any]) -> None:
        if self.turn_id is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("request belongs to an inactive turn")
        if self.evolved:
            raise RuntimeError("turn already crossed the evolution barrier")

    def prompt(self, params: dict[str, Any]) -> str:
        self.require_turn(params)
        if int(params.get("remaining_token_budget", 1)) <= 0:
            raise RuntimeError("interaction token budget is exhausted")
        prompt = str(params.get("request", ""))
        if self.session_id is None:
            prompt = (
                "Complete the user task in the shared workspace. Do not access evaluator "
                "or baseline state directories or modify persistent learning state.\n\n" + prompt
            )
            if params.get("history"):
                prompt += "\nPublic conversation:\n" + json.dumps(params["history"], ensure_ascii=True)
        return prompt

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_turn(params)
        self.close_session()
        self.evolved = True
        return {
            "completed": True, "usage": {},
            "events": [{"type": "evolution.skipped", "reason": "policy_disabled"}],
        }

    def close_session(self) -> None:
        pass

    def end_flow(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.close_session()
        if self.home is not None and self.home.exists():
            shutil.rmtree(self.home)
        self.home = None
        self.turn_id = None
        self.session_id = None
        self.workspace = None
        self.state_dir = None
        return {"completed": True}


def integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("usage counters must be non-negative integers")
    return value


def serve(bridge: NativeBridge) -> int:
    methods = {name: getattr(bridge, name) for name in (
        "initialize", "start_flow", "start_turn", "interact", "evolve", "end_flow",
    )}
    try:
        for line in sys.stdin:
            request: dict[str, Any] = {}
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("baseline request must be an object")
                result = methods[request["method"]](request.get("params", {}))
                response = {"id": request.get("id"), "result": result}
            except Exception as exc:
                response = {
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"message": f"{type(exc).__name__}: {exc}"},
                }
            serialized = json.dumps(response, ensure_ascii=True)
            key_env = str(bridge.options.get("api_key_env", "TRIEVO_API_KEY"))
            for name, value in os.environ.items():
                if value and (name.endswith("API_KEY") or name == key_env):
                    serialized = serialized.replace(json.dumps(value)[1:-1], "[redacted]")
            print(serialized, flush=True)
            if isinstance(request, dict) and request.get("method") == "end_flow":
                break
    finally:
        bridge.end_flow()
    return 0
