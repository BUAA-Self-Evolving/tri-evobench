from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


class SandboxError(RuntimeError):
    pass


def _safe_relative(path: str) -> str:
    posix = PurePosixPath(path)
    if not posix.is_absolute():
        posix = PurePosixPath("/workspace") / posix
    if ".." in posix.parts:
        raise SandboxError(f"path escapes sandbox: {path}")
    return str(posix)


class Sandbox:
    def setup(self, setup: dict[str, Any]) -> None:
        host_copy = setup.get("host_copy")
        if host_copy:
            copier = getattr(self, "_copy_host_workspace", None)
            if copier is None:
                raise SandboxError("host_copy setup is supported only by sandboxes with a shared workspace")
            copier(Path(str(host_copy)))
        for item in setup.get("files", []):
            self.write_file(item["path"], str(item.get("content", "")))
        for path in setup.get("remove", []):
            self.remove(path)

    def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError

    def read_file(self, path: str) -> str:
        raise NotImplementedError

    def remove(self, path: str) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    def export_workspace(self) -> Path:
        raise NotImplementedError

    def workspace_path(self) -> Path:
        """Return the live host path shared with an external baseline runtime."""
        raise NotImplementedError

    def cleanup_export(self, path: Path) -> None:
        pass

    def close(self) -> None:
        pass


class LocalSandbox(Sandbox):
    """Portable backend used for fast smoke tests; semantics mirror Ubuntu."""

    def __init__(self, root: str | None = None) -> None:
        self._owned = root is None
        self.root = Path(root or tempfile.mkdtemp(prefix="trievo-sandbox-"))
        (self.root / "workspace").mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        virtual = _safe_relative(path).lstrip("/")
        candidate = (self.root / virtual).resolve()
        if self.root.resolve() not in candidate.parents and candidate != self.root.resolve():
            raise SandboxError(f"path escapes sandbox: {path}")
        return candidate

    def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _copy_host_workspace(self, source: Path) -> None:
        if not source.is_dir():
            raise SandboxError(f"host_copy source does not exist: {source}")
        destination = self.root / "workspace"
        for item in source.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    def read_file(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def remove(self, path: str) -> None:
        target = self._resolve(path)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def snapshot(self) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for target in sorted((self.root / "workspace").rglob("*")):
            if target.is_file():
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                files.append({"path": "/" + str(target.relative_to(self.root)).replace(os.sep, "/"), "size": target.stat().st_size, "sha256": digest})
        return {"backend": "local", "files": files}

    def export_workspace(self) -> Path:
        return self.root / "workspace"

    def workspace_path(self) -> Path:
        return self.root / "workspace"

    def close(self) -> None:
        if self._owned:
            shutil.rmtree(self.root, ignore_errors=True)


class DockerSandbox(Sandbox):
    """Ubuntu container backend. One container is shared by all turns in one Flow."""

    def __init__(self, image: str = "ubuntu:22.04", name: str | None = None) -> None:
        self.image = image
        self.name = name or f"trievo-{uuid.uuid4().hex[:12]}"
        self._host_workspace = Path(tempfile.mkdtemp(prefix="trievo-workspace-"))
        try:
            command = ["docker", "run", "-d", "--name", self.name]
            if hasattr(os, "getuid") and hasattr(os, "getgid"):
                command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
            command.extend([
                "-v", f"{self._host_workspace}:/workspace",
                image, "sleep", "infinity",
            ])
            self._run(command)
        except Exception:
            shutil.rmtree(self._host_workspace, ignore_errors=True)
            raise

    def _run(self, command: list[str], input_text: str | None = None) -> str:
        try:
            result = subprocess.run(command, input=input_text, text=True, encoding="utf-8", errors="replace", capture_output=True, check=True, timeout=60)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"sandbox command failed: {' '.join(command)}") from exc
        return result.stdout

    def _run_exec(self, command: list[str]) -> str:
        return self._run(["docker", "exec", self.name, *command])

    def _copy_host_workspace(self, source: Path) -> None:
        if not source.is_dir():
            raise SandboxError(f"host_copy source does not exist: {source}")
        # The bind-mounted workspace is already visible to both the host and
        # container.  Copying here avoids constructing one enormous `docker
        # exec ... base64` command for Arena's documentation-heavy fixtures.
        for item in source.iterdir():
            target = self._host_workspace / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    def write_file(self, path: str, content: str) -> None:
        virtual = _safe_relative(path)
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        self._run_exec(["sh", "-lc", f"mkdir -p $(dirname {json.dumps(virtual)}) && echo {json.dumps(payload)} | base64 -d > {json.dumps(virtual)}"])

    def read_file(self, path: str) -> str:
        return self._run_exec(["cat", _safe_relative(path)])

    def remove(self, path: str) -> None:
        self._run_exec(["rm", "-rf", _safe_relative(path)])

    def snapshot(self) -> dict[str, Any]:
        raw = self._run_exec(["sh", "-lc", "find /workspace -type f -printf '%p\t%s\t' -exec sha256sum {} \; | sed 's/  /\t/'"])
        files: list[dict[str, Any]] = []
        for line in raw.splitlines():
            parts = line.split("	")
            if len(parts) >= 3:
                files.append({"path": parts[0], "size": int(parts[1]), "sha256": parts[2].split()[0]})
        return {"backend": "docker", "image": self.image, "files": files}

    def export_workspace(self) -> Path:
        exported = Path(tempfile.mkdtemp(prefix="trievo-export-"))
        self._run(["docker", "cp", f"{self.name}:/workspace/.", str(exported)])
        return exported

    def workspace_path(self) -> Path:
        return self._host_workspace

    def cleanup_export(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        shutil.rmtree(self._host_workspace, ignore_errors=True)


def create_sandbox(config: dict[str, Any], backend_override: str | None = None, image_override: str | None = None) -> Sandbox:
    backend = backend_override or config.get("backend", "local")
    if backend == "local":
        return LocalSandbox(config.get("root"))
    if backend == "docker":
        return DockerSandbox(image_override or config.get("image", "ubuntu:22.04"))
    raise SandboxError(f"unsupported sandbox backend: {backend}")
