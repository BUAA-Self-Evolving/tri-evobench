"""Docker-daemon-owned volume transport for nested/rootless environments.

The Docker client can stream files with ``docker cp`` even when the daemon
cannot resolve the client's host paths for bind mounts. This mirror keeps the
existing host-side checkpoint model while containers share a named volume
owned by the daemon.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class DockerVolumeError(RuntimeError):
    pass


_TRUE_VALUES = {"1", "true", "yes", "on", "required"}
_FALSE_VALUES = {"", "0", "false", "no", "off", "disabled"}
_MAX_STORAGE_COPY_FAILURES = 5
_STORAGE_ERROR_PATTERNS = (
    "no space left on device",
    "error setting up pivot dir",
    "input/output error",
    "stale file handle",
    "transport endpoint is not connected",
)


def docker_volume_transport_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environ is None else environ
    raw = source.get("TRIEVO_DOCKER_VOLUME_TRANSPORT", "off").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    accepted = ", ".join(repr(value) for value in sorted(_TRUE_VALUES | _FALSE_VALUES))
    raise DockerVolumeError(
        f"TRIEVO_DOCKER_VOLUME_TRANSPORT must be one of: {accepted}"
    )


@dataclass
class DockerVolumeMirror:
    """A host mirror synchronized through a private Docker carrier container."""

    host_path: Path
    role: str
    volume_name: str
    carrier_name: str
    helper_image: str
    uid: int
    gid: int
    _closed: bool = False
    _storage_gate: Any | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        host_path: Path,
        *,
        role: str,
        writable_uid: int | None = None,
        writable_gid: int | None = None,
        helper_image: str | None = None,
    ) -> "DockerVolumeMirror":
        requested = host_path.expanduser()
        if requested.is_symlink():
            raise DockerVolumeError(
                f"Docker volume mirror does not accept a symlink root: {requested}"
            )
        source = requested.resolve()
        if not source.is_dir():
            raise DockerVolumeError(
                f"Docker volume mirror requires a directory: {source}"
            )
        safe_role = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "data"
        suffix = uuid.uuid4().hex[:12]
        volume_name = f"trievo-{safe_role}-{suffix}"
        carrier_name = f"trievo-{safe_role}-carrier-{suffix}"
        image = helper_image or os.environ.get(
            "TRIEVO_DOCKER_TRANSFER_IMAGE", "trievo-l2:0.1"
        )
        uid = writable_uid if writable_uid is not None else _host_uid()
        gid = writable_gid if writable_gid is not None else _host_gid()
        mirror = cls(
            source,
            safe_role,
            volume_name,
            carrier_name,
            image,
            uid,
            gid,
            _storage_gate=_storage_gate_from_environment(),
        )
        try:
            mirror._create_resources()
            mirror.sync_from_host()
            return mirror
        except BaseException:
            mirror.close()
            raise

    def mount(self, target: str, *, read_only: bool = False) -> str:
        if not target.startswith("/"):
            raise DockerVolumeError("container volume target must be absolute")
        suffix = ":ro" if read_only else ""
        return f"{self.volume_name}:{target}{suffix}"

    def mount_spec(self, target: str, *, read_only: bool = False) -> str:
        """Return an unambiguous Docker ``--mount`` specification."""
        if not target.startswith("/"):
            raise DockerVolumeError("container volume target must be absolute")
        suffix = ",readonly" if read_only else ""
        return f"type=volume,src={self.volume_name},dst={target}{suffix}"

    def sync_from_host(self) -> None:
        self._require_open()

        def copy() -> None:
            _run_docker(
                ["exec", self.carrier_name, "python", "-c", _CLEAR_VOLUME_SCRIPT]
            )
            _run_docker(["cp", f"{self.host_path}/.", f"{self.carrier_name}:/volume"])
            _run_docker(
                [
                    "exec",
                    self.carrier_name,
                    "chown",
                    "-R",
                    f"{self.uid}:{self.gid}",
                    "/volume",
                ]
            )

        self._copy_with_storage_gate(copy)

    def sync_to_host(self) -> None:
        self._require_open()

        def copy() -> None:
            staged = Path(
                tempfile.mkdtemp(
                    prefix=f".{self.host_path.name}-docker-pull-",
                    dir=self.host_path.parent,
                )
            )
            backup = self.host_path.parent / (
                f".{self.host_path.name}-docker-backup-{uuid.uuid4().hex[:12]}"
            )
            try:
                _run_docker(["cp", f"{self.carrier_name}:/volume/.", str(staged)])
                shutil.copystat(self.host_path, staged, follow_symlinks=False)
                self.host_path.rename(backup)
                try:
                    staged.rename(self.host_path)
                except BaseException:
                    backup.rename(self.host_path)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
            finally:
                shutil.rmtree(staged, ignore_errors=True)

        self._copy_with_storage_gate(copy)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _run_docker(["rm", "-f", self.carrier_name], check=False)
        _run_docker(["volume", "rm", "-f", self.volume_name], check=False)

    def _create_resources(self) -> None:
        _run_docker(
            [
                "volume",
                "create",
                "--label",
                "io.trievo.managed=true",
                "--label",
                f"io.trievo.role={self.role}",
                self.volume_name,
            ]
        )
        _run_docker(
            [
                "run",
                "-d",
                "--name",
                self.carrier_name,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--mount",
                self.mount_spec("/volume"),
                self.helper_image,
                "sleep",
                "infinity",
            ]
        )

    def _require_open(self) -> None:
        if self._closed:
            raise DockerVolumeError("Docker volume mirror is closed")

    def _copy_with_storage_gate(self, operation) -> None:
        gate = self._storage_gate
        if gate is None:
            operation()
            return
        failures = 0
        while True:
            gate.wait()
            try:
                operation()
                return
            except BaseException as exc:
                if not _is_storage_error(exc):
                    raise
                failures += 1
                gate.trip(str(exc))
                if failures >= _MAX_STORAGE_COPY_FAILURES:
                    raise


_CLEAR_VOLUME_SCRIPT = """
from pathlib import Path
import shutil

root = Path('/volume')
for child in root.iterdir():
    if child.is_dir() and not child.is_symlink():
        shutil.rmtree(child)
    else:
        child.unlink(missing_ok=True)
""".strip()


def _docker_executable() -> str:
    configured = os.environ.get("DOCKER_EXE", "").strip()
    if configured:
        return configured
    resolved = shutil.which("docker")
    if resolved is None:
        raise DockerVolumeError("docker executable was not found")
    return resolved


def _run_docker(
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [_docker_executable(), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if check:
            raise DockerVolumeError(f"Docker volume command failed: {args[0]}") from exc
        return subprocess.CompletedProcess(args, 1, "", str(exc))
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise DockerVolumeError(
            f"Docker volume command failed ({args[0]}): {detail.strip()}"
        )
    return completed


def _storage_recovery_module():
    try:
        from . import storage_recovery  # type: ignore[attr-defined]
    except ImportError:
        if os.environ.get("TRIEVO_STORAGE_GUARD") == "1":
            raise
        return None
    return storage_recovery


def _storage_gate_from_environment():
    module = _storage_recovery_module()
    if module is None:
        if os.environ.get("TRIEVO_STORAGE_GUARD") == "1":
            raise DockerVolumeError("storage guard requested but unavailable")
        return None
    factory = getattr(module, "from_environment", None)
    if callable(factory):
        return factory()
    storage_gate = getattr(module, "StorageGate", None)
    class_factory = getattr(storage_gate, "from_environment", None)
    if callable(class_factory):
        return class_factory()
    if os.environ.get("TRIEVO_STORAGE_GUARD") == "1":
        raise DockerVolumeError("storage guard requested but factory unavailable")
    return None


def _is_storage_error(exc_or_str: BaseException | str) -> bool:
    module = _storage_recovery_module()
    detector = getattr(module, "is_storage_error", None) if module is not None else None
    if callable(detector):
        return bool(detector(exc_or_str))
    text = str(exc_or_str).lower()
    return any(pattern in text for pattern in _STORAGE_ERROR_PATTERNS)


def _host_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 1000


def _host_gid() -> int:
    return os.getgid() if hasattr(os, "getgid") else 1000
