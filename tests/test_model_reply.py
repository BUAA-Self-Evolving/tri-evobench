from __future__ import annotations

import http.client
import io
import json
import time
import urllib.error

import pytest

from trievo_bench.model import OpenAICompatibleClient, TokenBudget, client_from_config, parse_reply


def test_parse_reply_ignores_malformed_state_update() -> None:
    reply = parse_reply(json.dumps({
        "response": "usable answer",
        "actions": [],
        "state_update": ["not", "a", "mapping"],
    }))

    assert reply.text == "usable answer"
    assert reply.state_update == {}


def test_parse_reply_accepts_legacy_top_level_action_array() -> None:
    reply = parse_reply(json.dumps([
        {
            "type": "write",
            "path": "/workspace/report.json",
            "content": '{"status":"ready"}',
        }
    ]))

    assert reply.actions == [{
        "op": "write_file",
        "path": "/workspace/report.json",
        "content": '{"status":"ready"}',
    }]


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _success_response() -> _Response:
    return _Response({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 3},
    })


def test_openai_client_retries_incomplete_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [http.client.IncompleteRead(b'{"choices":'), _success_response()]
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("trievo_bench.model.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("trievo_bench.model.time.sleep", lambda _: None)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="secret",
        model="test-model",
        budget=TokenBudget(5000),
        timeout=17,
        max_retries=2,
    )

    assert client.complete([{"role": "user", "content": "hello"}]) == "ok"
    assert calls == [17, 17]
    assert client.budget.calls == 2


def test_openai_client_absolute_deadline_stops_continuous_body(monkeypatch) -> None:
    class EndlessResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            time.sleep(0.012)
            return b"x"

    monkeypatch.setattr(
        "trievo_bench.model.urllib.request.urlopen",
        lambda request, timeout: EndlessResponse(),
    )
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="must-not-appear",
        model="test-model",
        budget=TokenBudget(5000),
        timeout=0.03,
        max_retries=0,
    )

    with pytest.raises(RuntimeError, match="absolute deadline.*chunks=.*bytes=") as exc_info:
        client.complete([{"role": "user", "content": "private prompt"}])
    message = str(exc_info.value)
    assert "must-not-appear" not in message
    assert "private prompt" not in message
    assert "example.test" not in message


def test_openai_client_does_not_retry_oversized_success_body(monkeypatch) -> None:
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return io.BytesIO(b"x" * 64)

    monkeypatch.setattr("trievo_bench.model.urllib.request.urlopen", urlopen)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="secret",
        model="test-model",
        budget=TokenBudget(5000),
        max_retries=5,
        max_response_bytes=32,
    )

    with pytest.raises(RuntimeError, match="safe body limit"):
        client.complete([{"role": "user", "content": "hello"}])
    assert calls == 1


def test_retry_delay_uses_larger_of_backoff_and_retry_after() -> None:
    error = urllib.error.HTTPError(
        "https://example.test",
        429,
        "rate limited",
        {"Retry-After": "1"},
        io.BytesIO(),
    )
    assert OpenAICompatibleClient._retry_delay(2, error) == 8.0
    error.headers["Retry-After"] = "50"
    assert OpenAICompatibleClient._retry_delay(2, error) == 50.0
    error.headers["Retry-After"] = "500"
    assert OpenAICompatibleClient._retry_delay(2, error) == 60.0


def test_openai_client_does_not_retry_permanent_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "bad request",
            {},
            io.BytesIO(b'{"error":"invalid request"}'),
        )

    monkeypatch.setattr("trievo_bench.model.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("trievo_bench.model.time.sleep", lambda _: None)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="secret",
        model="test-model",
        budget=TokenBudget(5000),
        max_retries=4,
    )

    with pytest.raises(RuntimeError, match="HTTP 400"):
        client.complete([{"role": "user", "content": "hello"}])
    assert calls == 1
    assert client.budget.calls == 1


def test_openai_client_retries_malformed_success_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [_Response({"usage": {"total_tokens": 11}, "choices": []}), _success_response()]
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return outcomes.pop(0)

    monkeypatch.setattr("trievo_bench.model.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("trievo_bench.model.time.sleep", lambda _: None)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="secret",
        model="test-model",
        budget=TokenBudget(5000),
        max_retries=2,
    )

    assert client.complete([{"role": "user", "content": "hello"}]) == "ok"
    assert calls == 2
    assert client.budget.calls == 2
    assert client.budget.used == 14


def test_openai_client_surfaces_friday_business_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trievo_bench.model.urllib.request.urlopen",
        lambda request, timeout: _Response({"status": 400, "message": "invalid app authorization"}),
    )
    client = OpenAICompatibleClient(
        base_url="https://aigc.sankuai.com/v1/openai/native",
        api_key="test-app-id",
        model="deepseek-v4-flash",
        budget=TokenBudget(5000),
        max_retries=3,
    )

    with pytest.raises(RuntimeError, match="status 400.*invalid app authorization"):
        client.complete([{"role": "user", "content": "hello"}])
    assert client.budget.calls == 1


def test_openai_client_retries_friday_business_429(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [_Response({"status": 429, "message": "rate limited"}), _success_response()]
    monkeypatch.setattr(
        "trievo_bench.model.urllib.request.urlopen",
        lambda request, timeout: outcomes.pop(0),
    )
    monkeypatch.setattr("trievo_bench.model.time.sleep", lambda _: None)
    client = OpenAICompatibleClient(
        base_url="https://aigc.sankuai.com/v1/openai/native",
        api_key="test-app-id",
        model="deepseek-v4-flash",
        budget=TokenBudget(5000),
        max_retries=2,
    )

    assert client.complete([{"role": "user", "content": "hello"}]) == "ok"
    assert client.budget.calls == 2


def test_client_from_config_reads_relay_retry_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIEVO_API_KEY", "secret")
    monkeypatch.setenv("TRIEVO_API_TIMEOUT_SECONDS", "150")
    monkeypatch.setenv("TRIEVO_API_MAX_RETRIES", "4")

    client = client_from_config({}, TokenBudget(1000))

    assert client.timeout == 150
    assert client.max_retries == 4


@pytest.mark.parametrize("exhausted", [False, True])
def test_relay_524_retries_only_the_same_model_request(monkeypatch, exhausted):
    requests = []

    def urlopen(request, timeout):
        requests.append((request.data, timeout))
        if exhausted or len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 524, "timeout", {}, io.BytesIO(b"gateway timeout"))
        return _success_response()

    monkeypatch.setattr("trievo_bench.model.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("trievo_bench.model.time.sleep", lambda _: None)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="test-secret", model="test-model",
        budget=TokenBudget(50000), timeout=150, max_retries=4, thinking="disabled",
    )
    if exhausted:
        with pytest.raises(RuntimeError, match="HTTP 524"):
            client.complete([{"role": "user", "content": "judge the saved reply"}], max_tokens=12000)
    else:
        assert client.complete([{"role": "user", "content": "judge the saved reply"}], max_tokens=12000) == "ok"
    assert len(requests) == (5 if exhausted else 2)
    assert client.budget.calls == len(requests)
    assert all(item == requests[0] for item in requests)
    payload = json.loads(requests[0][0])
    assert payload["max_tokens"] == 12000
    assert payload["thinking"] == {"type": "disabled"}
