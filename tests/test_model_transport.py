import http.client
import io
import json

import pytest

from trievo_bench.model import OpenAICompatibleClient, TokenBudget


def client(*, max_retries=2):
    return OpenAICompatibleClient(base_url="https://example.invalid/v1", api_key="test",
                                  model="test", budget=TokenBudget(10000), max_retries=max_retries)


def test_disconnect_retries_same_request_once(monkeypatch):
    calls = []
    def request(value, **kwargs):
        calls.append(value.data)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("closed")
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}],
                                      "usage": {"total_tokens": 7}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr("time.sleep", lambda _: None)
    model = client()
    assert model.complete([{"role": "user", "content": "judge"}]) == "ok"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert model.budget.used == 7  # Only provider-reported usage is knowable.


def test_repeated_disconnect_is_bounded_and_preserves_type(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        raise http.client.RemoteDisconnected("closed")
    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="RemoteDisconnected") as error:
        client(max_retries=1).complete([{"role": "user", "content": "judge"}])
    assert len(calls) == 2
    assert isinstance(error.value.__cause__, http.client.RemoteDisconnected)
