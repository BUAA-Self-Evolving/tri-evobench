"""Opt-in native task paths; legacy V5 profiles do not use this module."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

SCHEMA_VERSION = "l2_native_environment/1.0"
# Never let task mappings replace tools, private evaluation mounts or Workspace.
_RESERVED = ("/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/opt", "/proc",
             "/sys", "/dev", "/workspace", "/tests", "/verifier", "/solution",
             "/oracle", "/logs")


def _absolute(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValueError("native path must be absolute")
    if ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value:
        raise ValueError("native path must be canonical")
    return value


def validate_native_environment(value: Any) -> dict[str, Any]:
    """Validate a native manifest without guessing unsupported launch behavior."""
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported native_environment schema_version")
    allowed = {"schema_version", "cwd", "path_mappings", "python_executable", "entrypoint",
               "verifier", "oracle", "adaptations", "image", "user"}
    if set(value) - allowed:
        raise ValueError("unknown native_environment fields: " + str(sorted(set(value) - allowed)))
    _absolute(value.get("cwd"))
    for key, default in (("python_executable", "python"), ("entrypoint", "/bin/sh")):
        command = value.get(key, default)
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ValueError(f"invalid native {key}")
    user = value.get("user", "")
    if not isinstance(user, str) or "\x00" in user or len(user.split(":")) > 2:
        raise ValueError("invalid native user")
    mappings = value.get("path_mappings")
    if not isinstance(mappings, list):
        raise ValueError("native path_mappings must be a list")
    destinations: list[PurePosixPath] = []
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) != {"native_path", "workspace_relative_path"}:
            raise ValueError("invalid native path mapping fields")
        native = _absolute(mapping["native_path"])
        path = PurePosixPath(native)
        if native in {"/", "/root", "/home", "/app", "/tmp", "/var"}:
            raise ValueError("cannot map an entire native root")
        if any(native == root or native.startswith(root + "/") for root in _RESERVED):
            raise ValueError("native mapping shadows a reserved path")
        if any(part in {".venv", ".local", ".cache", ".cargo", ".rustup", ".elan",
                            "skills", ".skills", ".claude", ".codex"} for part in path.parts):
            raise ValueError("native mapping shadows tools or skills")
        if any(path == old or path in old.parents or old in path.parents for old in destinations):
            raise ValueError("overlapping native mappings")
        destinations.append(path)
        relative = mapping["workspace_relative_path"]
        if (not isinstance(relative, str) or not relative or relative == "." or "\x00" in relative
                or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
                or str(PurePosixPath(relative)) != relative):
            raise ValueError("native mapping escapes task directory")
    return value


def mapping_script(environment: dict[str, Any], task_dir: str) -> str:
    """Python bootstrap shared by task startup and offline qualification.

    The builder must materialize public image assets in the task seed first.
    Only declared aliases are replaced; toolchain directories stay in the image.
    """
    validate_native_environment(environment)
    _absolute(task_dir)
    return f'''
import json, shutil
from pathlib import Path
root = Path({task_dir!r})
root.mkdir(parents=True, exist_ok=True)
root = root.resolve()
for item in json.loads({json.dumps(environment['path_mappings'])!r}):
    target = root / item['workspace_relative_path']
    if root not in target.resolve().parents:
        raise ValueError('native mapping target escapes task directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    alias = Path(item['native_path'])
    if any(parent.is_symlink() for parent in alias.parents):
        raise ValueError('native mapping parent must not be a symlink')
    if alias.is_symlink() and alias.resolve() == target.resolve():
        continue
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink() or alias.is_file():
        alias.unlink()
    elif alias.exists():
        shutil.rmtree(alias)
    alias.symlink_to(target)
# This is trusted startup, before tools run as the image's original USER.
import os, pwd, grp
user = {environment.get('user', '')!r}
if user:
    username, _, group = user.partition(':')
    try:
        account = pwd.getpwuid(int(username)) if username.isdigit() else pwd.getpwnam(username)
    except KeyError:
        account = None
    uid = int(username) if username.isdigit() else account.pw_uid
    gid = (int(group) if group.isdigit() else grp.getgrnam(group).gr_gid) if group else (account.pw_gid if account else 0)
    for item in [root, *root.rglob('*')]:
        os.chown(item, uid, gid, follow_symlinks=False)
'''
