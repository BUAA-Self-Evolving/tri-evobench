"""Exercise the real DSH SDK with a local, credential-free Chat provider."""
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import baseline.adapters.deepseek_harness.bridge as adapter
from baseline.adapters.deepseek_harness.friday_messages import FridayMessagesRelay


class Provider(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions"
        assert request["model"] == "deepseek-v4-flash"
        type(self).calls += 1
        body = {"id": "fake", "model": "deepseek-flash", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                      "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8}}
        if self.calls == 1:
            assert any(tool["function"]["name"] == "bash" for tool in request["tools"])
            body["choices"][0].update(finish_reason="tool_calls", message={"role": "assistant", "content": None,
                "reasoning_content": "Use the supplied shell.", "tool_calls": [
                    {"id": "fake-bash", "type": "function", "function": {"name": "bash",
                        "arguments": json.dumps({"command": "printf verified > native-tool-marker"})}}]})
        elif self.calls == 2:
            assert any(message.get("role") == "tool" and message["tool_call_id"] == "fake-bash"
                       for message in request["messages"])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["TRIEVO_API_KEY"] = "local-test-only"
adapter.FridayMessagesRelay = lambda *args, **kwargs: FridayMessagesRelay(*args, **kwargs, min_interval=0)
bridge = adapter.DeepSeekHarnessBridge()
try:
    with tempfile.TemporaryDirectory() as temporary:
        bridge.initialize({"protocol_version": "1.0", "baseline_id": "deepseek-harness", "options": {
            "executable": "/opt/deepseek-harness/apps/cli/lib/bin.js", "model": "deepseek-v4-flash",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1", "provider_transport": "friday_chat",
            "max_output_tokens": 4096, "request_timeout_seconds": 30}})
        bridge.start_flow({"workspace": temporary + "/workspace", "state_dir": temporary + "/state"})
        bridge.start_turn({"turn_id": "t1"})
        first = bridge.interact({"turn_id": "t1", "request": "Reply done"})
        second = bridge.interact({"turn_id": "t1", "request": "Reply done again"})
        assert first["text"] == second["text"] == "done"
        assert first["raw"] == second["raw"]
        for result, calls in ((first, 2), (second, 1)):
            assert result["usage"]["total_tokens"] == 15 * calls, result["usage"]
            assert result["usage"]["input_tokens"] == 10 * calls, result["usage"]
            assert result["usage"]["cached_read_tokens"] == 2 * calls, result["usage"]
            assert result["usage"]["api_calls"] == calls, result["usage"]
        assert Path(temporary + "/workspace/native-tool-marker").read_text() == "verified"
        assert len(first["tool_calls"]) == 1 and first["tool_calls_are_executed"]
        assert Provider.calls == 3
        print(json.dumps({"native_sdk": True, "same_session": True, "chat_calls": Provider.calls,
                          "usage": first["usage"]}))
finally:
    bridge.end_flow()
    server.shutdown()
