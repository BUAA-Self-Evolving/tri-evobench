"""Cooperative storage admission/wait gate for one matrix, not a disk quota.

The shared state lives on the artifact filesystem. No global pruning, stopping
other jobs, or replaying model/tool calls is performed here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def is_storage_error(error) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in (
        "no space left on device", "enospc", "disk quota exceeded", "edquot",
        "error setting up pivot dir", ".pivot_root", "session storage could not be written",
        "read-only file system", "stale file handle", "input/output error",
        "transport endpoint is not connected",
        "storagepressureerror", "storageunavailable", "storage guard",
    ))


class StorageUnavailable(RuntimeError):
    pass


class StorageGate:
    def __init__(self, root, path, *, min_free_gib=20, resume_free_gib=25,
                 min_free_inodes=10000, interval=2, recovery_seconds=30,
                 probe_image="trievo-l2:0.1", probe=None):
        if not all(math.isfinite(float(n)) and float(n) > 0 for n in
                   (min_free_gib, resume_free_gib, interval, recovery_seconds)):
            raise ValueError("storage thresholds and intervals must be finite and positive")
        if resume_free_gib < min_free_gib or min_free_inodes < 0:
            raise ValueError("storage resume floor must be >= stop floor; inode floor must be >= 0")
        self.root = Path(root).absolute()
        self.path = Path(path).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.floor = int(min_free_gib * 1024**3)
        self.resume_floor = int(resume_free_gib * 1024**3)
        self.inode_floor = int(min_free_inodes)
        self.interval = float(interval)
        self.recovery_seconds = float(recovery_seconds)
        self.probe_image = probe_image
        self.probe = probe or self._docker_probe
        self.state_path = self.root / "health.json"

    @classmethod
    def from_environment(cls):
        if os.environ.get("TRIEVO_STORAGE_GUARD") != "1":
            return None
        root = os.environ.get("TRIEVO_STORAGE_STATE_ROOT")
        path = os.environ.get("TRIEVO_STORAGE_PATH")
        if not root or not path:
            raise ValueError("storage guard needs TRIEVO_STORAGE_STATE_ROOT and TRIEVO_STORAGE_PATH")
        return cls(root, path,
                   min_free_gib=float(os.environ.get("TRIEVO_STORAGE_MIN_FREE_GIB", "20")),
                   resume_free_gib=float(os.environ.get("TRIEVO_STORAGE_RESUME_FREE_GIB", "25")),
                   min_free_inodes=int(os.environ.get("TRIEVO_STORAGE_MIN_FREE_INODES", "10000")),
                   interval=float(os.environ.get("TRIEVO_STORAGE_INTERVAL", "2")),
                   recovery_seconds=float(os.environ.get("TRIEVO_STORAGE_RECOVERY_SECONDS", "30")),
                   probe_image=os.environ.get("TRIEVO_STORAGE_PROBE_IMAGE", "trievo-l2:0.1"))

    @contextlib.contextmanager
    def _lock(self):
        # One lock shared by dispatcher and all workers; health checks/probes
        # cannot accidentally clear a concurrent fault notification.
        with (self.root / "health.lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0"); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _load(self):
        if not self.state_path.exists():
            return {"healthy": False, "reason": "initial storage qualification", "initial": True}
        return json.loads(self.state_path.read_text())

    def _save(self, state, old):
        state.update(path=str(self.path), floor_bytes=self.floor,
                     resume_floor_bytes=self.resume_floor, inode_floor=self.inode_floor)
        temporary = self.root / (".health-" + uuid.uuid4().hex + ".json")
        try:
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)
        if (old.get("healthy"), old.get("reason")) != (state.get("healthy"), state.get("reason")):
            with (self.root / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(state) + "\n")

    def trip(self, reason):
        with self._lock():
            old = self._load()
            state = dict(old)
            state.update(healthy=False, initial=False, reason=str(reason)[-1000:],
                         fault_at=time.time(), healthy_since=None, checked_at=0)
            self._save(state, old)

    def check(self):
        with self._lock():
            old = self._load()
            now = time.time()
            # Cache only already healthy checks; paused clients cannot make a
            # missing mount healthy by repeatedly calling this method.
            if old.get("healthy") and now - old.get("checked_at", 0) < self.interval:
                return old
            state = dict(old)
            state.update(checked_at=now)
            try:
                if old.get("path", str(self.path)) != str(self.path):
                    raise StorageUnavailable("storage guard watched path changed; explicit requalification required")
                if not self.path.is_dir():
                    raise StorageUnavailable("storage guard watched directory is unavailable")
                device = self.path.stat().st_dev
                if old.get("device", device) != device:
                    raise StorageUnavailable("storage guard filesystem changed; explicit requalification required")
                fs = os.statvfs(self.path)
                free = fs.f_bavail * fs.f_frsize
                state.update(device=device, free_bytes=free, total_bytes=fs.f_blocks * fs.f_frsize,
                             free_inodes=fs.f_favail, total_inodes=fs.f_files)
                required = self.floor if old.get("healthy") else self.resume_floor
                if free < required or (fs.f_files and fs.f_favail < self.inode_floor):
                    raise StorageUnavailable("storage guard free bytes/inodes below admission floor")
                if not old.get("healthy") and not old.get("initial"):
                    since = old.get("healthy_since")
                    state["healthy_since"] = now if since is None else since
                    if now - state["healthy_since"] < self.recovery_seconds:
                        state.update(healthy=False, reason="storage recovery stability window")
                        self._save(state, old)
                        return state
                # Exercise Docker's actual cp/overlay path, not just host df.
                if not old.get("healthy") or now - old.get("probe_at", 0) >= 30:
                    self.probe()
                    state["probe_at"] = time.time()
                state.update(healthy=True, initial=False, reason="healthy")
            except (OSError, ValueError, subprocess.SubprocessError, StorageUnavailable) as exc:
                state.update(healthy=False, initial=False, healthy_since=None,
                             reason=f"{type(exc).__name__}: {exc}"[-1000:])
            self._save(state, old)
            return state

    def wait(self):
        announced = False
        while True:
            state = self.check()
            if state["healthy"]:
                return
            if not announced:
                print("Waiting for shared storage recovery: " + state["reason"], flush=True)
                announced = True
            time.sleep(self.interval)

    def _docker(self, args):
        result = subprocess.run([os.environ.get("DOCKER_EXE", "docker"), *args],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise StorageUnavailable("storage guard Docker probe failed: " + (result.stderr or result.stdout)[-800:])
        return result.stdout.strip()

    def _docker_probe(self):
        actual = self._docker(["info", "--format", "{{.DockerRootDir}}"])
        if not actual or Path(actual).resolve() != self.path.resolve():
            raise StorageUnavailable("storage guard Docker root does not match watched directory")
        name = "trievo-storage-probe-" + uuid.uuid4().hex[:12]
        owner = hashlib.sha256(str(self.root).encode()).hexdigest()
        cid = None
        with tempfile.TemporaryDirectory(prefix="probe-", dir=self.root) as temporary:
            src = Path(temporary) / "sentinel"
            src.write_text(uuid.uuid4().hex)
            try:
                cid = self._docker(["create", "--name", name, "--network", "none",
                                    "--label", "io.trievo.storage_guard=" + owner,
                                    "--entrypoint", "true", self.probe_image])
                if len(cid) != 64 or any(c not in "0123456789abcdef" for c in cid):
                    cid = None
                    raise StorageUnavailable("storage guard Docker probe returned invalid container id")
                self._docker(["cp", str(src), cid + ":/trievo-storage-sentinel"])
                dest = Path(temporary) / "received"
                self._docker(["cp", cid + ":/trievo-storage-sentinel", str(dest)])
                if dest.read_bytes() != src.read_bytes():
                    raise StorageUnavailable("storage guard Docker probe read-back mismatch")
            finally:
                if cid is None:
                    # A create request can succeed in Docker even if the CLI
                    # response is lost. Recover only our unique labelled probe.
                    try:
                        identity = self._docker(["inspect", "--format",
                            '{{.Id}}|{{index .Config.Labels "io.trievo.storage_guard"}}', name])
                        candidate, label = identity.split("|", 1)
                        if len(candidate) == 64 and label == owner and all(c in "0123456789abcdef" for c in candidate):
                            cid = candidate
                    except (StorageUnavailable, subprocess.SubprocessError, ValueError):
                        pass
                if cid is not None:
                    # Only this uniquely owned exact id, never global prune.
                    self._docker(["rm", cid])


def from_environment():
    return StorageGate.from_environment()
