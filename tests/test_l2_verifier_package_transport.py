"""Verifier transport excludes build/qualification bulk, retaining runtime data."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from trievo_bench import l2_agent
from trievo_bench.l2_agent import L2AgentStreamRunner, _stage_v5_verifier_package


def fixture_package(root):
    package = root / "package"
    turn = package / "streams/selected/turn1"
    assets = turn / "evaluation_assets"
    assets.mkdir(parents=True)
    (turn.parent / "stream.json").write_text('{"stream_id":"selected"}')
    (turn / "task.json").write_text('{"turn_id":"turn1"}')
    (assets / "verifier_cli.py").write_text(
        'from source_verifier import verify\nimport json\nprint(json.dumps(verify()))\n')
    (assets / "source_verifier.py").write_text(
        'from pathlib import Path\nfrom helper import value\n'
        'def verify():\n'
        '    root=Path(__file__).parent\n'
        '    return {"score": value, "data": (root/"native_tests/fixtures/input.txt").read_text()}\n')
    (assets / "helper.py").write_text('value=1.0\n')
    nested = assets / "native_tests/fixtures"
    nested.mkdir(parents=True)
    (nested / "input.txt").write_text("required native fixture")
    for name in ("fixtures", "native_oracle", "private_oracle_skills"):
        directory = assets / name
        directory.mkdir()
        (directory / "private-gold.bin").write_bytes(b"gold" * 1024)
    for name in ("workspace_seed", "environment"):
        directory = turn / name
        directory.mkdir()
        (directory / "bulky.bin").write_bytes(b"bulk" * 100_000)
    (package / "streams/unrelated/turn1/evaluation_assets").mkdir(parents=True)
    (package / "streams/unrelated/turn1/evaluation_assets/unrelated.bin").write_bytes(b"other" * 100_000)
    (package / "images.tar").write_bytes(b"image" * 100_000)
    (package / "runtime/src").mkdir(parents=True)
    (package / "runtime/src/support.py").write_text("runtime_support=True\n")
    (package / "prompts/rubrics").mkdir(parents=True)
    (package / "prompts/rubrics/general.md").write_text("rubric")
    (package / "manifest.json").write_text("{}")
    return package, turn.parent


def test_selected_verifier_view_excludes_bulk_and_runs_with_real_private_dependencies(tmp_path):
    package, stream = fixture_package(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    _stage_v5_verifier_package(package, stream, stage)
    assets = stage / "streams/selected/turn1/evaluation_assets"
    for name in ("fixtures", "native_oracle", "private_oracle_skills"):
        assert not (assets / name).exists()
    assert not (stage / "streams/unrelated").exists()
    assert not (stage / "streams/selected/turn1/environment").exists()
    assert not (stage / "streams/selected/turn1/workspace_seed").exists()
    assert not (stage / "images.tar").exists()
    assert (stage / "runtime/src/support.py").read_text() == "runtime_support=True\n"
    assert (stage / "prompts/rubrics/general.md").read_text() == "rubric"
    completed = subprocess.run([sys.executable, str(assets / "verifier_cli.py")],
                               check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {"score": 1.0, "data": "required native fixture"}
    assert sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) < 5000
    assert (package / "images.tar").stat().st_size == 500000


def runner_fixture(tmp_path, package, stream, schema="l2_taskstream/1.2"):
    runner = object.__new__(L2AgentStreamRunner)
    runner.package_dir = package
    runner.stream_dir = stream
    runner.options = SimpleNamespace(output_root=tmp_path / "output")
    runner._taskstream_schema_version = schema
    runner._workspace_volume = runner._package_volume = runner._package_staging = None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return runner, workspace


class Mirror:
    def __init__(self, path, role):
        self.host_path, self.role, self.closed = path, role, False
        self.volume_name = "fake-volume-" + role

    def close(self):
        self.closed = True

    def sync_from_host(self):
        pass

    def sync_to_host(self):
        pass


def test_v5_volume_receives_staged_host_path_and_close_removes_staging(tmp_path, monkeypatch):
    package, stream = fixture_package(tmp_path)
    runner, workspace = runner_fixture(tmp_path, package, stream)
    mirrors = []
    def create(path, *, role, **kwargs):
        mirror = Mirror(path, role)
        mirrors.append(mirror)
        return mirror
    monkeypatch.setattr(l2_agent.DockerVolumeMirror, "create", create)
    runner._start_taskstream_volume_transport(workspace)
    stage = runner._package_staging
    assert stage is not None and stage.is_relative_to(runner.options.output_root)
    assert mirrors[1].host_path == stage and stage != package
    assert not (stage / "streams/selected/turn1/workspace_seed").exists()
    runner._close_taskstream_volume_transport()
    assert not stage.exists()
    assert all(mirror.closed for mirror in mirrors)
    assert runner._package_staging is None
    assert package.is_dir()


def test_failed_volume_setup_closes_workspace_and_removes_staging(tmp_path, monkeypatch):
    package, stream = fixture_package(tmp_path)
    runner, workspace = runner_fixture(tmp_path, package, stream)
    mirrors = []
    stages = []
    def create(path, *, role, **kwargs):
        if role == "l2-package":
            stages.append(path)
            raise RuntimeError("failed package transport")
        mirror = Mirror(path, role)
        mirrors.append(mirror)
        return mirror
    monkeypatch.setattr(l2_agent.DockerVolumeMirror, "create", create)
    with pytest.raises(RuntimeError, match="failed package transport"):
        runner._start_taskstream_volume_transport(workspace)
    assert mirrors[0].closed
    assert not stages[0].exists()
    assert runner._workspace_volume is runner._package_volume is runner._package_staging is None


def test_legacy_transport_keeps_original_package_path(tmp_path, monkeypatch):
    package, stream = fixture_package(tmp_path)
    runner, workspace = runner_fixture(tmp_path, package, stream, schema="l2_taskstream/1.1")
    paths = []
    def create(path, *, role, **kwargs):
        paths.append(path)
        return Mirror(path, role)
    monkeypatch.setattr(l2_agent.DockerVolumeMirror, "create", create)
    runner._start_taskstream_volume_transport(workspace)
    assert paths == [workspace, package]
    assert runner._package_staging is None
    runner._close_taskstream_volume_transport()
    assert package.is_dir()


def test_close_failure_still_closes_other_mirror_and_cleans_host_stage(tmp_path, monkeypatch):
    package, stream = fixture_package(tmp_path)
    runner, workspace = runner_fixture(tmp_path, package, stream)
    def create(path, *, role, **kwargs):
        return Mirror(path, role)
    monkeypatch.setattr(l2_agent.DockerVolumeMirror, "create", create)
    runner._start_taskstream_volume_transport(workspace)
    stage = runner._package_staging
    workspace_mirror = runner._workspace_volume
    def fail_close():
        raise RuntimeError("volume cleanup failed")
    runner._package_volume.close = fail_close
    with pytest.raises(RuntimeError, match="volume cleanup failed"):
        runner._close_taskstream_volume_transport()
    assert workspace_mirror.closed
    assert not stage.exists()


def test_staging_stays_outside_package_when_output_is_nested_inside_it(tmp_path, monkeypatch):
    package, stream = fixture_package(tmp_path)
    runner, workspace = runner_fixture(tmp_path, package, stream)
    runner.options.output_root = package / "runs"
    monkeypatch.setattr(l2_agent.DockerVolumeMirror, "create", lambda path, *, role, **kwargs: Mirror(path, role))
    runner._start_taskstream_volume_transport(workspace)
    assert not runner._package_staging.is_relative_to(package)
    runner._close_taskstream_volume_transport()


def test_report_write_failure_still_removes_owned_volumes_and_staging(tmp_path, monkeypatch):
    from test_l2_contract_runtime import _v5_package, _v5_options, _install_fake_runtime
    package = _v5_package(tmp_path, turns=1)
    _install_fake_runtime(monkeypatch)
    mirrors = []
    def create(path, *, role, **kwargs):
        mirror = Mirror(path, role)
        mirrors.append(mirror)
        return mirror
    monkeypatch.setattr(l2_agent.DockerVolumeMirror, "create", create)
    original = l2_agent._write_json
    def failing_report(path, value):
        if path.name == "report.json":
            raise OSError("report disk full")
        return original(path, value)
    monkeypatch.setattr(l2_agent, "_write_json", failing_report)
    runner = L2AgentStreamRunner(_v5_options(package, tmp_path / "output", "evolving"))
    with pytest.raises(OSError, match="report disk full"):
        runner.run()
    assert mirrors and all(mirror.closed for mirror in mirrors)
    assert not mirrors[1].host_path.exists()
    assert runner._package_staging is None
