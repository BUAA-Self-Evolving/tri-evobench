from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class FakeHarness:
    def __init__(self, **kwargs: Any) -> None:
        self.config = kwargs
        self.closed = False
        self.prompts: list[str] = []

    def close(self) -> None:
        self.closed = True

    def run(self, prompt: str, *, session_id: str | None = None) -> SimpleNamespace:
        self.prompts.append(prompt)
        (Path(self.config["cwd"]) / "native-result.txt").write_text("done", encoding="utf-8")
        return SimpleNamespace(
            session_id=session_id or str(uuid.uuid4()), final_response="done", finish_reason="completed",
            events=[
                {"type": "assistant/message", "seq": 1, "data": {"usage": {
                    "inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 3,
                    "cacheWriteTokens": 2, "reasoningTokens": 1, "totalTokens": 20,
                }}},
                {"type": "tool/call", "seq": 2, "data": {"callId": "c1", "name": "bash", "arguments": '{"command":"echo done"}'}},
                {"type": "tool/result", "seq": 3, "data": {"message": {
                    "role": "tool", "source": {"kind": "tool", "callId": "c1"},
                    "toolCallId": "c1", "isError": False,
                    "content": [{"type": "text", "text": "done"}],
                }}},
            ], notifications=[],
        )


def zcode_main() -> None:
    arguments = sys.argv[1:]
    session = arguments[arguments.index("--resume") + 1] if "--resume" in arguments else str(uuid.uuid4())
    workspace = Path(arguments[arguments.index("--cwd") + 1])
    (workspace / "native-result.txt").write_text("done", encoding="utf-8")
    rows = [{"type": "tool.updated", "sessionId": session, "payload": {
        "kind": kind, "toolCallId": "c1", "toolName": "Bash",
        "input": {"command": "echo done"}, "result": {"content": "done"},
    }} for kind in ("scheduled", "started", "progress", "result")]
    rows.append({
        "type": "result", "sessionId": session, "response": "done",
        "usage": {"inputTokens": 15, "outputTokens": 5, "totalTokens": 20,
                  "cacheReadTokens": 3, "cacheWriteTokens": 2, "reasoningTokens": 1,
                  "modelRequestCount": 1},
        "projection": {"status": "idle", "totalTokenCount": 1000},
    })
    for row in rows:
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    zcode_main()
