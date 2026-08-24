from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class FakeAdapter:
    def __init__(self) -> None:
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.options: dict[str, Any] = {}
        self.turn_id = ""
        self.flow_metadata: dict[str, Any] = {}
        self.turn_metadata: dict[str, Any] = {}

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            self.options = dict(params.get("options", {}))
            return {"protocol_version": "1.0", "adapter": "fake"}
        if method == "start_flow":
            self.workspace = Path(str(params["workspace"]))
            self.state_dir = Path(str(params["state_dir"]))
            self.flow_metadata = dict(params.get("metadata", {}))
            return {"framework": "fake-agent"}
        if method == "start_turn":
            self.turn_id = str(params["turn_id"])
            self.turn_metadata = dict(params.get("metadata", {}))
            if self.options.get("violate_start_turn"):
                if self.state_dir is None:
                    raise RuntimeError("flow not started")
                learning = self.state_dir / "learning"
                learning.mkdir(parents=True, exist_ok=True)
                (learning / "illegal-start.txt").write_text("illegal start-turn update", encoding="utf-8")
            return {"session_id": f"fake-{self.turn_id}"}
        if method == "interact":
            if self.workspace is None or self.state_dir is None:
                raise RuntimeError("flow not started")
            (self.workspace / "answer.txt").write_text("created by process baseline", encoding="utf-8")
            if self.options.get("violate_state_freeze"):
                learning = self.state_dir / "learning"
                learning.mkdir(parents=True, exist_ok=True)
                (learning / "illegal.txt").write_text("illegal foreground update", encoding="utf-8")
            if self.options.get("write_ignored_telemetry"):
                learning = self.state_dir / "learning"
                learning.mkdir(parents=True, exist_ok=True)
                (learning / "telemetry.json").write_text("foreground telemetry", encoding="utf-8")
            return {
                "text": "The process baseline completed the task.",
                "tool_calls": [{
                    "tool": "fake.write_file",
                    "arguments": {"path": "/workspace/answer.txt"},
                    "result": {"status": "created"}
                }],
                "events": [{"type": "tool.completed", "tool": "fake.write_file"}],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "api_calls": 1}
            }
        if method == "evolve":
            if self.state_dir is None:
                raise RuntimeError("flow not started")
            learning = self.state_dir / "learning"
            learning.mkdir(parents=True, exist_ok=True)
            (learning / f"{self.turn_id}.json").write_text(
                json.dumps({
                    **params,
                    "flow_metadata": self.flow_metadata,
                    "turn_metadata": self.turn_metadata,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return {
                "completed": not self.options.get("incomplete_evolution"),
                "events": [{"type": "evolution.completed"}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "api_calls": 1}
            }
        if method == "end_flow":
            return {"closed": True}
        raise RuntimeError(f"unsupported method: {method}")


def main() -> None:
    adapter = FakeAdapter()
    for line in sys.stdin:
        request = json.loads(line)
        request_id = request.get("id")
        method = str(request.get("method", ""))
        try:
            result = adapter.dispatch(method, request.get("params", {}))
            response = {"id": request_id, "result": result}
        except Exception as exc:
            response = {"id": request_id, "error": {"message": str(exc)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
        if method == "end_flow":
            break


if __name__ == "__main__":
    main()
