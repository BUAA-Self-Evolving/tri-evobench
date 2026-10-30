import json
import subprocess
from types import SimpleNamespace

import pytest

from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge
from tests.test_native_baselines import FakeHarness
from trievo_bench import verifier
from trievo_bench.sandbox import DockerSandbox


@pytest.mark.parametrize("failure", [False, True])
def test_verifier_reads_live_volume_and_cleans_checker(tmp_path, monkeypatch, failure):
    sandbox = object.__new__(DockerSandbox)
    sandbox.workspace_mount = lambda: "agent-live-volume"
    sandbox.export_workspace = lambda: pytest.fail("must not export to an invisible host path")
    closed = []
    mirror = SimpleNamespace(mount=lambda target, **kw: "checker-volume:/verifier:ro",
                             close=lambda: closed.append(True))
    monkeypatch.setattr(verifier.DockerVolumeMirror, "create", lambda *a, **kw: mirror)

    def run(command, **kwargs):
        assert "agent-live-volume:/workspace:ro" in command
        assert "checker-volume:/verifier:ro" in command
        if failure:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0, "PASSED", "")

    monkeypatch.setattr(verifier.subprocess, "run", run)
    result = verifier.ExecVerifier().evaluate({"path": str(tmp_path / "check.py")}, "", sandbox)
    assert result.passed is not failure
    assert closed == [True]


@pytest.mark.parametrize("rpc_seconds,expected", [(660, 660), (900, 900)])
def test_reference_profile_keeps_session_and_matches_effective_deadline(tmp_path, rpc_seconds, expected):
    bridge = DeepSeekHarnessBridge(FakeHarness)
    initialized = bridge.initialize({"protocol_version": "1.0", "baseline_id": "deepseek-harness",
        "options": {"execution_profile": "opencode-full-stream-20260928", "model": "fake",
                    "request_timeout_seconds": 900, "max_output_tokens": 4096}})
    assert initialized["session_policy"] == "one_per_flow"
    flow = {"flow_id": "f", "workspace": str(tmp_path / "workspace"), "state_dir": str(tmp_path / "state")}
    bridge.start_flow(flow)
    bridge.start_turn({"turn_id": "t1", "metadata": {"interaction_timeout_seconds": rpc_seconds,
                      "agent_limits": {"episode_timeout_seconds": 600}}})
    assert bridge.turn_timeout_seconds == expected
    first = bridge.interact({"turn_id": "t1", "request": "task one"})
    native = bridge.harness
    bridge.evolve({"turn_id": "t1", "feedback": [{"private": "never send to model"}]})
    assert not native.closed
    assert not bridge.start_turn({"turn_id": "t2"})["fresh_session"]
    second = bridge.interact({"turn_id": "t2", "request": "task two"})
    assert first["raw"]["session_id"] == second["raw"]["session_id"]
    assert bridge.harness is native
    assert "never send" not in json.dumps(native.prompts)
    bridge.end_flow()
    assert native.closed
    # Retained identity and transcript are included in exact state checkpoints.
    assert (tmp_path / "state/session.json").exists()
    assert bridge.start_flow(flow)["resumed_session"]
    assert bridge.session_id == first["raw"]["session_id"]
    bridge.end_flow()
