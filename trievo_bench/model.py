from __future__ import annotations

import http.client
import json
import logging
import os
import re
import ssl
import time
import random
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


class _TransientModelResponseError(RuntimeError):
    """A successful HTTP response did not contain one usable model message."""


_RETRYABLE_HTTP_STATUS = {408, 409, 429, *range(500, 600)}
_RETRYABLE_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    TimeoutError,
    ConnectionError,
    ssl.SSLError,
    UnicodeDecodeError,
    _TransientModelResponseError,
)


class TokenBudgetExceeded(RuntimeError):
    pass


@dataclass
class TokenBudget:
    limit: int = 3_000_000
    used: int = 0
    calls: int = 0

    def preflight(self, estimated_input: int, max_output: int) -> None:
        if self.used + estimated_input + max_output > self.limit:
            raise TokenBudgetExceeded(f"token budget would exceed {self.limit}: used={self.used}, requested={estimated_input + max_output}")

    def record(self, total_tokens: int, *, calls: int = 1) -> None:
        self.used += max(0, int(total_tokens))
        self.calls += max(0, int(calls))
        if self.used > self.limit:
            raise TokenBudgetExceeded(f"token budget exceeded: {self.used} > {self.limit}")


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass
class ModelReply:
    text: str
    actions: list[dict[str, Any]]
    state_update: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None


def parse_reply(text: str) -> ModelReply:
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            loose = _parse_loose_action(text)
            return ModelReply(text=text, actions=loose, state_update={}, raw=text)
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            loose = _parse_loose_action(text)
            return ModelReply(text=text, actions=loose, state_update={}, raw=text)
    if not isinstance(payload, dict):
        return ModelReply(text=text, actions=[], state_update={}, raw=payload)
    actions: list[dict[str, Any]] = []
    raw_actions = payload.get("actions", [])
    if isinstance(raw_actions, dict):
        raw_actions = [raw_actions]
    for raw_action in raw_actions if isinstance(raw_actions, list) else []:
        if not isinstance(raw_action, dict):
            continue
        if isinstance(raw_action.get("parameters"), dict):
            raw_action = {**raw_action, **raw_action["parameters"]}
        op = raw_action.get("op") or raw_action.get("type") or raw_action.get("action")
        if op in {"write", "file_write", "write_file"}:
            path = raw_action.get("path") or raw_action.get("file") or raw_action.get("filename") or raw_action.get("file_path") or raw_action.get("target")
            content = raw_action.get("content")
            if content is None:
                content = raw_action.get("contents", raw_action.get("text", raw_action.get("body", "")))
            actions.append({"op": "write_file", "path": path, "content": content})
        elif op in {"remove", "file_remove", "delete", "delete_file"}:
            path = raw_action.get("path") or raw_action.get("file") or raw_action.get("filename") or raw_action.get("file_path") or raw_action.get("target")
            actions.append({"op": "remove", "path": path})
    tool_calls = payload.get("tool_calls", payload.get("tools", []))
    events = payload.get("events", [])
    raw_state_update = payload.get("state_update", payload.get("state", {}))
    # Model-produced JSON is untrusted protocol input. A list/string here used
    # to make ``dict(...)`` raise and discard an otherwise usable response.
    state_update = raw_state_update if isinstance(raw_state_update, dict) else {}
    return ModelReply(
        text=str(payload.get("response", payload.get("text", text))),
        actions=actions,
        state_update=dict(state_update),
        tool_calls=list(tool_calls) if isinstance(tool_calls, list) else [],
        events=list(events) if isinstance(events, list) else [],
        raw=payload,
    )


def _parse_loose_action(text: str) -> list[dict[str, Any]]:
    """Parse only the common path/action/content YAML-like fallback."""
    path_match = re.search(r"(?:path|file|filename)\s*:\s*([^\s]+)", text, flags=re.IGNORECASE)
    op_match = re.search(r"(?:op|type|action)\s*:\s*['\"]?(write|file_write|write_file|remove|delete|delete_file)['\"]?", text, flags=re.IGNORECASE)
    if not path_match or not op_match:
        return []
    path = path_match.group(1).strip().strip('"\'')
    op = op_match.group(1).lower()
    if op in {"remove", "delete", "delete_file"}:
        return [{"op": "remove", "path": path}]
    content_match = re.search(r"content\s*:\s*(['\"])(.*?)\1", text, flags=re.IGNORECASE | re.DOTALL)
    content = content_match.group(2) if content_match else ""
    if "\\n" in content:
        content = content.replace("\\n", "\n")
    return [{"op": "write_file", "path": path, "content": content}]


class OpenAICompatibleClient:
    """Small dependency-free client for OpenAI-compatible chat-completions APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        budget: TokenBudget,
        timeout: int = 120,
        thinking: str | None = None,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.budget = budget
        self.timeout = timeout
        self.thinking = thinking
        self.max_retries = max_retries

    @staticmethod
    def _retry_delay(attempt: int, error: BaseException) -> float:
        if isinstance(error, urllib.error.HTTPError):
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if retry_after:
                try:
                    return min(60.0, max(0.0, float(retry_after)))
                except ValueError:
                    pass
        return min(30.0, float(2 ** (attempt + 1)))

    def _wait_for_retry(self, attempt: int, error: BaseException) -> None:
        delay = self._retry_delay(attempt, error)
        if os.environ.get("TRIEVO_SLOW_PROVIDER") == "1":
            delay += random.uniform(0, min(5.0, delay / 4))
        logger.warning(
            "Transient model request failure; retrying attempt %d/%d in %.1fs (%s)",
            attempt + 2,
            self.max_retries + 1,
            delay,
            type(error).__name__,
        )
        time.sleep(delay)

    @staticmethod
    def _message_from_result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise _TransientModelResponseError("response root is not an object")
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _TransientModelResponseError("response has no usable choice")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise _TransientModelResponseError("response choice has no usable message")
        usage = result.get("usage", {})
        if usage is not None and not isinstance(usage, dict):
            raise _TransientModelResponseError("response usage is not an object")
        return message

    def _record_reported_usage(self, result: Any) -> bool:
        if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
            return False
        total = result["usage"].get("total_tokens")
        try:
            parsed = int(total)
        except (TypeError, ValueError):
            return False
        if parsed < 0:
            return False
        self.budget.record(parsed, calls=0)
        return True

    def complete(self, messages: list[dict[str, str]], max_tokens: int = 1024, json_mode: bool = False) -> str:
        serialized = json.dumps(messages, ensure_ascii=False)
        self.budget.preflight(estimate_tokens(serialized), max_tokens)
        payload = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": self.thinking}
        elif self.thinking in {"low", "medium", "high", "max"}:
            # GLM-5.3-Flash is a mandatory-thinking model. Its OpenAI-
            # compatible endpoint selects effort with the top-level
            # reasoning_effort field; sending thinking.type=low yields 1210.
            payload["reasoning_effort"] = self.thinking
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        result: dict[str, Any] | None = None
        message: dict[str, Any] | None = None
        result_usage_recorded = False
        for attempt in range(self.max_retries + 1):
            # Provider attempts are infrastructure accounting, not benchmark
            # Interactions. Count failed attempts even when they report no usage.
            self.budget.record(0, calls=1)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    candidate = json.loads(response.read().decode("utf-8"))
                attempt_usage_recorded = self._record_reported_usage(candidate)
                message = self._message_from_result(candidate)
                result = candidate
                result_usage_recorded = attempt_usage_recorded
                break
            except urllib.error.HTTPError as exc:
                if exc.code in _RETRYABLE_HTTP_STATUS and attempt < self.max_retries:
                    self._wait_for_retry(attempt, exc)
                    continue
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    detail = ""
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(f"OpenAI-compatible model request failed (HTTP {exc.code}){suffix}") from exc
            except (*_RETRYABLE_TRANSPORT_ERRORS, json.JSONDecodeError) as exc:
                if attempt < self.max_retries:
                    self._wait_for_retry(attempt, exc)
                    continue
                raise RuntimeError("OpenAI-compatible model request failed") from exc
        if result is None or message is None:
            raise RuntimeError("OpenAI-compatible model request failed after retries")
        if not result_usage_recorded:
            self.budget.record(
                estimate_tokens(serialized) + estimate_tokens(str(result)),
                calls=0,
            )
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        if not content and not json_mode:
            content = message.get("reasoning_content", "")
        if not content:
            raise RuntimeError("OpenAI-compatible model returned empty content")
        return str(content)


def client_from_config(config: dict[str, Any], budget: TokenBudget) -> OpenAICompatibleClient:
    base_url = str(config.get("base_url") or os.environ.get("TRIEVO_BASE_URL", "https://api.deepseek.com"))
    api_key = str(config.get("api_key") or os.environ.get("TRIEVO_API_KEY", ""))
    model = str(config.get("model") or os.environ.get("TRIEVO_MODEL", "deepseek-chat"))
    if not api_key:
        raise RuntimeError("missing API key: set TRIEVO_API_KEY or agent/judge api_key")
    thinking = str(config.get("thinking") or os.environ.get("TRIEVO_THINKING", "")).lower() or None
    timeout = int(config.get("timeout") or os.environ.get("TRIEVO_API_TIMEOUT_SECONDS", 120))
    max_retries = int(config.get("max_retries") if config.get("max_retries") is not None else os.environ.get("TRIEVO_API_MAX_RETRIES", 2))
    if not 1 <= timeout <= 3600:
        raise ValueError("model timeout must be between 1 and 3600 seconds")
    if not 0 <= max_retries <= (100 if os.environ.get("TRIEVO_SLOW_PROVIDER") == "1" else 20):
        raise ValueError("model max_retries exceeds the configured retry range")
    return OpenAICompatibleClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        budget=budget,
        timeout=timeout,
        thinking=thinking,
        max_retries=max_retries,
    )
