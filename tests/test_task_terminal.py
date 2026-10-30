from contextlib import contextmanager
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace

import pytest

from trievo_bench.task_terminal import SCHEMA, TerminalService, _read_line, _send, run_command, serve, socket_from_metadata


@contextmanager
def terminal(*, timeout=3, calls=5):
    with tempfile.TemporaryDirectory(prefix="trievo-term-") as scratch:
        root = Path(scratch)
        service = TerminalService(root, root / "audit.json")
        public_path, private_path = root / "tool.sock", root / "private.sock"
        public = serve(public_path, service)
        private = serve(private_path, service, private=True)
        service.control({"op": "begin", "interaction": "turn1-i1", "max_commands": calls,
                         "episode_timeout_seconds": timeout, "terminal_timeout_seconds": timeout})
        try:
            yield service, root, str(public_path), str(private_path)
        finally:
            public.shutdown()
            private.shutdown()
            public.server_close()
            private.server_close()


def test_terminal_executes_workspace_command_and_preserves_nonzero_exit_and_streams():
    with terminal() as (service, root, public, _):
        result = run_command(public, "printf proof > artifact; printf output; printf detail >&2; exit 7", cwd=str(root))
        assert (root / "artifact").read_text() == "proof"
        assert result["returncode"] == 7
        assert result["stdout"] == "output" and result["stderr"] == "detail"
        assert result["limit_reason"] is None
        audit = json.loads((root / "audit.json").read_text())
        assert audit["completed"][0]["native_returncode"] == 7
        assert "command" not in json.dumps(audit)
        large = run_command(public, "python3 -c \"print('好' * 30000, end='')\"", cwd=str(root))
        assert large["returncode"] == 0 and large["stdout"] == "好" * 30000
        with pytest.raises(RuntimeError, match="inside the workspace"):
            run_command(public, "true", cwd="/")


def test_command_limit_is_per_interaction_and_agent_cannot_reset_it():
    with terminal(calls=1) as (service, root, public, private):
        assert run_command(public, "true", cwd=str(root))["returncode"] == 0
        denied = run_command(public, "touch forbidden", cwd=str(root))
        assert denied["returncode"] == 125 and denied["limit_reason"] == "max_commands"
        assert not (root / "forbidden").exists()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(public)
            _send(connection, {"op": "begin", "interaction": "malicious-reset"})
            assert "only run" in _read_line(connection)["error"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(private)
            _send(connection, {"op": "begin", "interaction": "turn1-i2", "max_commands": 1,
                               "episode_timeout_seconds": 3, "terminal_timeout_seconds": 3})
            assert _read_line(connection)["ready"]
        assert run_command(public, "true", cwd=str(root))["returncode"] == 0
        with pytest.raises(ValueError, match="new and nonempty"):
            service.control({"op": "begin", "interaction": "turn1-i2"})
        assert Path(private).stat().st_mode & 0o777 == 0o600


def test_timeout_and_client_cancel_kill_command_group_and_leave_no_late_write():
    with terminal(timeout=0.15) as (service, root, public, _):
        result = run_command(public, "sleep .5; touch late", cwd=str(root), timeout_seconds=3)
        assert result["returncode"] == 124 and result["limit_reason"] == "terminal_timeout"
        time.sleep(0.55)
        assert not (root / "late").exists()
    with terminal() as (service, root, public, _):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(public)
            _send(connection, {"op": "run", "command": "printf ready; sleep .5; touch after-cancel",
                               "cwd": str(root)})
            assert _read_line(connection)["type"] == "stdout"
        deadline = time.monotonic() + 2
        while service.active and time.monotonic() < deadline:
            time.sleep(.02)
        time.sleep(.55)
        assert not (root / "after-cancel").exists()
        assert not service.active
        assert service.completed[-1]["reason"] == "client_disconnected"


def test_declared_terminal_must_exist_and_old_adapter_is_rejected_before_interact():
    from trievo_bench.baselines.base import TurnContext
    from trievo_bench.baselines.process import BaselineProtocolError, ProcessBaselineRuntime
    metadata = {"task_sandbox": {"terminal_transport": SCHEMA, "terminal_socket": "/wrong/socket"}}
    with pytest.raises(ValueError, match="unsupported"):
        socket_from_metadata({"metadata": metadata})
    runtime = object.__new__(ProcessBaselineRuntime)
    runtime._transport = object()
    runtime._current_turn = None
    runtime._stable_state = None
    runtime._semantic_state = {}
    runtime._ensure_stable_state = lambda _: None
    methods = []
    def old_request(method, params):
        methods.append(method)
        return {}
    runtime._request = old_request
    with pytest.raises(BaselineProtocolError, match="did not acknowledge"):
        runtime.start_turn(TurnContext("turn1", "request", 1, metadata))
    assert methods == ["start_turn"]
    runtime._request = lambda *_args: {"task_terminal_transport": SCHEMA}
    runtime._capture_state = lambda **_kwargs: None
    runtime.manifest = SimpleNamespace(state={})
    runtime.start_turn(TurnContext("turn1", "request", 1, metadata))
    assert runtime._current_turn == "turn1"


def test_ending_interaction_reaps_native_command_before_next_interaction():
    with terminal() as (service, root, public, _):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(public)
            _send(connection, {"op": "run", "command": "printf ready; sleep .5; touch after-end", "cwd": str(root)})
            assert _read_line(connection)["type"] == "stdout"
            receipt = service.control({"op": "end"})
            assert receipt["active"] == 0
        time.sleep(.55)
        assert not (root / "after-end").exists()
        expired = run_command(public, "true", cwd=str(root))
        assert expired["limit_reason"] == "episode_timeout"


def test_new_terminal_mount_preserves_empty_volume_ownership_without_changing_legacy_mount():
    from trievo_bench.baselines.process import _rewrite_named_volume_mount
    command = ["docker", "run", "-v", "workspace-volume:/workspace", "image"]
    legacy = _rewrite_named_volume_mount(command, "workspace-volume", "/workspace")
    current = _rewrite_named_volume_mount(command, "workspace-volume", "/workspace", no_copy=True)
    assert "type=volume,src=workspace-volume,dst=/workspace" in legacy
    assert "type=volume,src=workspace-volume,dst=/workspace,volume-nocopy" in current
    assert command == ["docker", "run", "-v", "workspace-volume:/workspace", "image"]


def test_task_pty_preserves_shell_state_and_reaps_on_interaction_end():
    import base64
    with terminal(timeout=8) as (service, root, public, _):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(public)
            _send(client, {"op": "pty", "cwd": str(root), "timeout_seconds": 8,
                           "terminal_environment": {"PS1": "TASK> ", "TRIEVO_API_KEY": "must-not-cross"}})
            assert _read_line(client)["type"] == "ready"
            reader = client.makefile("rb")
            def until(marker):
                output = b""
                while marker not in output:
                    event = json.loads(reader.readline())
                    assert event["type"] == "stdout", event
                    output += base64.b64decode(event["data"])
                return output
            until(b"TASK> ")
            client.sendall(b"stty -echo; export PROOF=kept; mkdir nested; cd nested\n")
            until(b"TASK> ")
            client.sendall(b"printf 'state:%s:%s:%s\\n' \"$PROOF\" \"$PWD\" \"${TRIEVO_API_KEY-unset}\"\n")
            output = until(b"TASK> ")
            assert b"state:kept:" + str((root / "nested").resolve()).encode() + b":unset" in output
            receipt = service.control({"op": "end"})
            assert receipt["active"] == 0
            reader.close()


def test_long_task_terminal_budget_requires_v5_transport_and_matches_hermes():
    from dataclasses import asdict
    from trievo_bench.baselines.hermes_bridge import _parse_l2_agent_limits
    from trievo_bench.l2_agent import L2AgentWorkLimits, _work_limits
    limits = asdict(L2AgentWorkLimits(terminal_timeout_seconds=3599, episode_timeout_seconds=3600))
    with pytest.raises(RuntimeError, match='between 1 and 600'):
        _parse_l2_agent_limits({'agent_limits': limits})
    assert _parse_l2_agent_limits({'agent_limits': limits, 'task_sandbox': {
        'terminal_transport': SCHEMA}})['terminal_timeout_seconds'] == 3599
    with pytest.raises(Exception, match='between 1 and 600'):
        _work_limits({'options': {'l2_agent_limits': limits}})
    assert _work_limits({'options': {'l2_agent_limits': limits,
                                  'l2_task_terminal_transport': True}}).terminal_timeout_seconds == 3599
