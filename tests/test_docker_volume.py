from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from trievo_bench import docker_volume
from trievo_bench import sandbox as sandbox_module
from trievo_bench.baselines import process as process_module
from trievo_bench.baselines.base import FlowContext
from trievo_bench.baselines.manifest import BaselineManifest
from trievo_bench.baselines.process import BaselineProtocolError, ProcessBaselineRuntime
from trievo_bench.docker_volume import (
    DockerVolumeError,
    DockerVolumeMirror,
    docker_volume_transport_enabled,
)
from trievo_bench.model import TokenBudget
from trievo_bench.sandbox import DockerSandbox


def test_volume_transport_is_explicit_and_fail_closed():
    assert not docker_volume_transport_enabled({})
    assert docker_volume_transport_enabled(
        {"TRIEVO_DOCKER_VOLUME_TRANSPORT": "required"}
    )
    with pytest.raises(DockerVolumeError, match="must be one of") as exc_info:
        docker_volume_transport_enabled(
            {"TRIEVO_DOCKER_VOLUME_TRANSPORT": "sometimes"}
        )
    for accepted in ("''", "'0'", "'1'", "'disabled'", "'off'", "'required'", "'yes'"):
        assert accepted in str(exc_info.value)


def test_volume_mirror_uses_daemon_owned_volume_and_docker_cp(tmp_path, monkeypatch):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "seed.txt").write_text("seed", encoding="utf-8")
    calls = []

    def run(args, *, check=True):
        calls.append((list(args), check))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(docker_volume, "_run_docker", run)
    monkeypatch.setattr(docker_volume.uuid, "uuid4", lambda: type("U", (), {"hex": "a" * 32})())

    mirror = DockerVolumeMirror.create(source, role="l2-workspace")

    assert mirror.volume_name == "trievo-l2-workspace-aaaaaaaaaaaa"
    assert mirror.mount("/workspace") == (
        "trievo-l2-workspace-aaaaaaaaaaaa:/workspace"
    )
    assert mirror.mount_spec("/workspace", read_only=True) == (
        "type=volume,src=trievo-l2-workspace-aaaaaaaaaaaa,dst=/workspace,readonly"
    )
    create = calls[0][0]
    carrier = calls[1][0]
    copy = next(args for args, _ in calls if args[:1] == ["cp"])
    assert create[:2] == ["volume", "create"]
    assert "io.trievo.role=l2-workspace" in create
    assert "--mount" in carrier
    assert all(str(source) not in item for item in carrier)
    assert copy == [
        "cp",
        f"{source}/.",
        "trievo-l2-workspace-carrier-aaaaaaaaaaaa:/volume",
    ]

    mirror.close()
    assert calls[-2][0][:2] == ["rm", "-f"]
    assert calls[-1][0][:3] == ["volume", "rm", "-f"]


def test_l1_docker_sandbox_shares_daemon_volume_with_baseline(tmp_path, monkeypatch):
    calls = []

    class Mirror:
        volume_name = "l1-workspace-volume"

        def __init__(self, host_path):
            self.host_path = host_path
            self.pushes = 0
            self.pulls = 0
            self.closed = False

        def mount_spec(self, target):
            return f"type=volume,src={self.volume_name},dst={target}"

        def sync_from_host(self):
            self.pushes += 1

        def sync_to_host(self):
            self.pulls += 1

        def close(self):
            self.closed = True

    mirrors = []

    def create(host_path, **kwargs):
        assert kwargs == {"role": "l1-workspace", "helper_image": "sandbox:image"}
        mirror = Mirror(host_path)
        mirrors.append(mirror)
        return mirror

    monkeypatch.setenv("TRIEVO_DOCKER_VOLUME_TRANSPORT", "required")
    monkeypatch.setattr(sandbox_module.DockerVolumeMirror, "create", create)
    monkeypatch.setattr(DockerSandbox, "_run", lambda self, command, input_text=None: calls.append(command) or "")
    monkeypatch.setattr(
        sandbox_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    sandbox = DockerSandbox(image="sandbox:image", name="l1-sandbox")
    try:
        assert sandbox.workspace_mount() == "l1-workspace-volume"
        assert "type=volume,src=l1-workspace-volume,dst=/workspace" in calls[0]
        source = tmp_path / "fixture"
        source.mkdir()
        (source / "seed.txt").write_text("seed", encoding="utf-8")
        sandbox.setup({"host_copy": str(source)})
        sandbox.write_file("/workspace/result.txt", "done")
        assert (sandbox.workspace_path() / "result.txt").read_text(encoding="utf-8") == "done"
        assert mirrors[0].pushes == 1
    finally:
        sandbox.close()
    assert mirrors[0].pulls == 1
    assert mirrors[0].closed


def test_volume_pull_rolls_back_host_tree_when_atomic_swap_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "original.txt").write_text("keep", encoding="utf-8")

    def run(args, *, check=True):
        if args[:1] == ["cp"] and str(args[1]).endswith(":/volume/."):
            staged = Path(args[2])
            (staged / "replacement.txt").write_text("new", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(docker_volume, "_run_docker", run)
    mirror = DockerVolumeMirror.create(source, role="l2-workspace")
    original_rename = Path.rename

    def fail_staged_swap(path, target):
        if path.name.startswith(".workspace-docker-pull-") and Path(target) == source:
            raise OSError("injected atomic swap failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_staged_swap)

    with pytest.raises(OSError, match="injected atomic swap failure"):
        mirror.sync_to_host()

    assert (source / "original.txt").read_text(encoding="utf-8") == "keep"
    assert not (source / "replacement.txt").exists()
    mirror.close()


def test_volume_mount_rejects_relative_container_path(tmp_path, monkeypatch):
    source = tmp_path / "workspace"
    source.mkdir()
    monkeypatch.setattr(
        docker_volume,
        "_run_docker",
        lambda args, *, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )
    mirror = DockerVolumeMirror.create(source, role="test")
    with pytest.raises(DockerVolumeError, match="must be absolute"):
        mirror.mount("workspace")
    mirror.close()


def test_process_runtime_renders_workspace_and_state_volume_mounts(tmp_path, monkeypatch):
    mirrors = []

    class Mirror:
        volume_name = "state-volume"

        def __init__(self, host_path):
            self.host_path = host_path
            self.pulls = 0
            self.closed = False

        def sync_to_host(self):
            self.pulls += 1

        def sync_from_host(self):
            return None

        def close(self):
            self.closed = True

    def create(host_path, **_kwargs):
        mirror = Mirror(host_path)
        mirrors.append(mirror)
        return mirror

    commands = []

    class Transport:
        def __init__(self, command, **_kwargs):
            commands.append(command)
            self.process = SimpleNamespace(poll=lambda: None)

        def request(self, method, _params, timeout=None):
            if method == "initialize":
                return {"protocol_version": "1.0"}
            return {}

        def close(self):
            return None

    monkeypatch.setattr(process_module.DockerVolumeMirror, "create", create)
    monkeypatch.setattr(process_module, "NdjsonProcess", Transport)
    monkeypatch.setattr(process_module, "_resolve_executable", lambda value: value)
    manifest = BaselineManifest.from_mapping(
        tmp_path / "docker.json",
        {
            "schema_version": "1.0",
            "id": "fake-docker",
            "adapter": {
                "type": "process",
                "command": [
                    "docker",
                    "run",
                    "-i",
                    "-v",
                    "{workspace_mount}:/workspace",
                    "-v",
                    "{state_mount}:/state",
                    "fake:image",
                ],
            },
            "workspace": {"adapter_path": "/workspace"},
            "state": {"adapter_path": "/state", "paths": ["**/*"]},
        },
    )
    workspace = tmp_path / "workspace"
    output = tmp_path / "output"
    workspace.mkdir()
    output.mkdir()
    runtime = ProcessBaselineRuntime(
        manifest,
        budget=TokenBudget(limit=100),
        repo_root=tmp_path,
    )

    runtime.start_flow(
        FlowContext(
            "flow",
            workspace,
            output,
            workspace_mount="workspace-volume",
        )
    )
    runtime.end_flow()

    assert "type=volume,src=workspace-volume,dst=/workspace" in commands[0]
    assert "type=volume,src=state-volume,dst=/state" in commands[0]
    assert mirrors[0].pulls >= 1
    assert mirrors[0].closed
    assert runtime.metadata()["docker_volume_transport"] is True


def test_process_runtime_cleans_state_directory_when_seed_fails(tmp_path, monkeypatch):
    state_dir = tmp_path / "state-tmp"

    def make_state_dir(*_args, **_kwargs):
        state_dir.mkdir()
        return str(state_dir)

    monkeypatch.setattr(process_module.tempfile, "mkdtemp", make_state_dir)
    manifest = BaselineManifest.from_mapping(
        tmp_path / "missing-seed.json",
        {
            "schema_version": "1.0",
            "id": "fake",
            "adapter": {"type": "process", "command": ["python", "adapter.py"]},
            "workspace": {"adapter_path": "{workspace}"},
            "state": {
                "adapter_path": "{state_dir}",
                "seed_from": str(tmp_path / "missing-seed"),
                "paths": ["**/*"],
            },
        },
    )
    workspace = tmp_path / "workspace"
    output = tmp_path / "output"
    workspace.mkdir()
    output.mkdir()
    runtime = ProcessBaselineRuntime(
        manifest,
        budget=TokenBudget(limit=100),
        repo_root=tmp_path,
    )

    with pytest.raises(BaselineProtocolError, match="seed directory not found"):
        runtime.start_flow(FlowContext("flow", workspace, output))

    assert runtime._state_dir is None
    assert not state_dir.exists()


def test_process_runtime_releases_transport_and_bridge_when_state_pull_fails(tmp_path):
    manifest = BaselineManifest.from_mapping(
        tmp_path / "runtime.json",
        {
            "schema_version": "1.0",
            "id": "fake",
            "adapter": {"type": "process", "command": ["docker", "run", "fake:image"]},
            "workspace": {"adapter_path": "/workspace"},
            "state": {"adapter_path": "/state", "paths": ["**/*"]},
        },
    )
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    output = tmp_path / "output"
    state_dir.mkdir()
    workspace.mkdir()
    output.mkdir()

    class Volume:
        def __init__(self):
            self.closed = False

        def sync_to_host(self):
            raise DockerVolumeError("injected state pull failure")

        def close(self):
            self.closed = True

    class Bridge:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Transport:
        process = SimpleNamespace(poll=lambda: 1)

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    volume = Volume()
    bridge = Bridge()
    transport = Transport()
    runtime = ProcessBaselineRuntime(
        manifest,
        budget=TokenBudget(limit=100),
        repo_root=tmp_path,
    )
    runtime._state_dir = state_dir
    runtime._flow = FlowContext("flow", workspace, output)
    runtime._state_volume = volume
    runtime._docker_proxy_bridge = bridge
    runtime._transport = transport

    with pytest.raises(DockerVolumeError, match="injected state pull failure"):
        runtime.end_flow()

    assert transport.closed
    assert bridge.closed
    assert volume.closed
    assert runtime._state_dir is None
    assert not state_dir.exists()
