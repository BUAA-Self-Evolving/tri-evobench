"""Run inside the existing DSH image against a host-only mock gateway."""
import json
import os
from pathlib import Path
import tempfile

from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge

bridge = DeepSeekHarnessBridge()
try:
    with tempfile.TemporaryDirectory() as temporary:
        bridge.initialize({"protocol_version": "1.0", "baseline_id": "deepseek-harness", "options": {
            "executable": "/opt/deepseek-harness/apps/cli/lib/bin.js",
            "execution_profile": "opencode-full-stream-20260928",
            "model": os.environ.get("TRIEVO_MODEL", "deepseek-v4-flash"), "base_url": os.environ["TRIEVO_BASE_URL"],
            "provider_transport": "friday_chat", "max_output_tokens": 4096,
            "request_timeout_seconds": 45, "upstream_timeout_seconds": 30,
            "patches": json.loads(os.environ.get("TRIEVO_DSH_PATCHES", "[]"))}})
        bridge.start_flow({"workspace": temporary + "/workspace", "state_dir": temporary + "/state"})
        bridge.start_turn({"turn_id": "t1"})
        original_run = bridge._run_bounded
        def capture(prompt):
            result = original_run(prompt)
            if result.finish_reason != "completed":
                import sys
                print(json.dumps({"native_failure_events": result.events}), file=sys.stderr)
            return result
        bridge._run_bounded = capture
        result = bridge.interact({"turn_id": "t1", "request": "Use bash to write verified to native-tool-marker, then reply done."})
        assert result["text"] == "done", result["text"]
        assert Path(temporary + "/workspace/native-tool-marker").read_text() == "verified"
        assert result["usage"]["api_calls"] == 2
        assert result["usage"]["total_tokens"] == 30
        assert result["tool_calls_are_executed"]
        print(json.dumps({"native_sdk": True, "native_tool_executed": True,
                          "api_calls": 2, "total_tokens": 30, "reply": "done"}))
finally:
    bridge.end_flow()
