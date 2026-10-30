"""Host control of the task terminal; private control is never mounted in SUT."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any

from .task_terminal import PUBLIC_SOCKET, SCHEMA


class TaskTerminalController:
    def __init__(self, docker: str, container: str):
        self.docker, self.container = docker, container
        self.interactions: list[dict[str, Any]] = []

    def _run(self, args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
        result = subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("task terminal control failed: " + (result.stderr or result.stdout)[-2000:])
        return result

    def start(self) -> dict[str, Any]:
        probe = self._run(["exec", self.container, "python", "-c",
                          "import tempfile,json,os; f=tempfile.TemporaryFile(dir='/workspace'); f.close(); "
                          "s=os.stat('/workspace'); print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),"
                          "'workspace_uid':s.st_uid,'workspace_gid':s.st_gid,'workspace_mode':oct(s.st_mode & 511)}))"])
        permissions = json.loads(probe.stdout)
        source = Path(__file__).with_name("task_terminal.py")
        self._run(["cp", str(source), self.container + ":/tmp/trievo-task-terminal.py"])
        self._run(["exec", "-d", self.container, "sh", "-c",
                   "exec python /tmp/trievo-task-terminal.py serve > /tmp/trievo-task-terminal.log 2>&1"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = subprocess.run([self.docker, "exec", self.container, "test", "-S", PUBLIC_SOCKET],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"terminal_transport": SCHEMA, "terminal_socket": PUBLIC_SOCKET,
                        "terminal_service_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "task_permissions": permissions,
                        "terminal_interactions": self.interactions}
            time.sleep(.1)
        log = self._run(["exec", self.container, "cat", "/tmp/trievo-task-terminal.log"]).stdout
        raise RuntimeError("task terminal did not become ready: " + log[-2000:])

    def control(self, value: dict[str, Any]) -> dict[str, Any]:
        result = self._run(["exec", self.container, "python", "/tmp/trievo-task-terminal.py", "control",
                            "--request", json.dumps(value)], timeout=20)
        response = json.loads(result.stdout)
        if response.get("schema") != SCHEMA:
            raise RuntimeError("task terminal returned an invalid control receipt")
        return response

    def begin(self, identity: str, limits: dict[str, Any]) -> None:
        receipt = self.control({"op": "begin", "interaction": identity,
                                "episode_timeout_seconds": limits["episode_timeout_seconds"],
                                "terminal_timeout_seconds": limits["terminal_timeout_seconds"],
                                # Internal bootstrap shell commands are not public tool calls.
                                # Adapters enforce the canonical model/tool counts.
                                "max_commands": 10000})
        if receipt.get("ready") is not True or receipt.get("interaction") != identity:
            raise RuntimeError("task terminal refused the Interaction boundary")

    def end(self) -> dict[str, Any]:
        receipt = self.control({"op": "end"})
        self.interactions.append(receipt)
        return receipt
