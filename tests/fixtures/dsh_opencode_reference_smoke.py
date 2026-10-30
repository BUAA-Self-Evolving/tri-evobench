"""Real native DSH, local fake provider: >12 calls and cross-turn persistence."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge


class Provider(BaseHTTPRequestHandler):
    calls = 0
    def log_message(self, *args):
        pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.calls += 1
        n = Provider.calls
        assert request["max_tokens"] == 4096
        message = {"role": "assistant", "content": "reference done"}
        reason = "stop"
        if n <= 13:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call-{n}", "type": "function", "function": {"name": "bash",
                "arguments": json.dumps({"command": "printf verified > native-reference-marker"})}}]}
            reason = "tool_calls"
        if n == 15:
            assert any(m.get("role") == "assistant" and m.get("content") == "reference done"
                       for m in request["messages"]), "native history lost across turns"
        body = {"id": f"fake-{n}", "model": "deepseek-flash", "choices": [
            {"index": 0, "message": message, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["TRIEVO_API_KEY"] = "local-fake-provider-only"
bridge = DeepSeekHarnessBridge()
try:
    with tempfile.TemporaryDirectory() as temporary:
        bridge.initialize({"protocol_version": "1.0", "baseline_id": "deepseek-harness", "options": {
            "execution_profile": "opencode-full-stream-20260928", "provider_transport": "friday_chat",
            "executable": "/opt/deepseek-harness/apps/cli/lib/bin.js", "model": "deepseek-v4-flash",
            "max_output_tokens": 4096, "request_timeout_seconds": 60,
            "patches": ["/opt/trievo/baseline/adapters/deepseek_harness/opencode_reference.patch.yml"],
            "base_url": f"http://127.0.0.1:{server.server_port}"}})
        flow = {"flow_id": "reference-smoke", "workspace": temporary + "/workspace",
                "state_dir": temporary + "/state"}
        bridge.start_flow(flow)
        bridge.start_turn({"turn_id": "t1"})
        started = time.monotonic()
        first = bridge.interact({"turn_id": "t1", "request": "Create the marker, then finish."})
        assert len(first["tool_calls"]) == 13
        assert Provider.calls == 14
        assert time.monotonic() - started < 45, "unwanted 60s request throttle"
        bridge.evolve({"turn_id": "t1"})
        bridge.start_turn({"turn_id": "t2"})
        second = bridge.interact({"turn_id": "t2", "request": "Confirm done again."})
        assert first["raw"]["session_id"] == second["raw"]["session_id"]
        assert Provider.calls == 15
        assert Path(flow["workspace"], "native-reference-marker").read_text() == "verified"
        bridge.evolve({"turn_id": "t2"})
        bridge.end_flow()
        # Recreate the SDK process from retained DSH JSONL, as a checkpoint resume does.
        assert bridge.start_flow(flow)["resumed_session"]
        bridge.start_turn({"turn_id": "t3"})
        third = bridge.interact({"turn_id": "t3", "request": "Confirm after process restart."})
        assert third["raw"]["session_id"] == first["raw"]["session_id"]
        assert Provider.calls == 16
        print(json.dumps({"native_sdk": True, "provider_calls": Provider.calls,
                          "cross_turn_session": True, "restart_session": True,
                          "model_call_cap_matches_opencode": True, "paid_calls": 0}))
finally:
    bridge.end_flow()
    server.shutdown()
    server.server_close()
