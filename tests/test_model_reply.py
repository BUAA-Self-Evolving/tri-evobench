from __future__ import annotations

import http.client
import io
import json
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
