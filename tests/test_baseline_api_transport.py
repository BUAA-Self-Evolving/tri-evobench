import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch, name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _sdk(monkeypatch):
    clients = []

    class APIConnectionError(RuntimeError):
        pass

    class BadRequestError(RuntimeError):
        status_code = 400

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
            clients.append(self)

        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise APIConnectionError("injected transport failure")

    class AsyncClient(Client):
        async def create(self, **kwargs):
            return super().create(**kwargs)

    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(
            OpenAI=Client,
            AsyncOpenAI=AsyncClient,
            APIConnectionError=APIConnectionError,
            BadRequestError=BadRequestError,
        ),
    )
    return clients


def _environment(monkeypatch, enabled):
    for key in ("TRIEVO_SLOW_PROVIDER", "TRIEVO_API_TIMEOUT_SECONDS", "TRIEVO_API_MAX_RETRIES"):
        monkeypatch.delenv(key, raising=False)
    if enabled:
        monkeypatch.setenv("TRIEVO_SLOW_PROVIDER", "1")
        monkeypatch.setenv("TRIEVO_API_TIMEOUT_SECONDS", "150")
        monkeypatch.setenv("TRIEVO_API_MAX_RETRIES", "4")


@pytest.mark.parametrize("enabled", [False, True])
def test_trace_sdk_transport_policy_has_only_one_retry_layer(monkeypatch, enabled):
    _environment(monkeypatch, enabled)
    clients = _sdk(monkeypatch)
    module = _load(monkeypatch, "transport_trace_models", "baseline/Trace2Skill/src/react_agent/models.py")
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(module.random, "uniform", lambda *_: 0.0)
    client = module.OpenAIClient(api_key="fake-test-key", base_url="https://example.invalid", timeout=120, use_cache=False)
    assert clients[0].kwargs["timeout"] == (150 if enabled else 120)
    assert clients[0].kwargs["max_retries"] == 0
    with pytest.raises(RuntimeError, match="injected"):
        client._send_request_with_retry([], {"max_tokens": 12000})
    assert len(clients[0].calls) == (5 if enabled else 4)

    async def no_sleep(*_args):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(client._send_request_with_retry_async([], {"max_tokens": 12000}))
    assert clients[1].kwargs == clients[0].kwargs
    assert len(clients[1].calls) == (5 if enabled else 4)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("role", ["target", "optimizer"])
def test_skillopt_api_timeout_is_separate_from_tool_timeout(monkeypatch, enabled, role):
    _environment(monkeypatch, enabled)
    clients = _sdk(monkeypatch)
    common = types.ModuleType("skillopt.model.common")
    common.TokenTracker = lambda: types.SimpleNamespace(record=lambda *_: None)
    common.compat_message_from_chat_message = lambda value: value
    common.default_model_for_backend = lambda *_: "test-model"
    common.usage_from_openai_usage = lambda *_: {"prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setitem(sys.modules, "skillopt.model.common", common)
    module = _load(monkeypatch, "transport_skillopt_backend", "baseline/SkillOpt/skillopt/model/openai_compatible_backend.py")
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(module.random, "uniform", lambda *_: 0.0)
    module._config_for(role).timeout_seconds = 300
    with pytest.raises(RuntimeError, match="injected"):
        module._chat_messages_impl([], 12000, 3, "test", role=role, timeout=90)
    assert clients[0].kwargs["timeout"] == (150 if enabled else 300)
    assert clients[0].kwargs["max_retries"] == 0
    assert len(clients[0].calls) == (5 if enabled else 3)
    assert all(call["timeout"] == (150 if enabled else 90) for call in clients[0].calls)


def test_trace_does_not_retry_permanent_http_error(monkeypatch):
    _environment(monkeypatch, True)
    clients = _sdk(monkeypatch)
    module = _load(monkeypatch, "transport_trace_models_permanent", "baseline/Trace2Skill/src/react_agent/models.py")
    client = module.OpenAIClient(api_key="fake-test-key", base_url="https://example.invalid", timeout=120, use_cache=False)

    class BadRequestError(RuntimeError):
        status_code = 400

    def bad_request(**kwargs):
        clients[0].calls.append(kwargs)
        raise BadRequestError("bad request")

    client._client.chat.completions.create = bad_request
    with pytest.raises(BadRequestError):
        client._send_request_with_retry([], {"max_tokens": 12000})
    assert len(clients[0].calls) == 1


def test_skillopt_does_not_retry_permanent_http_error(monkeypatch):
    _environment(monkeypatch, True)
    _sdk(monkeypatch)
    common = types.ModuleType("skillopt.model.common")
    common.TokenTracker = lambda: types.SimpleNamespace(record=lambda *_: None)
    common.compat_message_from_chat_message = lambda value: value
    common.default_model_for_backend = lambda *_: "test-model"
    common.usage_from_openai_usage = lambda *_: {"prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setitem(sys.modules, "skillopt.model.common", common)
    module = _load(monkeypatch, "transport_skillopt_backend_permanent", "baseline/SkillOpt/skillopt/model/openai_compatible_backend.py")

    class UnauthorizedError(RuntimeError):
        status_code = 401

    class PermanentRecorder:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise UnauthorizedError("unauthorized")

    recorder = PermanentRecorder()
    monkeypatch.setattr(module, "_get_client", lambda role: types.SimpleNamespace(chat=types.SimpleNamespace(completions=recorder)))
    with pytest.raises(RuntimeError, match="unauthorized"):
        module._chat_messages_impl([], 12000, 3, "test", role="target", timeout=90)
    assert len(recorder.calls) == 1
