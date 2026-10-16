"""Hermes foreground routing must not execute native tasks in its own image."""
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from trievo_bench.baselines.hermes_bridge import (
    HermesBridge, _native_file_handler, _native_terminal, _native_workspace_path,
)


def runtime():
    return {"environment_version": "l2_environment/1.0", "container_name": "trievo-l2-task-" + "a" * 12,
            "cwd": "/root", "task_directory": "/workspace/tasks/turn1",
            "path_mappings": [{"native_path": "/root/data", "workspace_relative_path": "data"}]}


def test_terminal_requests_host_execution_without_docker_access():
    with patch("trievo_bench.baselines.hermes_bridge._NATIVE_TOOL_REQUEST",
               return_value={"output": "3.12", "exit_code": 0}) as request:
        result = json.loads(_native_terminal("python3 --version", runtime=runtime(), timeout=42))
    assert result == {"output": "3.12", "exit_code": 0}
    assert request.call_args.args == ({"command": "python3 --version", "timeout_seconds": 42},)
    assert "container_name" not in request.call_args.args[0]


def test_missing_transport_cannot_silently_run_in_baseline_image():
    with patch("trievo_bench.baselines.hermes_bridge._NATIVE_TOOL_REQUEST", None):
        with pytest.raises(RuntimeError, match="refusing baseline-image"):
            _native_terminal("echo learned", runtime=runtime())


def test_native_file_aliases_include_relative_paths_and_reject_escape():
    assert _native_workspace_path("data/a.txt", runtime()) == "/workspace/tasks/turn1/data/a.txt"
    assert _native_workspace_path("/root/data/a.txt", runtime()) == "/workspace/tasks/turn1/data/a.txt"
    assert _native_workspace_path("/workspace/tasks/turn0/result", runtime()) == "/workspace/tasks/turn0/result"
    with pytest.raises(ValueError, match="outside declared"):
        _native_workspace_path("data/../secret", runtime())
    seen = []
    def write_file(path, content, task_id="default"):
        seen.append((path, content, task_id))
        return "ok"
    assert _native_file_handler(write_file, runtime())("data/a", "content", task_id="session") == "ok"
    assert seen == [("/workspace/tasks/turn1/data/a", "content", "session")]


def test_native_foreground_routes_and_restores_handlers_even_on_failure():
    terminal = ModuleType("tools.terminal_tool")
    terminal._get_approval_callback = lambda: None
    terminal.set_approval_callback = lambda value: None
    terminal.terminal_tool = lambda *args, **kwargs: "LEGACY"
    memory = ModuleType("tools.memory_tool")
    memory.memory_tool = lambda *args, **kwargs: "memory"
    files = ModuleType("tools.file_tools")
    def file_handler(path=".", **kwargs):
        return path
    for name in ("read_file_tool", "write_file_tool", "patch_tool", "search_tool"):
        setattr(files, name, file_handler)
    tools = ModuleType("tools")
    tools.terminal_tool, tools.memory_tool, tools.file_tools = terminal, memory, files
    plugins = ModuleType("hermes_cli.plugins")
    plugins.set_thread_tool_whitelist = lambda *args, **kwargs: None
    plugins.clear_thread_tool_whitelist = lambda: None
    modules = {"tools": tools, "tools.terminal_tool": terminal, "tools.memory_tool": memory,
               "tools.file_tools": files, "hermes_cli.plugins": plugins}
    agent = SimpleNamespace(valid_tool_names={"terminal", "read_file", "execute_code", "delegate_task"})
    bridge = HermesBridge()
    bridge._task_runtime = runtime()
    with patch.dict(sys.modules, modules):
        with pytest.raises(RuntimeError, match="stop"):
            with bridge._foreground_policy(agent, "session", "/workspace", {}):
                assert agent.valid_tool_names == {"terminal", "read_file"}
                assert files.read_file_tool("data/a") == "/workspace/tasks/turn1/data/a"
                with patch("trievo_bench.baselines.hermes_bridge._native_terminal", return_value='{"exit_code": 0}') as routed:
                    assert json.loads(terminal.terminal_tool("echo x"))["exit_code"] == 0
                    assert routed.call_args.kwargs["runtime"] == runtime()
                raise RuntimeError("stop")
    assert files.read_file_tool is file_handler
    assert terminal.terminal_tool() == "LEGACY"
    assert agent.valid_tool_names == {"terminal", "read_file", "execute_code", "delegate_task"}


def test_main_reverse_rpc_uses_protocol_stdout_and_correlates_response():
    import io
    from trievo_bench.baselines import hermes_bridge
    output = io.StringIO()
    class Input:
        def __iter__(self):
            return iter([json.dumps({"id": 1, "method": "end_flow", "params": {}})])
        def readline(self):
            event = json.loads(output.getvalue().splitlines()[-1])
            assert event["event"] == "native_tool_request"
            assert event["params"] == {"command": "echo learned", "timeout_seconds": 300}
            return json.dumps({"tool_response_id": event["tool_request_id"],
                               "result": {"output": "learned", "exit_code": 0}}) + "\n"
    class Bridge:
        def dispatch(self, method, params):
            return json.loads(_native_terminal("echo learned", runtime=runtime()))
    with patch.object(hermes_bridge, "HermesBridge", Bridge), \
            patch.object(hermes_bridge, "_NATIVE_TOOL_REQUEST", None), \
            patch.object(sys, "stdin", Input()), patch.object(sys, "stdout", output):
        hermes_bridge.main()
    assert json.loads(output.getvalue().splitlines()[-1]) == {
        "id": 1, "result": {"output": "learned", "exit_code": 0}}
