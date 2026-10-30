"""Start, finish and collect one live τ² session container per Interaction.

The session container runs the Turn's τ² image with the Stream Workspace
volume at ``/workspace`` and default networking, so the user simulator can
reach its LLM. Only the credential and proxy variables are passed, by name, so
their values never appear on a command line. The private task JSON is copied
into the container (never into the Workspace). The agent talks to the session
through ``/workspace/.tau2/``; the runtime finishes it through a
container-local control socket and copies the sealed record out before the
Workspace is synchronized back.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")
SERVICE_PYTHON = "/opt/tau2/venv/bin/python"
READY_TIMEOUT_SECONDS = 120
# The orchestrator may be waiting on one user-simulator request (120 s timeout,
# 3 retries) when the Interaction ends; the NL judge may then take as long.
FINISH_GRACE_SECONDS = 900


# The image has no procps; look for the service in /proc.
_SERVICE_ALIVE = (
    "import os, sys; sys.exit(0 if any(b'tau2_session.service' in open(f'/proc/{p}/cmdline', 'rb').read()"
    " for p in os.listdir('/proc') if p.isdigit()) else 1)"
)


class Tau2LiveSessionError(RuntimeError):
    """The live session could not be started or collected (infrastructure)."""


def is_live_profile(profile: dict[str, Any] | None) -> bool:
    live = (profile or {}).get("live_session")
    return isinstance(live, dict) and live.get("kind") == "tau2_live_session"


def _docker() -> str:
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    return str(candidate) if candidate.is_file() else "docker"


def _run(args: list[str], *, timeout: float = 120, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_docker(), *args], text=True, encoding="utf-8", errors="replace", capture_output=True,
        check=False, timeout=timeout, input=input_text,
    )


class Tau2LiveSession:
    def __init__(
        self,
        *,
        profile: dict[str, Any],
        turn_dir: Path,
        workspace_mount_spec: str,
        timeout_seconds: float,
        live_config: dict[str, Any] | None = None,
        workspace_owner: tuple[int, int] | None = None,
    ) -> None:
        if not is_live_profile(profile):
            raise Tau2LiveSessionError("runtime profile does not declare a tau2 live session")
        self.profile = profile
        self.image = str(profile["task_sandbox_image_digest"])
        self.assets = Path(turn_dir) / "evaluation_assets"
        self.meta = json.loads((self.assets / "tau2_meta.json").read_text(encoding="utf-8"))
        # Run-scoped routing overrides leave the package's private assets intact.
        allowed = {"user_llm_base_url", "user_llm", "user_llm_api_key_env"}
        if live_config is not None and (
            not isinstance(live_config, dict) or set(live_config) - allowed
            or any(not isinstance(v, str) or not v for v in live_config.values())
        ):
            raise Tau2LiveSessionError("invalid live tau2 routing configuration")
        self.live_config = {**(self.meta.get("live_config") or {}), **(live_config or {})}
        self.workspace_mount_spec = workspace_mount_spec
        self.timeout_seconds = float(timeout_seconds)
        self.name = f"trievo-l2-tau2-{uuid4().hex[:12]}"
        self.credential_env = [str(n) for n in profile["live_session"].get("credential_env", [])]
        if live_config and live_config.get("user_llm_api_key_env"):
            self.credential_env = [live_config["user_llm_api_key_env"]]
        self.evidence: dict[str, Any] = {}
        self._started = False
        if workspace_owner is not None and (
            len(workspace_owner) != 2
            or any(type(value) is not int or value < 0 for value in workspace_owner)
        ):
            raise Tau2LiveSessionError("invalid tau2 workspace owner")
        self.service_user = ":".join(map(str, workspace_owner)) if workspace_owner else None
        self.private_root = "/tmp/trievo-tau2-private" if workspace_owner else "/out"
        self.control_socket = (self.private_root + "/control.sock" if workspace_owner
                               else "/run/trievo-tau2/control.sock")

    def _service_exec(self) -> list[str]:
        return ["exec", *(["--user", self.service_user] if self.service_user else [])]

    def start(self) -> dict[str, Any]:
        missing = [name for name in self.credential_env if not os.environ.get(name)]
        if missing:
            raise Tau2LiveSessionError(f"live tau2 session needs {missing} in the runtime environment")
        env_args: list[str] = []
        for name in (*self.credential_env, *PROXY_ENV_NAMES):
            if os.environ.get(name):
                env_args += ["-e", name]
        started = _run([
            "run", "-d", "--name", self.name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
            "--mount", self.workspace_mount_spec + (",volume-nocopy" if self.service_user else ""),
            *env_args, "-e", "TAU2_RUN_ROOT=" + self.private_root + "/run",
            self.image, "sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
        ])
        if started.returncode != 0:
            self.close()
            raise Tau2LiveSessionError("failed to start the tau2 session container: " + started.stderr[-2000:])
        self._started = True
        made = _run(["exec", self.name, "mkdir", "-p", "/private", "/out"])
        if made.returncode != 0:
            raise Tau2LiveSessionError("tau2 session container setup failed: " + made.stderr[-2000:])
        with tempfile.TemporaryDirectory(prefix="trievo-tau2-") as scratch:
            config = Path(scratch) / "config.json"
            config.write_text(json.dumps(self.live_config), encoding="utf-8")
            for source, target in ((self.assets / "tau2_task.json", "/private/task.json"), (config, "/private/config.json")):
                copied = _run(["cp", str(source), f"{self.name}:{target}"])
                if copied.returncode != 0:
                    raise Tau2LiveSessionError("failed to stage the tau2 task: " + copied.stderr[-2000:])
        if self.service_user:
            # Cap-dropped root cannot write the Task UID's 0755 workspace.
            # Keep service control and records private to this container.
            prepared = _run([*self._service_exec(), self.name, "sh", "-c",
                             "umask 077; mkdir -p " + self.private_root])
            if prepared.returncode != 0:
                raise Tau2LiveSessionError("failed to prepare private tau2 service directory")
            readable = _run([*self._service_exec(), self.name, "chmod", "644", "/private/task.json", "/private/config.json"])
            if readable.returncode != 0:
                raise Tau2LiveSessionError("failed to prepare private tau2 inputs")
        service = _run([
            *self._service_exec(), "-d", "-w", self.private_root, self.name, "sh", "-c",
            f"exec {SERVICE_PYTHON} -m tau2_session.service --task /private/task.json"
            f" --domain {self.meta['domain']} --config /private/config.json"
            f" --timeout {int(self.timeout_seconds)} --tau2-revision {self.meta['revision']}"
            f" --control {self.control_socket} --out {self.private_root}"
            f" > {self.private_root}/service.log 2>&1",
        ])
        if service.returncode != 0:
            raise Tau2LiveSessionError("failed to launch the tau2 session service: " + service.stderr[-2000:])
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            ready = _run(["exec", self.name, "test", "-S", "/workspace/.tau2/session.sock"])
            if ready.returncode == 0:
                break
            alive = _run(["exec", self.name, SERVICE_PYTHON, "-c", _SERVICE_ALIVE])
            if alive.returncode != 0:
                raise Tau2LiveSessionError("tau2 session service exited: " + self._service_log()[-3000:])
            time.sleep(0.5)
        else:
            raise Tau2LiveSessionError("tau2 session service did not become ready: " + self._service_log()[-3000:])
        inspected = _run(["inspect", self.name, "--format", "{{.Image}} {{.State.Running}}"])
        self.evidence = {
            "kind": "tau2_live_session",
            "container_name": self.name,
            "image_digest": inspected.stdout.strip().split(" ")[0],
            "domain": self.meta["domain"],
            "task_id": self.meta["task_id"],
            "tau2_revision": self.meta["revision"],
            "network": "default (user-simulator LLM only; the agent reaches the session through /workspace/.tau2)",
            "agent_entrypoint": "/workspace/.tau2/tau2.py",
            "live_config": dict(self.live_config),
            **({"service_user": self.service_user, "private_root": self.private_root}
               if self.service_user else {}),
        }
        return dict(self.evidence)

    def finish(self) -> dict[str, Any]:
        """End the conversation if still open, score it and return the record."""
        if not self._started:
            raise Tau2LiveSessionError("tau2 session was not started")
        ctl = _run([
            *self._service_exec(), self.name, SERVICE_PYTHON, "-m", "tau2_session.ctl", "finish",
            "--socket", self.control_socket,
            "--grace-seconds", str(FINISH_GRACE_SECONDS),
        ], timeout=FINISH_GRACE_SECONDS + 120)
        if ctl.returncode != 0:
            raise Tau2LiveSessionError("tau2 session finish failed: " + (ctl.stderr or ctl.stdout)[-2000:]
                                       + self._service_log()[-2000:])
        with tempfile.TemporaryDirectory(prefix="trievo-tau2-") as scratch:
            target = Path(scratch) / "record.json"
            copied = _run(["cp", f"{self.name}:{self.private_root}/record.json", str(target)])
            if copied.returncode != 0 or not target.is_file():
                raise Tau2LiveSessionError("tau2 session record is missing: " + copied.stderr[-2000:])
            record = json.loads(target.read_text(encoding="utf-8"))
        # The service removed /workspace/.tau2; make sure no socket or CLI is
        # left for the next Interaction or the Workspace sync.
        _run([*self._service_exec(), self.name, "rm", "-rf", "/workspace/.tau2"])
        return record

    def _service_log(self) -> str:
        log = _run([*self._service_exec(), self.name, "cat", self.private_root + "/service.log"])
        return log.stdout if log.returncode == 0 else ""

    def close(self) -> None:
        _run(["rm", "-f", self.name])
        self._started = False
