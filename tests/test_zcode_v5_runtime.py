import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from baseline.adapters.zcode.bridge import ZCodeBridge, parse_output
from baseline.adapters.zcode.install_runtime_hooks import install
from trievo_bench.task_terminal import SCHEMA


def test_declared_task_transport_and_interaction_budgets_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIEVO_API_KEY", "mock-private")
    monkeypatch.setattr("trievo_bench.task_terminal.socket_from_metadata", lambda _: "/public/socket")
    observations = []
    bridge = ZCodeBridge()
    bridge.initialize({"protocol_version": "1.0", "baseline_id": "zcode", "options": {"model": "mock"},
                       "evolution": {"policy": "disabled"}})
    bridge.start_flow({"workspace": str(tmp_path / "w"), "state_dir": str(tmp_path / "s")})
    limits = {"max_model_calls": 2, "max_tool_calls": 1, "episode_timeout_seconds": 90,
              "max_output_tokens": 100, "terminal_timeout_seconds": 89}
    assert bridge.start_turn({"turn_id": "t1", "metadata": {"agent_limits": limits}})["task_terminal_transport"] == SCHEMA

    def native(command, **kwargs):
        env = kwargs["env"]
        assert env["SHELL"].endswith("/task-bin/bash")
        path = Path(env["TRIEVO_NATIVE_BUDGET"])
        budget = json.loads(path.read_text())
        assert budget["model_calls"] == budget["tool_calls"] == 0
        assert "mock-private" not in path.read_text()
        budget.update(model_calls=2, tool_calls=1, limit_reasons=["max_tool_calls"])
        path.write_text(json.dumps(budget))
        observations.append(budget)
        result = {"type": "result", "sessionId": "one", "response": "saved original reply",
                  "projection": {"status": "failed"},
                  "usage": {"totalTokens": 15, "inputTokens": 10, "outputTokens": 5,
                            "modelRequestCount": 2}}
        return SimpleNamespace(returncode=1, stdout=json.dumps(result), stderr="budget stopped")

    bridge.run_command = native
    for index in [1, 2]:
        result = bridge.interact({"turn_id": "t1", "request": "fresh interaction", "remaining_token_budget": 1000})
        assert result["raw"]["limit_exhausted"] and result["usage"]["total_tokens"] == 15
        assert result["text"] == "saved original reply"
    assert len(observations) == 2


def test_limit_failure_cannot_fabricate_native_usage_or_terminal_result():
    with pytest.raises(ValueError, match="terminal result"):
        parse_output('{"type":"turn.failed","sessionId":"s"}', allow_limit_failure=True)
    result = {"type": "result", "sessionId": "s", "response": "", "usage": {}}
    with pytest.raises(ValueError, match="provider usage"):
        parse_output(json.dumps(result), allow_limit_failure=True)


def test_image_hook_installation_rejects_unknown_upstream_boundaries(tmp_path):
    core = tmp_path / "apps/zcode-cli/packages/core/src"
    (core / "runtime/methods").mkdir(parents=True)
    (core / "tool/executor").mkdir(parents=True)
    requests = tmp_path / "apps/zcode-cli/packages/adapters/src/model"
    requests.mkdir(parents=True)
    (requests / "request-admission.ts").write_text("  if (input.admission === undefined) return NO_ADMISSION;\n")
    cwd = requests.parent / "exec"
    cwd.mkdir()
    (cwd / "cwd-capture.ts").write_text("  const cwdCaptureDir = tmpdir();\n")
    model = core / "runtime/methods/runtime-model.ts"
    model.write_text("    generateText(request) {\n    streamText(request) {\n")
    tool = core / "tool/executor/call-runner.ts"
    tool.write_text("      const output = await executeWithTimeout(\n")
    install(tmp_path)
    assert '?.outputCap()' in model.read_text()
    assert '?.admit("model")' in (requests / "request-admission.ts").read_text()
    assert '?.admit("tool")' in tool.read_text()
    with pytest.raises(ValueError, match="anchor changed"):
        install(tmp_path)


def test_limit_result_accounts_each_completed_request_once():
    events = [
        {"type": "session.updated", "sessionId": "s", "payload": {"type": "model_request_started", "requestId": "r1", "attempt": 1}},
        {"type": "session.updated", "sessionId": "s", "payload": {"type": "model_request_completed", "requestId": "r1", "attempt": 1,
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}},
        {"type": "session.updated", "sessionId": "s", "payload": {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}},
        {"type": "turn.failed", "sessionId": "s", "payload": {"error": "budget"}},
    ]
    reply = parse_output("\n".join(json.dumps(v) for v in events), allow_limit_failure=True)
    assert reply["usage"]["api_calls"] == 1 and reply["usage"]["total_tokens"] == 15
    with pytest.raises(ValueError, match="terminal result"):
        parse_output("\n".join(json.dumps(v) for v in events if v['payload'].get('type') != 'model_request_completed'), allow_limit_failure=True)
