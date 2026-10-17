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



_GUARDED_SERVICE = r'''
import json, subprocess, time
from pathlib import Path
from resource_guard import require_container_resources
root = Path("/private")
spec = json.loads((root / "spec.json").read_text())
Path("/out").mkdir(exist_ok=True)
resources = require_container_resources(enforcement_mode=spec["mode"])
(root / "resources.json").write_text(json.dumps({"nonce": spec["nonce"], "resource_readback": resources}))
with Path("/out/service.log").open("w") as log:
    log.write(json.dumps({"resource_readback": resources, "resource_enforcement": resources["resource_enforcement"]}) + "\n")
    log.flush()
    deadline = time.monotonic() + spec["ready_timeout"]
    while not (root / "authorization.json").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("host resource authorization timed out")
        time.sleep(0.1)
    authorization = json.loads((root / "authorization.json").read_text())
    if authorization.get("nonce") != spec["nonce"]:
        raise RuntimeError("invalid host resource authorization")
    ready_socket = Path("/workspace/.tau2/session.sock")
    if ready_socket.exists() or ready_socket.is_symlink():
        ready_socket.unlink()
    process = subprocess.Popen(spec["command"], cwd="/out", stdout=log, stderr=subprocess.STDOUT)
    while not Path("/workspace/.tau2/session.sock").is_socket():
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError("tau2 service exited or did not create its socket")
        time.sleep(0.1)
    (root / "ready.json").write_text(json.dumps({"nonce": spec["nonce"]}))
    raise SystemExit(process.wait())
'''

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
    ) -> None:
        if not is_live_profile(profile):
            raise Tau2LiveSessionError("runtime profile does not declare a tau2 live session")
        self.profile = profile
        self.image = str(profile["task_sandbox_image_digest"])
        self.assets = Path(turn_dir) / "evaluation_assets"
        self.meta = json.loads((self.assets / "tau2_meta.json").read_text(encoding="utf-8"))
        self.workspace_mount_spec = workspace_mount_spec
        self.timeout_seconds = float(timeout_seconds)
        self.name = f"trievo-l2-tau2-{uuid4().hex[:12]}"
        self.credential_env = [str(n) for n in profile["live_session"].get("credential_env", [])]
        self.evidence: dict[str, Any] = {}
        self._started = False

    def start(self) -> dict[str, Any]:
        missing = [name for name in self.credential_env if not os.environ.get(name)]
        if missing:
            raise Tau2LiveSessionError(f"live tau2 session needs {missing} in the runtime environment")
        if self.profile.get("environment_version"):
            return self._start_guarded()
        env_args: list[str] = []
        for name in (*self.credential_env, *PROXY_ENV_NAMES):
            if os.environ.get(name):
                env_args += ["-e", name]
        started = _run([
            "run", "-d", "--name", self.name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
            "--mount", self.workspace_mount_spec, *env_args, "-e", "TAU2_RUN_ROOT=/out/run",
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
            config.write_text(json.dumps(self.meta.get("live_config") or {}), encoding="utf-8")
            for source, target in ((self.assets / "tau2_task.json", "/private/task.json"), (config, "/private/config.json")):
                copied = _run(["cp", str(source), f"{self.name}:{target}"])
                if copied.returncode != 0:
                    raise Tau2LiveSessionError("failed to stage the tau2 task: " + copied.stderr[-2000:])
        service = _run([
            "exec", "-d", "-w", "/out", self.name, "sh", "-c",
            f"exec {SERVICE_PYTHON} -m tau2_session.service --task /private/task.json"
            f" --domain {self.meta['domain']} --config /private/config.json"
            f" --timeout {int(self.timeout_seconds)} --tau2-revision {self.meta['revision']}"
            " > /out/service.log 2>&1",
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
        }
        return dict(self.evidence)

    def _start_guarded(self) -> dict[str, Any]:
        from ..container_resources import (
            ContainerResourceError, docker_resource_capability_preflight,
            require_container_resources, resource_enforcement_mode,
        )
        import trievo_bench.container_resources as resource_module

        try:
            mode = resource_enforcement_mode()
            capability = docker_resource_capability_preflight(docker=_docker())
        except ContainerResourceError as exc:
            raise Tau2LiveSessionError(str(exc)) from exc
        if not capability.get("allowed", capability["passed"]):
            raise Tau2LiveSessionError("tau2 service resource enforcement unavailable: " + "; ".join(capability["errors"]))
        nonce = uuid4().hex
        env_args = [value for name in (*self.credential_env, *PROXY_ENV_NAMES) if os.environ.get(name)
                    for value in ("-e", name)]
        command = [SERVICE_PYTHON, "-m", "tau2_session.service", "--task", "/private/task.json",
                   "--domain", self.meta["domain"], "--config", "/private/config.json",
                   "--timeout", str(int(self.timeout_seconds)), "--tau2-revision", self.meta["revision"]]
        try:
            with tempfile.TemporaryDirectory(prefix="trievo-tau2-guard-") as scratch:
                local = Path(scratch)
                payload = local / "payload"
                payload.mkdir()
                (payload / "spec.json").write_text(json.dumps({"nonce": nonce, "mode": mode, "command": command,
                    "ready_timeout": READY_TIMEOUT_SECONDS}))
                (payload / "config.json").write_text(json.dumps(self.meta.get("live_config") or {}))
                (payload / "bootstrap.py").write_text(_GUARDED_SERVICE)
                import shutil
                shutil.copy2(resource_module.__file__, payload / "resource_guard.py")
                shutil.copy2(self.assets / "tau2_task.json", payload / "task.json")
                created = _run(["create", "--name", self.name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--pids-limit", "256", "--memory", "4g", "--cpus", "2", "--mount", self.workspace_mount_spec,
                    *env_args, "-e", "TAU2_RUN_ROOT=/out/run", "--entrypoint", SERVICE_PYTHON,
                    self.image, "/private/bootstrap.py"])
                if created.returncode:
                    raise Tau2LiveSessionError("failed to create guarded tau2 service: " + created.stderr[-2000:])
                self._started = True
                copied = _run(["cp", str(payload), f"{self.name}:/private"])
                if copied.returncode or _run(["start", self.name]).returncode:
                    raise Tau2LiveSessionError("failed to stage/start guarded tau2 service")
                observed = self._wait_guard_marker("resources", nonce, local)
                readback = observed.get("resource_readback")
                if not isinstance(readback, dict):
                    raise Tau2LiveSessionError("guarded tau2 service resource snapshot is missing")
                checked = require_container_resources(readback, enforcement_mode=mode)
                inspected = _run(["inspect", self.name, "--format", "{{.Image}} {{.State.Running}} {{.HostConfig.NetworkMode}}"])
                if inspected.returncode or inspected.stdout.strip().split() != [self.image, "true", "default"]:
                    raise Tau2LiveSessionError("guarded tau2 service image/network readback mismatch")
                # The copied authorization is private to this fresh container;
                # no simulator/import/model code runs until host validation.
                authorization = local / "authorization.json"
                authorization.write_text(json.dumps({"nonce": nonce}))
                if _run(["cp", str(authorization), f"{self.name}:/private/authorization.json"]).returncode:
                    raise Tau2LiveSessionError("failed to authorize guarded tau2 service")
                self._wait_guard_marker("ready", nonce, local)
                self.evidence = {"kind": "tau2_live_session", "container_name": self.name,
                    "image_digest": self.image, "domain": self.meta["domain"], "task_id": self.meta["task_id"],
                    "tau2_revision": self.meta["revision"], "network": "default (user-simulator LLM only)",
                    "agent_entrypoint": "/workspace/.tau2/tau2.py", "resource_readback": checked,
                    "resource_enforcement": checked["resource_enforcement"],
                    "requested_resources": checked["requested_resources"], "observed_resources": checked["observed_resources"],
                    "resource_observation_provenance": "actual service container before host authorization/model calls"}
                return dict(self.evidence)
        except (ContainerResourceError, OSError, ValueError, subprocess.SubprocessError, Tau2LiveSessionError) as exc:
            self.close()
            raise Tau2LiveSessionError("guarded tau2 service startup failed: " + str(exc)) from exc

    def _wait_guard_marker(self, kind: str, nonce: str, local: Path) -> dict[str, Any]:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # A fresh destination per attempt rejects no-op cp and stale files.
            target = local / (kind + "-" + uuid4().hex + ".json")
            copied = _run(["cp", f"{self.name}:/private/{kind}.json", str(target)])
            if copied.returncode == 0 and target.is_file():
                value = json.loads(target.read_text())
                if not isinstance(value, dict) or value.get("nonce") != nonce:
                    raise Tau2LiveSessionError("guarded tau2 service nonce mismatch")
                return value
            state = _run(["inspect", self.name, "--format", "{{.State.Running}}"])
            if state.returncode or state.stdout.strip() != "true":
                raise Tau2LiveSessionError("guarded tau2 service exited before " + kind + ": " + self._service_log()[-2000:])
            time.sleep(0.2)
        raise Tau2LiveSessionError("guarded tau2 service timed out before " + kind)

    def finish(self) -> dict[str, Any]:
        """End the conversation if still open, score it and return the record."""
        if not self._started:
            raise Tau2LiveSessionError("tau2 session was not started")
        ctl = _run([
            "exec", self.name, SERVICE_PYTHON, "-m", "tau2_session.ctl", "finish",
            "--grace-seconds", str(FINISH_GRACE_SECONDS),
        ], timeout=FINISH_GRACE_SECONDS + 120)
        if ctl.returncode != 0:
            raise Tau2LiveSessionError("tau2 session finish failed: " + (ctl.stderr or ctl.stdout)[-2000:]
                                       + self._service_log()[-2000:])
        with tempfile.TemporaryDirectory(prefix="trievo-tau2-") as scratch:
            target = Path(scratch) / "record.json"
            copied = _run(["cp", f"{self.name}:/out/record.json", str(target)])
            if copied.returncode != 0 or not target.is_file():
                raise Tau2LiveSessionError("tau2 session record is missing: " + copied.stderr[-2000:])
            record = json.loads(target.read_text(encoding="utf-8"))
        # The service removed /workspace/.tau2; make sure no socket or CLI is
        # left for the next Interaction or the Workspace sync.
        _run(["exec", self.name, "rm", "-rf", "/workspace/.tau2"])
        return record

    def _service_log(self) -> str:
        log = _run(["exec", self.name, "cat", "/out/service.log"])
        return log.stdout if log.returncode == 0 else ""

    def close(self) -> None:
        _run(["rm", "-f", self.name])
        self._started = False
