from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge, parse_events
from baseline.adapters.zcode.bridge import ZCodeBridge, parse_output
from trievo_bench.baselines.base import FlowContext
from trievo_bench.baselines.manifest import find_manifest, load_manifest
from trievo_bench.baselines.process import BaselineProtocolError, ProcessBaselineRuntime
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.model import TokenBudget
from trievo_bench.runner import Runner
from trievo_bench.spec import BenchmarkSpec


REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/fake_native_agent.py"
FakeHarness = runpy.run_path(str(FIXTURE))["FakeHarness"]
BASELINES = ("deepseek-harness", "zcode")


@pytest.mark.parametrize("baseline", BASELINES)
def test_built_native_agent_with_local_mock_provider(baseline):
    binary = REPO / ("baseline/deepseek-harness/apps/cli/lib/bin.js" if baseline == "deepseek-harness"
                     else "baseline/ZCode/apps/zcode-cli/packages/cli/dist/zcode.cjs")
    if not binary.exists():
        pytest.skip("build the pinned native source before running transport smoke")
    if baseline == "deepseek-harness" and importlib.util.find_spec("deepseek_harness") is None:
        pytest.skip("install the pinned Python SDK before running transport smoke")
    completed = subprocess.run(
        [sys.executable, str(REPO / "tests/fixtures/native_transport_smoke.py"), baseline],
        cwd=REPO, capture_output=True, text=True, timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["same_session"]
    assert result["first_usage"]["total_tokens"] == result["second_usage"]["total_tokens"] == 15


def make_bridge(root: Path, baseline: str):
    bridge = DeepSeekHarnessBridge(FakeHarness) if baseline == "deepseek-harness" else ZCodeBridge()
    bridge.initialize({
        "protocol_version": "1.0", "baseline_id": baseline,
        "options": {"model": "fake", "executable": [sys.executable, str(FIXTURE)]},
        "evolution": {"policy": "disabled"},
    })
    bridge.start_flow({"flow_id": "f", "workspace": str(root / "workspace"), "state_dir": str(root / "state")})
    bridge.start_turn({"turn_id": "t1"})
    return bridge


def local_manifest(baseline: str):
    if baseline == "deepseek-harness":
        bootstrap = (
            "import runpy; from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge; "
            "from baseline.adapters.native_runtime import serve; "
            f"serve(DeepSeekHarnessBridge(runpy.run_path({str(FIXTURE)!r})['FakeHarness']))"
        )
        command = [sys.executable, "-c", bootstrap]
    else:
        command = [sys.executable, "-m", "baseline.adapters.zcode.bridge"]
    return load_manifest(find_manifest(baseline, REPO)).with_overrides({
        "adapter": {"command": command, "cleanup_command": None},
        "workspace": {"mode": "host_path", "adapter_path": "{workspace}"},
        "state": {"adapter_path": "{state_dir}"},
        "options": {"executable": [sys.executable, str(FIXTURE)], "model": "fake"},
    })


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("TRIEVO_API_KEY", "test-secret")


@pytest.mark.parametrize("baseline", BASELINES)
def test_manifest_spec_and_docker_transport(baseline):
    manifest = load_manifest(find_manifest(baseline, REPO))
    spec = BenchmarkSpec.from_dict({
        "schema_version": "0.1", "baseline": {"type": baseline, "options": {"model": "custom"}},
        "flows": [{"id": "f", "turns": [{"id": "t", "request": "x", "verifier": {"type": "always"}}]}],
    })
    selected, _, overrides = resolve_baseline_selection(
        spec, baseline_override=None, agent_override=None, config_override=None, manifest_override=None,
    )
    assert selected == baseline
    assert overrides["options"]["model"] == "custom"
    assert manifest.state["enforce_turn_freeze"]
    assert manifest.evolution["policy"] == "disabled"
    assert manifest.adapter["command"][0] == "docker"
    assert "--init" in manifest.adapter["command"]
    assert "TRIEVO_API_KEY" in manifest.environment["inherit"]
    assert manifest.state["paths"] == ["learning/**/*"]


@pytest.mark.parametrize("baseline", BASELINES)
def test_sessions_usage_barrier_and_flow_isolation(tmp_path, baseline):
    bridge = make_bridge(tmp_path, baseline)
    first = bridge.interact({"turn_id": "t1", "request": "solve"})
    session = bridge.session_id
    second = bridge.interact({"turn_id": "t1", "request": "retry"})
    assert bridge.session_id == session
    assert first["usage"] == second["usage"]
    assert first["usage"]["input_tokens"] == 15
    assert first["usage"]["total_tokens"] == 20
    assert first["usage"]["cached_read_tokens"] == 3
    assert len(first["tool_calls"]) == 1
    assert first["tool_calls"][0]["result"]
    assert first["tool_calls_are_executed"] and first["actions_are_executed"]
    with pytest.raises(RuntimeError, match="barrier"):
        bridge.start_turn({"turn_id": "t2"})
    (bridge.home / "old-session").write_text("transcript")
    harness = getattr(bridge, "harness", None)
    result = bridge.evolve({"turn_id": "t1", "update_enabled": True, "feedback": [{"private": "secret"}]})
    assert result["usage"] == {}
    assert list((bridge.state_dir / "learning").iterdir()) == []
    if harness is not None:
        assert harness.closed
    with pytest.raises(RuntimeError, match="barrier"):
        bridge.evolve({"turn_id": "t1"})
    bridge.start_turn({"turn_id": "t2"})
    assert not (bridge.home / "old-session").exists()
    assert bridge.session_id is None
    bridge.interact({"turn_id": "t2", "request": "next"})
    assert bridge.session_id != session
    other = make_bridge(tmp_path / "other", baseline)
    assert other.session_id is None
    bridge.end_flow()
    assert not (tmp_path / "state/sessions").exists()
    other.end_flow()


@pytest.mark.parametrize("baseline", BASELINES)
def test_unsupported_evolution_and_wrong_turn_are_rejected(tmp_path, baseline):
    bridge = make_bridge(tmp_path, baseline)
    with pytest.raises(ValueError, match="disabled"):
        bridge.initialize({"protocol_version": "1.0", "baseline_id": baseline, "evolution": {"policy": "turn_boundary"}})
    with pytest.raises(RuntimeError, match="inactive"):
        bridge.interact({"turn_id": "other", "request": "x"})
    with pytest.raises(RuntimeError, match="exhausted"):
        bridge.interact({"turn_id": "t1", "remaining_token_budget": 0})
    bridge.end_flow()


def test_deepseek_uses_public_sdk_and_isolated_home(tmp_path):
    bridge = make_bridge(tmp_path, "deepseek-harness")
    bridge.options.update({"base_url": "https://example.test", "reasoning_effort": "high", "max_output_tokens": 512})
    bridge.interact({"turn_id": "t1", "request": "task", "history": [{"role": "user", "content": "public"}]})
    config = bridge.harness.config
    assert config["profile"] == "sdk-minimal"
    assert config["api_key"] == "test-secret"
    assert config["max_tokens"] == 512
    assert config["cwd"] == str(bridge.workspace)
    assert config["dsh_home"].startswith(str(bridge.home))
    assert "public" in bridge.harness.prompts[0]
    assert "_launch_args" not in config
    bridge.end_flow()


def test_deepseek_failed_attempt_accounting_and_deduplication():
    attempt = {"type": "assistant/attempt", "seq": 1, "data": {"stream": [
        {"type": "chunk", "time": 0, "chunk": {"type": "usage", "usage": {
            "inputTokens": 4, "outputTokens": 2, "cacheReadTokens": 1,
        }}},
    ]}}
    parsed = parse_events([attempt, attempt])
    assert parsed["usage"]["total_tokens"] == 7
    assert parsed["usage"]["api_calls"] == 1
    with pytest.raises(ValueError, match="usage"):
        parse_events([])
    with pytest.raises(ValueError, match="non-negative"):
        parse_events([{"type": "assistant/message", "data": {"usage": {"inputTokens": -1}}}])


def test_deepseek_failure_is_not_success(tmp_path):
    bridge = make_bridge(tmp_path, "deepseek-harness")
    bridge.harness = SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(finish_reason="max-tokens"), close=lambda: None)
    with pytest.raises(RuntimeError, match="max-tokens"):
        bridge.interact({"turn_id": "t1", "request": "task"})
    bridge.end_flow()


def test_zcode_config_credentials_and_same_turn_resume(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, "zcode")
    observed = []
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", "unrelated")
    def run(command, **kwargs):
        personal = Path(kwargs["env"]["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"])
        observed.append((command, kwargs, json.loads(personal.read_text())))
        assert personal.stat().st_mode & 0o077 == 0
        return subprocess.run(command, **kwargs)
    bridge.run_command = run
    bridge.interact({"turn_id": "t1", "request": "task"})
    bridge.interact({"turn_id": "t1", "request": "/help"})
    command, kwargs, config = observed[1]
    assert "--resume" in command
    assert command[-1].startswith("User task:")
    assert kwargs["env"]["HOME"] == str(bridge.home)
    assert kwargs["env"]["ZCODE_DATA_BASE_DIR"] == str(bridge.home)
    assert "ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE" not in kwargs["env"]
    assert config["config"]["defaultModelSelection"]["modelId"] == "fake"
    assert config["config"]["providerConfigRules"]["providerRules"][0]["config"]["access"]["apiKey"] == "test-secret"
    assert not (bridge.home / "provider_config.json").exists()
    native_config = json.loads((bridge.home / ".zcode/cli/config.json").read_text())
    assert not native_config["memory"]["use"]
    assert not native_config["plugins"]["enabled"]
    bridge.end_flow()


def zcode_result(**changes):
    result = {"type": "result", "sessionId": "s", "response": "done",
              "usage": {"totalTokens": 10, "modelRequestCount": 1}, "projection": {"status": "idle"}}
    result.update(changes)
    return json.dumps(result)


@pytest.mark.parametrize("output", ["not json", "[]", "{}", "", zcode_result(usage=None),
    zcode_result(usage={"totalTokens": -1, "modelRequestCount": 1}), zcode_result(response=None),
    zcode_result() + "\n" + zcode_result(),
    '{"type":"turn.failed","sessionId":"s"}\n' + zcode_result(),
    '{"type":"session.created","sessionId":"other"}\n' + zcode_result(),
])
def test_zcode_malformed_protocol_is_rejected(output):
    with pytest.raises((ValueError, RuntimeError)):
        parse_output(output)


def test_zcode_error_redaction_timeout_and_credential_cleanup(tmp_path):
    bridge = make_bridge(tmp_path, "zcode")
    bridge.run_command = lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "error test-secret")
    with pytest.raises(RuntimeError, match="redacted") as error:
        bridge.interact({"turn_id": "t1", "request": "task"})
    assert "test-secret" not in str(error.value)
    assert not (bridge.home / "provider_config.json").exists()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("zcode", 1)
    bridge.run_command = timeout
    with pytest.raises(RuntimeError, match="do not replay"):
        bridge.interact({"turn_id": "t1", "request": "task"})
    assert not (bridge.home / "provider_config.json").exists()
    bridge.end_flow()


@pytest.mark.parametrize("baseline", BASELINES)
def test_runtime_checkpoint_resume_and_workspace_export(tmp_path, baseline):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(local_manifest(baseline).raw))
    spec = BenchmarkSpec.from_dict({
        "schema_version": "0.1", "run": {"max_total_tokens": 1000}, "sandbox": {"backend": "local"},
        "flows": [{"id": "f", "turns": [
            {"id": turn, "request": "solve", "verifier": {"type": "response_contains", "value": "done"}}
            for turn in ("t1", "t2")
        ]}],
    })
    first = Runner(spec, output_dir=tmp_path / "first", baseline=baseline, baseline_manifest=str(path)).run(spec.flows[0])
    checkpoint = next((tmp_path / "first/_checkpoints").glob("*/t0001"))
    with zipfile.ZipFile(checkpoint / "baseline_state.zip") as bundle:
        assert not any("sessions" in name or "provider_config" in name for name in bundle.namelist())
    with zipfile.ZipFile(checkpoint / "workspace.zip") as bundle:
        assert any(name.endswith("native-result.txt") for name in bundle.namelist())
    resumed = Runner(spec, output_dir=tmp_path / "resumed", baseline=baseline, baseline_manifest=str(path), resume_checkpoint=checkpoint).run(spec.flows[0])
    assert resumed["turns"][0] == first["turns"][0]
    assert resumed["token_budget"]["used"] == first["token_budget"]["used"]
    assert first["token_budget"]["used"] == 40


@pytest.mark.parametrize("baseline", BASELINES)
def test_revision_mismatch_refuses_evaluation(tmp_path, baseline):
    manifest = local_manifest(baseline).with_overrides({"source": {"revision": "0" * 40}})
    runtime = ProcessBaselineRuntime(manifest, budget=TokenBudget(1000), repo_root=REPO)
    with pytest.raises(BaselineProtocolError, match="revision mismatch"):
        runtime.start_flow(FlowContext("f", tmp_path / "workspace", tmp_path / "output"))
