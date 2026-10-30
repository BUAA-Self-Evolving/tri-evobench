"""Native SDK notification-deadline test using a blocked fake provider only."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge

released = threading.Event()


class Provider(BaseHTTPRequestHandler):
    calls = 0
    def log_message(self, *args):
        pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        Provider.calls += 1
        released.wait(15)
        self.send_response(503)
        self.end_headers()


server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
os.environ["TRIEVO_API_KEY"] = "fake-deadline-test"
try:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        bridge = DeepSeekHarnessBridge()
        bridge.initialize({"protocol_version": "1.0", "baseline_id": "deepseek-harness",
            "options": {"model": "deepseek-v4-flash", "provider_transport": "friday_chat",
            "executable": "/opt/deepseek-harness/apps/cli/lib/bin.js",
            "base_url": f"http://127.0.0.1:{server.server_port}", "api_key_env": "TRIEVO_API_KEY",
            "max_output_tokens": 4096, "request_timeout_seconds": 5}})
        bridge.start_flow({"flow_id": "deadline-test", "workspace": str(workspace),
                           "state_dir": str(Path(temporary) / "state")})
        bridge.start_turn({"turn_id": "turn1", "metadata": {"agent_limits": {"episode_timeout_seconds": 1}}})
        started = time.monotonic()
        result = bridge.interact({"turn_id": "turn1", "request": "Say hello.", "remaining_token_budget": 1000})
        elapsed = time.monotonic() - started
        assert elapsed < 10, elapsed
        assert result["raw"]["execution_error"]["type"] == "adapter_deadline"
        assert not result["raw"].get("limit_exhausted")
        assert Provider.calls == 1, Provider.calls
        assert result["events"]  # Actual native inbox/status evidence is retained.
        released.set()
        bridge.end_flow()
        print(json.dumps({"native_sdk_deadline": True, "provider_calls": Provider.calls,
                          "replayed": 0, "elapsed_seconds": round(elapsed, 2)}))
finally:
    released.set()
    server.shutdown()
    server.server_close()
    thread.join()
