from __future__ import annotations

import pytest

from trievo_bench import docker_proxy
from trievo_bench.baselines import process as process_module
from trievo_bench.baselines.base import FlowContext
from trievo_bench.baselines.manifest import BaselineManifest
from trievo_bench.baselines.process import ProcessBaselineRuntime
from trievo_bench.docker_proxy import DockerProxyBridge, DockerProxyBridgeError
from trievo_bench.model import TokenBudget


class _Process:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_bridge_is_explicitly_disabled_by_default():
    bridge = DockerProxyBridge.from_environment(
        {"HTTPS_PROXY": "http://127.0.0.1:7890"}
    )
    assert not bridge.active
    assert bridge.environment == {}


def test_non_loopback_proxy_is_passed_through_without_a_forwarder(monkeypatch):
    monkeypatch.setattr(
        docker_proxy,
        "_default_route_source",
        lambda: (_ for _ in ()).throw(AssertionError("route lookup is unnecessary")),
    )
    bridge = DockerProxyBridge.from_environment(
        {
            "TRIEVO_DOCKER_PROXY_BRIDGE": "auto",
            "HTTPS_PROXY": "http://proxy.example:8080",
        }
    )
    assert not bridge.active
    assert bridge.environment["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert bridge.environment["https_proxy"] == "http://proxy.example:8080"
    assert not bridge.environment["NO_PROXY"].endswith(",")


def test_loopback_proxy_starts_scoped_forwarder(monkeypatch):
    process = _Process()
    calls = []
    monkeypatch.setattr(docker_proxy, "_default_route_source", lambda: "10.2.3.4")
    monkeypatch.setattr(docker_proxy, "_allocate_port", lambda _host: 23456)
    monkeypatch.setattr(docker_proxy, "_wait_until_listening", lambda *_args: None)

    def start(**kwargs):
        calls.append(kwargs)
        return process

    monkeypatch.setattr(docker_proxy, "_start_socat_forwarder", start)
    bridge = DockerProxyBridge.from_environment(
        {
            "TRIEVO_DOCKER_PROXY_BRIDGE": "required",
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "internal.example",
            "TRIEVO_DOCKER_PROXY_RANGE": "172.20.0.0/16",
        }
    )

    assert bridge.active
    assert len(calls) == 1
    assert calls[0] == {
        "listen_host": "10.2.3.4",
        "listen_port": 23456,
        "allow_range": "172.20.0.0/16",
        "target_host": "127.0.0.1",
        "target_port": 7890,
    }
    assert bridge.environment["HTTP_PROXY"] == "http://10.2.3.4:23456"
    assert bridge.environment["HTTPS_PROXY"] == "http://10.2.3.4:23456"
    assert bridge.environment["NO_PROXY"] == (
        "internal.example,localhost,127.0.0.1,10.2.3.4"
    )

    bridge.close()
    assert process.terminated
    assert not bridge.active


def test_bridge_rejects_credentials_and_conflicting_case_variants():
    with pytest.raises(DockerProxyBridgeError, match="authenticated"):
        DockerProxyBridge.from_environment(
            {
                "TRIEVO_DOCKER_PROXY_BRIDGE": "auto",
                "HTTPS_PROXY": "http://user:secret@127.0.0.1:7890",
            }
        )
    with pytest.raises(DockerProxyBridgeError, match="conflicting"):
        DockerProxyBridge.from_environment(
            {
                "TRIEVO_DOCKER_PROXY_BRIDGE": "auto",
                "HTTP_PROXY": "http://127.0.0.1:7890",
                "http_proxy": "http://127.0.0.1:7891",
            }
        )


def test_required_bridge_rejects_missing_proxy():
    with pytest.raises(DockerProxyBridgeError, match="is absent"):
        DockerProxyBridge.from_environment(
            {"TRIEVO_DOCKER_PROXY_BRIDGE": "required"}
        )


def test_loopback_bridge_requires_an_explicit_docker_source_range(monkeypatch):
    monkeypatch.setattr(docker_proxy, "_default_route_source", lambda: "10.2.3.4")
    with pytest.raises(DockerProxyBridgeError, match="observed Docker client source"):
        DockerProxyBridge.from_environment(
            {
                "TRIEVO_DOCKER_PROXY_BRIDGE": "required",
                "HTTPS_PROXY": "http://127.0.0.1:7890",
            }
        )


def test_is_docker_command_accepts_resolved_executable():
    assert docker_proxy.is_docker_command(["/usr/bin/docker", "run"])
    assert docker_proxy.is_docker_command(["docker.exe", "run"])
    assert not docker_proxy.is_docker_command(["python", "adapter.py"])


def test_process_runtime_closes_bridge_when_adapter_initialization_fails(
    tmp_path,
    monkeypatch,
):
    class Bridge:
        active = True
        environment = {"HTTPS_PROXY": "http://10.2.3.4:23456"}

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    bridge = Bridge()

    def fail_transport(*_args, **_kwargs):
        raise OSError("injected adapter startup failure")

    monkeypatch.setattr(
        process_module.DockerProxyBridge,
        "from_environment",
        lambda: bridge,
    )
    monkeypatch.setattr(process_module, "NdjsonProcess", fail_transport)
    monkeypatch.setattr(process_module, "_resolve_executable", lambda value: value)
    manifest = BaselineManifest.from_mapping(
        tmp_path / "docker.json",
        {
            "schema_version": "1.0",
            "id": "fake-docker",
            "adapter": {
                "type": "process",
                "command": ["docker", "run", "fake:image"],
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

    with pytest.raises(OSError, match="injected adapter startup failure"):
        runtime.start_flow(FlowContext("flow", workspace, output))

    assert bridge.closed
    assert runtime._docker_proxy_bridge is None
    assert runtime._state_dir is None
