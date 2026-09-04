"""OpenAI-compatible text model client with secret-safe configuration."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    usage: dict[str, int | None]
    request_id: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    api_retry_count: int = 0


def normalize_base_url(value: str) -> str:
    """Return an OpenAI-compatible API root, adding ``/v1`` to an origin."""

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API base URL must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise ValueError("API base URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class OpenAIChatModel:
    """Small Chat Completions adapter used by both the Agent and learner."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "medium",
        request_timeout_seconds: float = 180.0,
        max_api_retries: int = 4,
        retry_backoff_seconds: float = 15.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("L2_API_KEY is required")
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_api_retries = max_api_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=request_timeout_seconds,
            max_retries=0,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str | None = None,
        model: str | None = None,
        reasoning_effort: str = "medium",
    ) -> "OpenAIChatModel":
        api_key = os.environ.get("L2_API_KEY", "")
        if not api_key:
            raise RuntimeError("L2_API_KEY is required")
        resolved_base = base_url or os.environ.get("L2_API_BASE", "")
        if not resolved_base:
            raise RuntimeError("L2_API_BASE is required")
        resolved_model = model or os.environ.get("L2_MODEL", "gpt-5.6-terra")
        return cls(
            api_key=api_key,
            base_url=resolved_base,
            model=resolved_model,
            reasoning_effort=reasoning_effort,
        )

    def list_model_ids(self) -> list[str]:
        return sorted(model.id for model in self._client.models.list().data)

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        episode_id: str,
        attempt: int,
    ) -> ModelResponse:
        # The demo asks for a short learner update and one compact code/JSON
        # deliverable.  Reserving 6k output tokens per call needlessly consumes
        # provider capacity and can trigger opaque upstream admission failures.
        max_tokens = 800 if purpose == "learner" else 3000
        last_error: Exception | None = None
        for retry in range(self.max_api_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    reasoning_effort=self.reasoning_effort,
                    max_completion_tokens=max_tokens,
                )
                text = response.choices[0].message.content or ""
                if not text.strip():
                    raise RuntimeError(
                        f"empty model response for {purpose} {episode_id} attempt {attempt}"
                    )
                usage = response.usage
                usage_dict = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
                return ModelResponse(
                    text=text,
                    model=response.model,
                    usage=usage_dict,
                    request_id=getattr(response, "_request_id", None),
                    response_id=getattr(response, "id", None),
                    finish_reason=getattr(response.choices[0], "finish_reason", None),
                    api_retry_count=retry,
                )
            except Exception as exc:  # SDK exposes several provider-specific errors.
                last_error = exc
                if retry == self.max_api_retries or not _is_retryable_api_error(exc):
                    break
                time.sleep(min(self.retry_backoff_seconds * (2**retry), 30.0))
        raise RuntimeError(
            f"model request failed for {purpose} {episode_id} attempt {attempt}: "
            f"{type(last_error).__name__}: {last_error} "
            f"(after {retry + 1} API call(s))"
        ) from last_error


def _is_retryable_api_error(exc: Exception) -> bool:
    """Retry transport/transient statuses, including this provider's wrapped 400.

    Ordinary malformed-request 400s are deliberately not retried.  DragTokens
    currently wraps an upstream availability failure as HTTP 400 with the exact
    message ``Upstream request failed``; treating only that signature as
    transient keeps genuine contract errors visible immediately.
    """

    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 429}:
        return True
    if isinstance(status_code, int) and status_code >= 500:
        return True
    if status_code == 400 and "upstream request failed" in str(exc).lower():
        return True
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}
