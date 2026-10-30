"""Terminal transport into a credential-free, offline V5 task container.

The service runs inside the declared task image. Agents share only its Unix
socket through the workspace volume. The private control socket remains in
the task container and lets the harness begin each Interaction exactly once.
No command, credential or model endpoint is added to the workspace.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any, Callable

SCHEMA = "trievo_task_terminal/1.0"
PUBLIC_DIRECTORY = "/run/trievo-task-runtime"
PUBLIC_SOCKET = PUBLIC_DIRECTORY + "/terminal.sock"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def _send(connection: socket.socket, value: dict[str, Any]) -> None:
    connection.sendall((json.dumps(value, separators=(",", ":")) + "\n").encode())


def _read_line(connection: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while not data.endswith(b"\n"):
        # Do not consume bytes belonging to a subsequent interactive input frame.
        chunk = connection.recv(1)
        if not chunk:
            raise ConnectionError("task terminal disconnected")
        data.extend(chunk)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("task terminal request is too large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("task terminal request must be an object")
    return value


def _positive(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout must be positive and finite")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout must be positive and finite")
    return result


def _kill_group(process: subprocess.Popen) -> None:
    # Also reap background descendants after the foreground command exits.
    # Interactive bash creates foreground/background process groups in its session.
    if Path('/proc').exists():
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    if os.getsid(int(entry.name)) == process.pid:
                        os.kill(int(entry.name), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class TerminalService:
    def __init__(self, workspace: Path, audit_path: Path | None = None):
        self.workspace = workspace.resolve()
        self.audit_path = audit_path
        self.lock = threading.Lock()
        self.interaction: str | None = None
        self.deadline = 0.0
        self.command_timeout = 0.0
        self.max_calls = 0
        self.calls = 0
        self.active: set[subprocess.Popen] = set()
        self.completed: list[dict[str, Any]] = []

    def control(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") == "end":
            with self.lock:
                self.deadline = 0.0
                processes = list(self.active)
            for process in processes:
                _kill_group(process)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.lock:
                    idle = not self.active
                if idle:
                    return self.control({"op": "status"})
                time.sleep(.01)
            raise RuntimeError("task commands did not stop at the Interaction boundary")
        with self.lock:
            if request.get("op") == "status":
                return {"schema": SCHEMA, "interaction": self.interaction,
                        "calls": self.calls, "active": len(self.active), "completed": self.completed}
            if request.get("op") != "begin":
                raise ValueError("unsupported private task-terminal operation")
            identity = request.get("interaction")
            if not isinstance(identity, str) or not identity or identity == self.interaction:
                raise ValueError("Interaction identity must be new and nonempty")
            if self.active:
                raise ValueError("previous Interaction still has running commands")
            # Commands also include native terminal bootstrap/housekeeping.
            # Canonical public tool budgets remain the adapter's responsibility.
            count = request.get("max_commands")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10000:
                raise ValueError("invalid task-terminal call limit")
            timeout = _positive(request["episode_timeout_seconds"])
            command_timeout = _positive(request["terminal_timeout_seconds"])
            self.interaction = identity
            self.deadline = time.monotonic() + timeout
            self.command_timeout = min(command_timeout, timeout)
            self.max_calls, self.calls = count, 0
            self._save()
            return {"schema": SCHEMA, "interaction": identity, "ready": True}

    def _save(self) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.audit_path.with_suffix(".tmp")
        # Commands, stdin, output and process environment are intentionally absent.
        temp.write_text(json.dumps({"schema": SCHEMA, "interaction": self.interaction,
                                   "calls": self.calls, "completed": self.completed}, indent=2) + "\n")
        temp.replace(self.audit_path)

    def execute(self, connection: socket.socket, request: dict[str, Any]) -> None:
        interactive = request.get("op") == "pty"
        if request.get("op") not in {"run", "pty"}:
            raise ValueError("only run/pty are available on the agent socket")
        command = "interactive bash" if interactive else request.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("nonempty command is required")
        cwd = Path(str(request.get("cwd") or self.workspace)).resolve()
        if cwd != self.workspace and self.workspace not in cwd.parents:
            raise ValueError("task working directory must be inside the workspace")
        stdin = request.get("stdin", "")
        if not isinstance(stdin, str):
            raise ValueError("stdin must be text")
        requested_timeout = _positive(request.get("timeout_seconds", 600))
        with self.lock:
            remaining = self.deadline - time.monotonic()
            reason = ("interaction_not_started" if self.interaction is None else
                      "episode_timeout" if remaining <= 0 else
                      "max_commands" if self.calls >= self.max_calls else None)
            if reason:
                _send(connection, {"type": "done", "returncode": 124 if reason == "episode_timeout" else 125,
                                   "limit_reason": reason})
                return
            self.calls += 1
            identity, call_number = self.interaction, self.calls
            timeout = min(remaining, requested_timeout) if interactive else min(remaining, self.command_timeout, requested_timeout)
            self._save()
            started = time.monotonic()
            master = slave = None
            if interactive:
                import fcntl
                import termios
                master, slave = os.openpty()
                def establish_terminal():
                    os.setsid()
                    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                # Only terminal presentation fields cross this boundary. Never inherit
                # the Agent environment or credentials into the task container.
                presentation = request.get("terminal_environment", {})
                presentation = {key: value for key, value in presentation.items()
                                if key in {"PS1", "PROMPT_COMMAND", "TERM"} and isinstance(value, str)}
                process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-i"], cwd=cwd,
                    env={**os.environ, "HOME": str(self.workspace), **presentation},
                    stdin=slave, stdout=slave, stderr=slave, preexec_fn=establish_terminal)
                os.close(slave)
            else:
                process = subprocess.Popen(["/bin/bash", "-lc", command], cwd=cwd,
                                           env={**os.environ, "HOME": str(self.workspace)},
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
            self.active.add(process)
        reason = None
        result = None
        output_bytes = 0
        try:
            # A separate writer prevents a large stdin payload from blocking output.
            def write_input():
                try:
                    process.stdin.write(stdin.encode())
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            if interactive:
                _send(connection, {"type": "ready"})
            else:
                threading.Thread(target=write_input, daemon=True).start()
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_READ, "connection")
                if interactive:
                    selector.register(master, selectors.EVENT_READ, "stdout")
                else:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                streams = 1 if interactive else 2
                while streams:
                    if time.monotonic() - started >= timeout:
                        reason = "terminal_timeout"
                        break
                    for key, _ in selector.select(min(0.1, max(0.0, timeout - (time.monotonic() - started)))):
                        if key.data == "connection":
                            # No additional client messages are valid. EOF means cancel.
                            data = connection.recv(65536 if interactive else 1)
                            if not data:
                                reason = "client_disconnected"
                                break
                            if interactive:
                                os.write(master, data)
                                continue
                            raise ValueError("unexpected task-terminal control data")
                        try:
                            chunk = os.read(key.fd, 65536)
                        except OSError:
                            if not interactive:
                                raise
                            chunk = b""
                        if not chunk:
                            selector.unregister(key.fileobj)
                            streams -= 1
                            continue
                        output_bytes += len(chunk)
                        if output_bytes > MAX_OUTPUT_BYTES:
                            reason = "output_limit"
                            break
                        _send(connection, {"type": key.data, "data": base64.b64encode(chunk).decode()})
                    if reason:
                        break
            if reason is None:
                try:
                    process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    reason = "terminal_timeout"
            _kill_group(process)
            result = {"type": "done", "returncode": 124 if reason == "terminal_timeout" else
                      125 if reason else process.returncode, "limit_reason": reason}
        except (BrokenPipeError, ConnectionResetError):
            reason = "client_disconnected"
        finally:
            _kill_group(process)
            if master is not None:
                os.close(master)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    pipe.close()
            with self.lock:
                self.active.discard(process)
                self.completed.append({"interaction": identity, "call": call_number,
                                       "elapsed_seconds": round(time.monotonic() - started, 3),
                                       "native_returncode": process.returncode, "reason": reason})
                self._save()
        if result is not None:
            _send(connection, result)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(socket_path: Path, service: TerminalService, *, private: bool = False) -> _Server:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(30)
                request = _read_line(self.request)
                self.request.settimeout(5)
                if private:
                    _send(self.request, service.control(request))
                else:
                    service.execute(self.request, request)
            except Exception as error:
                try:
                    _send(self.request, {"type": "error", "error": f"{type(error).__name__}: {error}"})
                except OSError:
                    pass
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = _Server(str(socket_path), Handler)
    os.chmod(socket_path, 0o600 if private else 0o666)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_command(socket_path: str, command: str, *, cwd: str = "/workspace", timeout_seconds: float = 600,
                stdin: str = "", on_chunk: Callable[[str, bytes], None] | None = None) -> dict[str, Any]:
    output = {"stdout": bytearray(), "stderr": bytearray()}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(_positive(timeout_seconds) + 30)
        connection.connect(socket_path)
        _send(connection, {"op": "run", "command": command, "cwd": cwd,
                           "timeout_seconds": timeout_seconds, "stdin": stdin})
        with connection.makefile("rb") as reader:
            for line in reader:
                event = json.loads(line)
                if event.get("type") in {"stdout", "stderr"}:
                    chunk = base64.b64decode(event["data"])
                    output[event["type"]].extend(chunk)
                    if on_chunk:
                        on_chunk(event["type"], chunk)
                elif event.get("type") == "done":
                    return {**{k: v.decode("utf-8", errors="replace") for k, v in output.items()}, **event}
                else:
                    raise RuntimeError(event.get("error", "invalid task-terminal response"))
    raise RuntimeError("task terminal disconnected before command completion")


def socket_from_metadata(params: dict[str, Any]) -> str | None:
    metadata = params.get("metadata") or {}
    runtime = metadata.get("task_sandbox") or {}
    transport = runtime.get("terminal_transport")
    if transport is None:
        return None
    if transport != SCHEMA or runtime.get("terminal_socket") != PUBLIC_SOCKET:
        raise ValueError("unsupported task terminal transport")
    if not Path(PUBLIC_SOCKET).is_socket():
        raise RuntimeError("declared task terminal socket is unavailable")
    return PUBLIC_SOCKET


def shell_proxy_main() -> int:
    # Native shell services invoke a configured executable as `shell -c script`.
    # No fallback to the Agent container is permitted.
    argv = sys.argv[1:]
    if "--pty" in argv:
        return pty_proxy_main()
    index = next((i for i, value in enumerate(argv) if value in {"-c", "-lc", "-lic"}), None)
    if index is None or index + 1 >= len(argv):
        raise ValueError("task shell requires a command string")
    import shlex
    command = argv[index + 1]
    if argv[index + 2:]:
        command = "/bin/bash -c " + shlex.quote(command) + " " + shlex.join(argv[index + 2:])
    def emit(kind, chunk):
        stream = sys.stdout if kind == "stdout" else sys.stderr
        stream.buffer.write(chunk)
        stream.buffer.flush()
    result = run_command(PUBLIC_SOCKET, command, cwd=os.getcwd(), timeout_seconds=86400,
                         stdin=sys.stdin.read(), on_chunk=emit)
    if result.get("limit_reason"):
        sys.stderr.write("\nTask terminal limit: " + result["limit_reason"] + "\n")
    return result["returncode"]


def pty_proxy_main() -> int:
    """Duplex PTY bytes; the native adapter retains prompt parsing and tool policy."""
    import termios
    import tty
    previous = termios.tcgetattr(0) if os.isatty(0) else None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(PUBLIC_SOCKET)
        _send(connection, {"op": "pty", "cwd": os.getcwd(), "timeout_seconds": 86400,
                           "terminal_environment": {k: os.environ[k] for k in
                                                    ("PS1", "PROMPT_COMMAND", "TERM") if k in os.environ}})
        event = _read_line(connection)
        if event.get("type") != "ready":
            raise RuntimeError("task PTY refused startup: " + repr(event))
        try:
            if previous is not None:
                tty.setraw(0)
            pending = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_READ)
                selector.register(0, selectors.EVENT_READ)
                while True:
                    for key, _ in selector.select():
                        data = os.read(key.fd, 65536)
                        if not data:
                            return 0
                        if key.fd == 0:
                            connection.sendall(data)
                            continue
                        pending.extend(data)
                        while b"\n" in pending:
                            line, _, tail = pending.partition(b"\n")
                            pending[:] = tail
                            event = json.loads(line)
                            if event.get("type") in {"stdout", "stderr"}:
                                sys.stdout.buffer.write(base64.b64decode(event["data"]))
                                sys.stdout.buffer.flush()
                            elif event.get("type") == "done":
                                return event["returncode"]
                            else:
                                raise RuntimeError(event.get("error", "invalid PTY event"))
        finally:
            if previous is not None:
                termios.tcsetattr(0, termios.TCSANOW, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "control", "shell"))
    parser.add_argument("--socket", default=PUBLIC_SOCKET)
    parser.add_argument("--control", default="/tmp/trievo-task-terminal-control/control.sock")
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--audit", default="/tmp/trievo-task-terminal-audit.json")
    parser.add_argument("--request")
    parser.add_argument("-c", dest="command")
    parser.add_argument("--timeout", type=float, default=86400)
    args = parser.parse_args()
    if args.mode == "serve":
        service = TerminalService(Path(args.workspace), Path(args.audit))
        public = serve(Path(args.socket), service)
        private = serve(Path(args.control), service, private=True)
        try:
            while True:
                time.sleep(0.5)
        finally:
            public.shutdown()
            private.shutdown()
    elif args.mode == "control":
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(args.control)
            _send(connection, json.loads(args.request))
            response = _read_line(connection)
            print(json.dumps(response))
            return 1 if response.get("type") == "error" else 0
    else:
        def emit(kind, chunk):
            stream = sys.stdout if kind == "stdout" else sys.stderr
            stream.buffer.write(chunk)
            stream.buffer.flush()
        result = run_command(args.socket, args.command, cwd=os.getcwd(), timeout_seconds=args.timeout,
                             stdin=sys.stdin.read(), on_chunk=emit)
        if result.get("limit_reason"):
            sys.stderr.write("\nTask terminal limit: " + result["limit_reason"] + "\n")
        return result["returncode"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
