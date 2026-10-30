from __future__ import annotations

import errno
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baseline.adapters.prime_agent.bridge import PrimeAgentBridge, PrimeRpcProcess
from trievo_bench.baselines.hermes_bridge import HermesBridge
from trievo_bench.l2_agent import L2AgentStreamRunner


class FakeRpc:
    def __init__(self):
        self.prompts = []

    def request(self, kind, **params):
        if kind == "get_state":
            return {"isStreaming": False}, []
        if kind == "prompt":
            self.prompts.append(params["message"])
            return {}, [
                {"type": "message_end", "message": {"role": "assistant"}},
                {"type": "tool_execution_start", "toolName": "ipython"},
                {"type": "tool_execution_end", "toolName": "ipython"},
            ]
        if kind == "get_session_stats":
            n = len(self.prompts)
            return {"assistantMessages": n, "tokens": {"input": n * 10, "output": n * 2}}, []
        if kind == "get_last_assistant_text":
            return {"text": "original reply"}, []
        raise AssertionError(kind)

    def close(self):
        pass


def prime_bridge(tmp_path):
    rpc = FakeRpc()
    bridge = PrimeAgentBridge(rpc_factory=lambda command, env: rpc)
    bridge.initialize({"protocol_version": "1.0", "options": {"l2_per_interaction_limits": True}})
    bridge.start_flow({"workspace": str(tmp_path / "workspace"), "state_dir": str(tmp_path / "state")})
    bridge.start_turn({"turn_id": "t1", "metadata": {"agent_limits": {
        "episode_timeout_seconds": 60, "max_model_calls": 2, "max_tool_calls": 1,
    }}})
    return bridge, rpc


def test_prime_interactions_each_receive_declared_tool_budget_without_extending_turn(tmp_path):
    bridge, rpc = prime_bridge(tmp_path)
    deadline = bridge._turn_deadline
    first = bridge.interact({"turn_id": "t1", "request": "first"})
    second = bridge.interact({"turn_id": "t1", "request": "second"})
    assert rpc.prompts == ["first", "second"]
    assert first["raw"]["tool_calls"] == second["raw"]["tool_calls"] == 1
    assert first["usage"]["total_tokens"] == second["usage"]["total_tokens"] == 12
    assert second["raw"]["limit_scope"] == "interaction"
    assert bridge._turn_deadline == deadline


def test_prime_expired_turn_returns_limit_without_a_new_prompt(tmp_path):
    bridge, rpc = prime_bridge(tmp_path)
    bridge._turn_deadline = time.monotonic() - 1
    reply = bridge.interact({"turn_id": "t1", "request": "never send"})
    assert rpc.prompts == []
    assert reply["raw"]["limit_exhausted"]
    assert reply["raw"]["limit_reasons"] == ["episode_timeout"]
    assert reply["usage"] == {}


def test_probe_backpressure_does_not_resend_or_lose_rpc_response():
    code = '''import json,sys
for line in sys.stdin:
    p=json.loads(line)
    print(json.dumps({"id":p["id"],"type":"response","success":True,"data":{"original":True}}),flush=True)
'''
    rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))

    class Backpressure:
        def write(self, value):
            raise BlockingIOError(errno.EAGAIN, "full diagnostic pipe")

        def flush(self):
            pass

    try:
        with patch.dict(os.environ, {"TRIEVO_PRIME_PROBE": "1"}), patch("sys.stderr", Backpressure()):
            result, _ = rpc.request("get_state", deadline=time.monotonic() + 1)
        assert result == {"original": True}
        assert rpc._request_id == 1
    finally:
        rpc.close()


def test_verifier_uses_owned_read_only_copy_and_preserves_original(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "private.xml"
    artifact.write_text("same bytes")
    artifact.chmod(0o600)
    runner = object.__new__(L2AgentStreamRunner)
    runner.options = SimpleNamespace(baseline_overrides={"options": {"l2_verifier_input_copy": True}})
    closed = []
    mirror = SimpleNamespace(close=lambda: closed.append(True))

    def create(path, **kwargs):
        assert Path(path) != workspace
        assert (Path(path) / "private.xml").read_text() == "same bytes"
        assert kwargs["writable_uid"] == 0 and kwargs["writable_gid"] == 0
        return mirror

    def verify(**kwargs):
        assert kwargs["verifier_input_volume"] is mirror
        return {"category": "pass", "score": 1, "details": {}}

    runner._verify_taskstream_in_volume = verify
    with patch("trievo_bench.l2_agent.DockerVolumeMirror.create", side_effect=create):
        result = runner._verify_taskstream(
            task={"evaluation": {"native_verifier": {"input_bindings": [{"source": "workspace"}]}}},
            turn_dir=tmp_path, workspace=workspace, reply=None,
        )
    assert closed == [True]
    assert result["verification_workspace"]["read_only"] is True
    assert artifact.read_text() == "same bytes"
    assert artifact.stat().st_mode & 0o777 == 0o600


def test_verifier_copy_is_removed_after_verification_error(tmp_path):
    (tmp_path / "workspace").mkdir()
    runner = object.__new__(L2AgentStreamRunner)
    runner.options = SimpleNamespace(baseline_overrides={"options": {"l2_verifier_input_copy": True}})
    closed = []
    runner._verify_taskstream_in_volume = lambda **kwargs: (_ for _ in ()).throw(ValueError("verification failed"))
    with patch("trievo_bench.l2_agent.DockerVolumeMirror.create", return_value=SimpleNamespace(close=lambda: closed.append(True))):
        with pytest.raises(ValueError, match="verification failed"):
            runner._verify_taskstream(
                task={"evaluation": {"native_verifier": {"input_bindings": [{"source": "workspace"}]}}},
                turn_dir=tmp_path, workspace=tmp_path / "workspace", reply=None,
            )
    assert closed == [True]


def test_inline_verifier_does_not_copy_live_session_socket_paths(tmp_path):
    runner = object.__new__(L2AgentStreamRunner)
    runner.options = SimpleNamespace(baseline_overrides={"options": {"l2_verifier_input_copy": True}})
    runner._verify_taskstream_in_volume = lambda **kwargs: {"category": "pass", "score": 1}
    with patch("trievo_bench.l2_agent.DockerVolumeMirror.create") as create:
        result = runner._verify_taskstream(
            task={"evaluation": {"native_verifier": {"input_bindings": [{"source": "interaction_record"}]}}},
            turn_dir=tmp_path, workspace=tmp_path, reply=None,
        )
    create.assert_not_called()
    assert result["category"] == "pass"


def test_hermes_bound_covers_usage_flush_before_review_and_never_releases_live_writer():
    bridge = HermesBridge()
    bridge._bounded_evolution_barrier = True
    bridge._evolution_teardown_grace = 0.02
    bridge._turn_limits = {"evolution_timeout_seconds": 0.03}
    bridge._turn_id = "t1"
    bridge._state = SimpleNamespace(agent=SimpleNamespace())
    release = threading.Event()

    def stuck(params):
        bridge._evolution_phase = "usage_before"
        release.wait(1)
        return {"completed": True}

    bridge._evolve_once = stuck
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="usage_before"):
            bridge.evolve({"turn_id": "t1", "update_enabled": True})
        assert time.monotonic() - start < 0.5
        assert not bridge._turn_evolved
    finally:
        release.set()


def test_hermes_bound_preserves_exact_success_result():
    bridge = HermesBridge()
    bridge._bounded_evolution_barrier = True
    bridge._turn_limits = {"evolution_timeout_seconds": 1}
    bridge._turn_id = "t1"
    bridge._state = SimpleNamespace(agent=SimpleNamespace())
    expected = {"completed": True, "usage": {"total_tokens": 12}, "events": []}
    bridge._evolve_once = lambda params: expected
    assert bridge.evolve({"turn_id": "t1", "update_enabled": True}) == expected
