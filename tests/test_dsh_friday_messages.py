import json
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from baseline.adapters.deepseek_harness.friday_messages import FridayMessagesRelay, to_chat, to_message, stream_events


def response():
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": None, "reasoning_content": "Need to inspect.", "tool_calls": [{
            "id": "call_1", "function": {"name": "shell", "arguments": '{"command":"ls"}'}}]}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 12}}}


def test_roundtrip_reasoning_tools_results_and_cache_usage():
    original = response()
    native = to_message(original, "deepseek-v4-flash")
    assert native["usage"] == {"input_tokens": 8, "output_tokens": 7,
        "cache_read_input_tokens": 12, "cache_creation_input_tokens": 0}
    assert native["stop_reason"] == "tool_use"
    payload = to_chat({"model": "deepseek-v4-flash", "max_tokens": 8000,
        "system": [{"type": "text", "text": "system"}], "messages": [
            {"role": "assistant", "content": native["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "a.txt"}]}],
        "tools": [{"name": "shell", "input_schema": {"type": "object"}}]}, "deepseek-v4-flash", 4096)
    assistant = payload["messages"][1]
    assert assistant["reasoning_content"] == "Need to inspect."
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}
    assert payload["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}
    assert payload["max_tokens"] == 4096
    events = list(stream_events(native))
    assert events[0]["type"] == "message_start"
    assert events[-1]["type"] == "message_stop"
    assert events[-2]["usage"]["output_tokens"] == 7
    assert [e["delta"]["partial_json"] for e in events if e.get("delta", {}).get("type") == "input_json_delta"] == ['{"command": "ls"}']


def test_missing_usage_empty_reply_and_bad_tool_json_fail_closed():
    value = response()
    value.pop("usage")
    with pytest.raises(ValueError, match="actual usage"):
        to_message(value, "deepseek-v4-flash")
    value = response()
    value["choices"][0]["message"] = {"content": ""}
    with pytest.raises(ValueError, match="empty"):
        to_message(value, "deepseek-v4-flash")
    value = response()
    value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "bad"
    with pytest.raises(ValueError):
        to_message(value, "deepseek-v4-flash")


def test_http_stream_transport_and_call_limit_do_not_replay():
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            assert self.headers["Authorization"] == "Bearer fake-test-key"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response()).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = FridayMessagesRelay(f"http://127.0.0.1:{server.server_port}", "fake-test-key",
        "deepseek-v4-flash", 4096, min_interval=0, max_calls=1)
    url = relay.start() + "/v1/messages"
    request = urllib.request.Request(url, data=json.dumps({"model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "inspect"}], "stream": True}).encode())
    try:
        with urllib.request.urlopen(request) as result:
            raw = result.read().decode()
        assert "event: message_start" in raw and "event: message_stop" in raw
        assert "fake-test-key" not in raw
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request)
        assert failure.value.code == 400
        assert len(requests) == 1
        assert requests[0]["stream"] is False
    finally:
        relay.close()
        server.shutdown()
        server.server_close()
        thread.join()
