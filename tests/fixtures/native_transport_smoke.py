from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge
from baseline.adapters.zcode.bridge import ZCodeBridge


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/messages"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            rows = [
                {"type": "message_start", "message": {"id": "mock", "model": request["model"],
                    "usage": {"input_tokens": 8, "output_tokens": 0, "cache_read_input_tokens": 2}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"},
            ]
            for row in rows:
                self.wfile.write(("event: " + row["type"] + "\ndata: " + json.dumps(row) + "\n\n").encode())
            return
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                                         "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8}},
            ]
            for chunk in chunks:
                chunk.update({"id": "mock", "object": "chat.completion.chunk", "created": 1, "model": request["model"]})
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "mock", "object": "chat.completion", "created": 1,
                "model": request["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
threading.Thread(target=server.serve_forever, daemon=True).start()
root = Path(__file__).resolve().parents[2]
os.environ["TRIEVO_API_KEY"] = "mock-key"
for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(name, None)
baseline = sys.argv[1]
def diagnostic_harness(**kwargs):
    from deepseek_harness import DeepSeekHarness
    harness = DeepSeekHarness(**kwargs)
    original_run = harness.run
    def run(*args, **run_kwargs):
        result = original_run(*args, **run_kwargs)
        if result.finish_reason != "completed":
            print(json.dumps(result.events, indent=2), file=sys.stderr)
        return result
    harness.run = run
    return harness
bridge = DeepSeekHarnessBridge(diagnostic_harness) if baseline == "deepseek-harness" else ZCodeBridge()
options = {"model": "deepseek-v4-flash" if baseline == "deepseek-harness" else "glm-5",
           "base_url": f"http://127.0.0.1:{server.server_port}/v1", "request_timeout_seconds": 30,
           "reasoning_effort": "high" if baseline == "deepseek-harness" else None}
if baseline == "deepseek-harness":
    options["executable"] = str(root / "baseline/deepseek-harness/apps/cli/lib/bin.js")
else:
    options["executable"] = ["node", str(root / "baseline/ZCode/apps/zcode-cli/packages/cli/dist/zcode.cjs")]
    options["builtin_config"] = str(root / "baseline/ZCode/config/provider/zcode-builtin.json")
with tempfile.TemporaryDirectory() as temporary:
    bridge.initialize({"protocol_version": "1.0", "baseline_id": baseline, "options": options})
    bridge.start_flow({"workspace": temporary + "/workspace", "state_dir": temporary + "/state"})
    bridge.start_turn({"turn_id": "t1"})
    try:
        first = bridge.interact({"turn_id": "t1", "request": "Reply done"})
        second = bridge.interact({"turn_id": "t1", "request": "Reply done again"})
        assert first["text"] == second["text"] == "done"
        assert first["usage"]["total_tokens"] == second["usage"]["total_tokens"] == 15
        print(json.dumps({"baseline": baseline, "first_usage": first["usage"], "second_usage": second["usage"], "same_session": first["raw"] == second["raw"]}))
    finally:
        bridge.end_flow()
server.shutdown()
