"""In-memory Messages-to-Chat transport for the unmodified native DSH SDK.

The relay executes no tools, persists no requests, and performs no retries.
Native DSH retains control of tools, sessions, prompts and model decisions.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def content_part(block: dict[str, Any]) -> dict[str, Any]:
    if block.get("type") == "text":
        return {"type": "text", "text": block["text"]}
    if block.get("type") == "image":
        source = block["source"]
        if source["type"] == "base64":
            url = "data:" + source["media_type"] + ";base64," + source["data"]
        elif source["type"] == "url":
            url = source["url"]
        else:
            raise ValueError("unsupported Messages image source")
        return {"type": "image_url", "image_url": {"url": url}}
    raise ValueError("unsupported Messages content block")


def to_chat(request: dict[str, Any], model: str, output_cap: int) -> dict[str, Any]:
    if request.get("model") != model:
        raise ValueError("model identity differs from configured model")
    messages = []
    system = request.get("system")
    if system:
        text = system if isinstance(system, str) else "\n".join(block["text"] for block in system if block["type"] == "text")
        messages.append({"role": "system", "content": text})
    for message in request["messages"]:
        role = message["role"]
        if role not in {"user", "assistant"}:
            raise ValueError("unsupported Messages role")
        blocks = message["content"]
        if isinstance(blocks, str):
            messages.append({"role": role, "content": blocks})
            continue
        parts, calls, thinking = [], [], []
        for block in blocks:
            kind = block["type"]
            if kind == "tool_result":
                if role != "user":
                    raise ValueError("tool results must belong to user messages")
                value = block.get("content", "")
                if isinstance(value, list):
                    if any(item.get("type") != "text" for item in value):
                        raise ValueError("non-text tool result is not supported by this transport")
                    value = "\n".join(item["text"] for item in value)
                messages.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": value})
            elif kind == "tool_use":
                if role != "assistant":
                    raise ValueError("tool calls must belong to assistant messages")
                calls.append({"id": block["id"], "type": "function", "function": {
                    "name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False)}})
            elif kind == "thinking":
                thinking.append(block["thinking"])
            else:
                parts.append(content_part(block))
        if parts or calls or thinking:
            text_only = all(part["type"] == "text" for part in parts)
            item = {"role": role, "content": "\n".join(part["text"] for part in parts) if text_only else parts}
            if calls:
                item["tool_calls"] = calls
            if thinking:
                item["reasoning_content"] = "".join(thinking)
            messages.append(item)
    result = {"model": model, "messages": messages, "stream": False,
              "max_tokens": min(int(request.get("max_tokens", output_cap)), output_cap),
              "thinking": {"type": "enabled"}}
    tools = request.get("tools", [])
    if tools:
        result["tools"] = [{"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool["input_schema"]}} for tool in tools]
    choice = request.get("tool_choice", {})
    if choice:
        kind = choice["type"]
        result["tool_choice"] = ({"type": "function", "function": {"name": choice["name"]}}
                                 if kind == "tool" else {"any": "required", "auto": "auto", "none": "none"}[kind])
    if "temperature" in request:
        result["temperature"] = request["temperature"]
    return result


def to_message(response: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choices = response.get("choices")
    usage = response.get("usage")
    if not choices or not isinstance(usage, dict):
        raise ValueError("upstream reply lacks choices or actual usage")
    choice = choices[0]
    message = choice["message"]
    blocks = []
    reasoning = message.get("reasoning_content")
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = message.get("content")
    if text:
        if not isinstance(text, str):
            raise ValueError("non-text upstream assistant content")
        blocks.append({"type": "text", "text": text})
    for tool in message.get("tool_calls", []):
        function = tool["function"]
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        blocks.append({"type": "tool_use", "id": tool["id"], "name": function["name"], "input": arguments})
    if not blocks:
        raise ValueError("upstream returned an empty assistant reply")
    reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(choice.get("finish_reason"))
    if reason is None:
        raise ValueError("unsupported upstream finish reason")
    total_input = int(usage["prompt_tokens"])
    cached = int(usage.get("prompt_tokens_details", {}).get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0)))
    if cached < 0 or cached > total_input:
        raise ValueError("upstream cached input exceeds total input")
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
            "model": response.get("model", requested_model), "content": blocks,
            "stop_reason": reason, "stop_sequence": None,
            "usage": {"input_tokens": total_input - cached, "output_tokens": int(usage["completion_tokens"]),
                      "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}}


def stream_events(message: dict[str, Any]):
    start = {**message, "content": [], "stop_reason": None,
             "usage": {**message["usage"], "output_tokens": 0}}
    yield {"type": "message_start", "message": start}
    for index, block in enumerate(message["content"]):
        kind = block["type"]
        initial = ({**block, "input": {}} if kind == "tool_use" else
                   {**block, "text": ""} if kind == "text" else {**block, "thinking": "", "signature": ""})
        yield {"type": "content_block_start", "index": index, "content_block": initial}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)} if kind == "tool_use" else
                 {"type": "text_delta", "text": block["text"]} if kind == "text" else
                 {"type": "thinking_delta", "thinking": block["thinking"]})
        yield {"type": "content_block_delta", "index": index, "delta": delta}
        if kind == "thinking":
            yield {"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": ""}}
        yield {"type": "content_block_stop", "index": index}
    yield {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
           "usage": message["usage"]}
    yield {"type": "message_stop"}


class FridayMessagesRelay:
    def __init__(self, base_url: str, key: str, model: str, output_cap: int,
                 timeout: float = 300, max_calls: int | None = 12, min_interval: float = 60):
        self.base_url, self.key, self.model = base_url.rstrip("/"), key, model
        self.output_cap, self.timeout, self.max_calls = output_cap, timeout, max_calls
        self.min_interval, self.last_request, self.calls = min_interval, 0.0, 0
        self.accepting = True
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                if self.path not in {"/messages", "/v1/messages"}:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 32 * 1024 * 1024:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length))
                    payload = to_chat(body, relay.model, relay.output_cap)
                    with relay.lock:
                        if not relay.accepting:
                            self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "interaction closed; no further provider admission"}})
                            return
                        if relay.max_calls is not None and relay.calls >= relay.max_calls:
                            self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "model call limit reached"}})
                            return
                        delay = max(0, relay.last_request + relay.min_interval - time.monotonic())
                        if delay:
                            time.sleep(delay)
                        if not relay.accepting:
                            self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "interaction closed; no further provider admission"}})
                            return
                        relay.calls += 1
                        relay.last_request = time.monotonic()
                        request = urllib.request.Request(relay.base_url + "/chat/completions",
                            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + relay.key})
                        with relay.opener.open(request, timeout=relay.timeout) as response:
                            message = to_message(json.load(response), relay.model)
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for event in stream_events(message):
                            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                            self.wfile.flush()
                        self.close_connection = True
                    else:
                        self.send_json(200, message)
                except urllib.error.HTTPError as exc:
                    # Never return upstream bodies/headers that might echo a credential.
                    self.send_json(exc.code, {"type": "error", "error": {"type": "api_error", "message": f"Friday HTTP {exc.code}; no relay retry"}})
                except Exception as exc:
                    self.send_json(502, {"type": "error", "error": {"type": "api_error", "message": type(exc).__name__}})
            def send_json(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.key = ""
