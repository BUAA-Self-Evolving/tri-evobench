from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from baseline.adapters.codex.bridge import CodexBridge, CodexError, _parse_output, _usage_delta
from trievo_bench.baselines.base import FlowContext, InteractionContext, TurnContext
from trievo_bench.baselines.manifest import find_manifest, load_manifest
from trievo_bench.baselines.process import BaselineProtocolError, ProcessBaselineRuntime
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.model import TokenBudget
from trievo_bench.runner import Runner
from trievo_bench.spec import BenchmarkSpec


REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "tests" / "fixtures" / "fake_codex_cli.py"
BRIDGE = REPO / "baseline" / "adapters" / "codex" / "bridge.py"


def make_bridge(root: Path, *, policy: str = "turn_boundary", run_command=None) -> CodexBridge:
    bridge = CodexBridge(run_command=run_command)
    bridge.initialize({
        "protocol_version": "1.0", "baseline_id": "codex",
        "options": {"executable": [sys.executable, str(CLI)], "model": "fake-model"},
        "evolution": {"policy": policy},
    })
    bridge.start_flow({"flow_id": "f", "workspace": str(root / "workspace"), "state_dir": str(root / "state")})
    bridge.start_turn({"turn_id": "t1"})
    return bridge


def local_manifest():
    return load_manifest(find_manifest("codex", REPO)).with_overrides({
        "adapter": {"command": [sys.executable, str(BRIDGE)], "cleanup_command": None},
        "workspace": {"mode": "host_path", "adapter_path": "{workspace}"},
        "state": {"adapter_path": "{state_dir}"},
        "options": {"executable": [sys.executable, str(CLI)], "model": "fake-model"},
        "evolution": {"policy": "turn_boundary"},
    })


def test_manifest_and_spec_select_codex():
    manifest = load_manifest(find_manifest("codex", REPO))
    assert manifest.baseline_id == "codex"
    assert manifest.source["revision"] == "cc7ba33601286a751477832b01de7b6f8de0dd9c"
    assert manifest.adapter["command"][0] == "docker"
    assert "--init" in manifest.adapter["command"]
    assert manifest.state["enforce_turn_freeze"]
    assert manifest.evolution["policy"] == "disabled"
    spec = BenchmarkSpec.from_dict({
        "schema_version": "0.1",
        "baseline": {"type": "codex", "options": {"reasoning_effort": "high"}},
        "flows": [{"id": "f", "turns": [{"id": "t", "request": "x", "verifier": {"type": "always"}}]}],
    })
    selected, explicit, overrides = resolve_baseline_selection(
        spec, baseline_override=None, agent_override=None, config_override=None, manifest_override=None,
    )
    assert selected == "codex"
    assert explicit is None
    assert overrides["options"]["reasoning_effort"] == "high"


def test_session_is_resumed_only_within_turn_and_usage_is_incremental(tmp_path):
    bridge = make_bridge(tmp_path)
    first = bridge.interact({"turn_id": "t1", "request": "solve"})
    session = bridge.session_id
    second = bridge.interact({"turn_id": "t1", "request": "retry"})
    assert bridge.session_id == session
    assert first["usage"] == second["usage"]
    assert second["usage"]["total_tokens"] == 12
    assert second["usage"]["cached_read_tokens"] == 3
    assert second["tool_calls"][0]["tool"] == "command_execution"
    assert second["actions_are_executed"] and second["tool_calls_are_executed"]
    with pytest.raises(RuntimeError, match="barrier"):
        bridge.start_turn({"turn_id": "t2"})
    evolution = bridge.evolve({"turn_id": "t1", "trajectory": [], "feedback": []})
    assert evolution["usage"]["total_tokens"] == 12
    with pytest.raises(RuntimeError, match="barrier"):
        bridge.evolve({"turn_id": "t1"})
    bridge.start_turn({"turn_id": "t2"})
    assert bridge.session_id is None
    bridge.interact({"turn_id": "t2", "request": "next task"})
    assert bridge.session_id != session
    assert (tmp_path / "state/learning/memory.md").exists()


@pytest.mark.parametrize("policy,enabled", [("disabled", True), ("turn_boundary", False)])
def test_disabled_or_frozen_evolution_never_calls_model(tmp_path, policy, enabled):
    bridge = make_bridge(tmp_path, policy=policy, run_command=lambda *_args, **_kwargs: pytest.fail("unexpected call"))
    assert bridge.evolve({"turn_id": "t1", "update_enabled": enabled})["completed"]
    assert not (tmp_path / "state/learning/memory.md").exists()


def test_evolution_uses_only_public_context_and_keeps_solver_session(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.run(command, **kwargs)

    bridge = make_bridge(tmp_path, run_command=run)
    bridge.interact({"turn_id": "t1", "request": "solve"})
    session = bridge.session_id
    bridge.evolve({
        "turn_id": "t1", "trajectory": [{"request": "public", "verifier_details": "secret"}],
        "feedback": [{"feedback": "retry", "oracle_update": "secret"}], "metadata": "secret",
    })
    command, kwargs = calls[-1]
    assert "secret" not in kwargs["input"]
    assert "public" in kwargs["input"]
    assert "--ephemeral" in command and "read-only" in command
    assert "resume" not in command
    assert bridge.session_id == session
    config = tomllib.loads((Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text())
    assert not config["features"]["shell_tool"]
    other = make_bridge(tmp_path / "other")
    assert other._memory() == ""


def test_provider_options_use_isolated_config_and_env_only_credentials(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.options.update({"base_url": "https://example.test/v1", "reasoning_effort": "high"})
    with patch.dict(os.environ, {"TRIEVO_API_KEY": "test-secret", "CODEX_HOME": "unrelated-home"}):
        config = tomllib.loads(bridge._config(evolution=False))
        env = bridge._environment(tmp_path / "isolated")
    assert env["CODEX_HOME"] == str(tmp_path / "isolated")
    assert env["TRIEVO_CODEX_API_KEY"] == "test-secret"
    assert config["model_providers"]["trievo"]["wire_api"] == "responses"
    assert config["model_reasoning_effort"] == "high"
    assert "test-secret" not in json.dumps(config)
    assert not config["memories"]["generate_memories"]
    assert not config["features"]["multi_agent"]


@pytest.mark.parametrize("output", ["bad JSON", "[]", '{}',
    '{"type":"thread.started","thread_id":"s"}',
    '{"type":"turn.completed","usage":{"input_tokens":-1}}',
    '{"type":"turn.completed","usage":{"output_tokens":"bad"}}',
    '{"type":"item.completed","item":null}',
])
def test_malformed_output_is_rejected(output):
    with pytest.raises(CodexError):
        _parse_output(output)


def test_provider_failure_and_timeout_are_not_model_replies(tmp_path):
    parsed = _parse_output('{"type":"turn.failed","error":{"message":"provider failed"}}')
    assert parsed["error"] == "provider failed"
    bridge = make_bridge(tmp_path, run_command=lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 1, '{"type":"turn.failed","error":{"message":"provider failed"}}', "",
    ))
    with pytest.raises(CodexError, match="provider failed"):
        bridge.interact({"turn_id": "t1", "request": "solve"})
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("codex", 1)
    bridge._run_command = timeout
    with pytest.raises(CodexError, match="do not replay"):
        bridge.interact({"turn_id": "t1", "request": "solve"})


def test_startup_error_retains_diagnostic_and_redacts_credentials(tmp_path):
    bridge = make_bridge(tmp_path, run_command=lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 2, "", "invalid configuration for test-secret",
    ))
    with patch.dict(os.environ, {"TRIEVO_API_KEY": "test-secret"}):
        with pytest.raises(CodexError, match="invalid configuration") as error:
            bridge.interact({"turn_id": "t1", "request": "solve"})
    assert "test-secret" not in str(error.value)


def test_failed_memory_update_retains_state_and_usage(tmp_path):
    bridge = make_bridge(tmp_path)
    target = tmp_path / "state/learning/memory.md"
    target.write_text("existing")
    def invalid(command, **kwargs):
        result = subprocess.run(command, **kwargs)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        for row in rows:
            if row.get("type") == "item.completed":
                row["item"]["text"] = "invalid memory JSON"
        result.stdout = "\n".join(json.dumps(row) for row in rows)
        return result
    bridge._run_command = invalid
    result = bridge.evolve({"turn_id": "t1", "trajectory": [], "feedback": []})
    assert not result["completed"]
    assert result["usage"]["total_tokens"] == 12
    assert target.read_text() == "existing"


def test_checkpoint_resume_exports_only_learning_state_and_workspace(tmp_path):
    manifest = local_manifest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest.raw))
    spec = BenchmarkSpec.from_dict({
        "schema_version": "0.1", "run": {"max_total_tokens": 1000},
        "sandbox": {"backend": "local"},
        "flows": [{"id": "f", "turns": [
            {"id": turn, "request": "solve", "verifier": {"type": "response_contains", "value": "done"}}
            for turn in ("t1", "t2")
        ]}],
    })
    first = Runner(spec, output_dir=tmp_path / "first", baseline="codex", baseline_manifest=str(path)).run(spec.flows[0])
    checkpoint = next((tmp_path / "first/_checkpoints").glob("*/t0001"))
    with zipfile.ZipFile(checkpoint / "baseline_state.zip") as bundle:
        assert "learning/memory.md" in bundle.namelist()
        assert not any("sessions" in name or "config.toml" in name for name in bundle.namelist())
    with zipfile.ZipFile(checkpoint / "workspace.zip") as bundle:
        assert any(name.endswith("codex-result.txt") for name in bundle.namelist())
    resumed = Runner(
        spec, output_dir=tmp_path / "resumed", baseline="codex", baseline_manifest=str(path), resume_checkpoint=checkpoint,
    ).run(spec.flows[0])
    assert resumed["turns"][0] == first["turns"][0]
    assert resumed["token_budget"]["used"] == first["token_budget"]["used"]


def test_runtime_rejects_learning_writes_outside_barrier(tmp_path):
    runtime = ProcessBaselineRuntime(local_manifest(), budget=TokenBudget(1000), repo_root=REPO)
    output = tmp_path / "output"
    output.mkdir()
    runtime.start_flow(FlowContext("f", tmp_path / "workspace", output))
    try:
        runtime.start_turn(TurnContext("t1", "solve", 1))
        (runtime._state_dir / "learning/memory.md").write_text("illegal")
        with pytest.raises(RuntimeError, match="persistent state"):
            runtime.interact(InteractionContext("t1", 1, "solve", [], {}, 1000))
        assert not (runtime._state_dir / "learning/memory.md").exists()
    finally:
        runtime.end_flow()


def test_revision_mismatch_refuses_evaluation(tmp_path):
    manifest = local_manifest().with_overrides({"source": {"revision": "0" * 40}})
    runtime = ProcessBaselineRuntime(manifest, budget=TokenBudget(1000), repo_root=REPO)
    with pytest.raises(BaselineProtocolError, match="revision mismatch"):
        runtime.start_flow(FlowContext("f", tmp_path / "workspace", tmp_path / "output"))


def test_cached_and_reasoning_tokens_are_not_counted_twice():
    usage = _usage_delta(
        {"input_tokens": 100, "output_tokens": 20},
        {"input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 5, "reasoning_output_tokens": 3},
    )
    assert usage["total_tokens"] == 30
