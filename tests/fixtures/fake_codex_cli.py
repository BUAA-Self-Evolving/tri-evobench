from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path


def main() -> int:
    prompt = sys.stdin.read()
    if "MALFORMED_CODEX_OUTPUT" in prompt:
        print("not JSON")
        return 0
    home = Path(os.environ["CODEX_HOME"])
    saved = home / "fake-session.json"
    if "resume" in sys.argv:
        state = json.loads(saved.read_text())
        if sys.argv[sys.argv.index("resume") + 1] != state["id"]:
            return 2
    else:
        state = {"id": str(uuid.uuid4()), "calls": 0}
    state["calls"] += 1
    saved.write_text(json.dumps(state))
    events = [{"type": "thread.started", "thread_id": state["id"]}]
    if "--output-schema" in sys.argv:
        text = json.dumps({"memory": "Check output files before reporting success."})
    else:
        Path("codex-result.txt").write_text("done", encoding="utf-8")
        text = "done"
        events.append({"type": "item.completed", "item": {
            "id": "command-1", "type": "command_execution", "command": "write output",
            "aggregated_output": "done", "exit_code": 0, "status": "completed",
        }})
    events.extend([
        {"type": "item.completed", "item": {"id": "message-1", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {
            "input_tokens": state["calls"] * 10, "output_tokens": state["calls"] * 2,
            "cached_input_tokens": state["calls"] * 3, "reasoning_output_tokens": state["calls"],
        }},
    ])
    for event in events:
        print(json.dumps(event), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
