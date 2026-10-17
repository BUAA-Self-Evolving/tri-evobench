"""Recipe builds become runnable bindings without claiming qualification."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import l2_v5_build_task_images as builder
import l2_v5_materialize_package as materializer
from test_l2_v5_contract import _minimal_v5, _with_pending_recipe, _read_json, _write_json, _rewrite_binding
from trievo_bench.l2_compat import inspect_l2_package, L2SpecError
from trievo_bench.l2_agent import _require_materialized_runtime_profile


RAW_IMAGE = "sha256:" + "c" * 64
SAFE_IMAGE = "sha256:" + "d" * 64


def package_fixture(tmp_path):
    package = _minimal_v5(tmp_path)
    profile = _with_pending_recipe(package)
    turn = package / "streams/s/turn2"
    context = turn / "environment"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM python:3.12\nWORKDIR /root\n")
    tests = turn / "evaluation_assets/native_tests"
    oracle = turn / "evaluation_assets/native_oracle"
    tests.mkdir(parents=True)
    oracle.mkdir()
    (tests / "test.sh").write_text("#!/bin/bash\ncat /root/answer.txt\n")
    (oracle / "solve.sh").write_text("#!/bin/bash\ncp /root/input.txt /root/answer.txt\n")
    recipe = {
        "kind": "dockerfile", "recipe_path": "environment/Dockerfile", "context_path": "environment",
        "recipe_sha256": hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest(),
        "context_sha256": builder.tree_sha256(context),
        "build_contexts": {"trievo_verifier": {"path": "evaluation_assets/native_tests", "sha256": builder.tree_sha256(tests)}},
        "source_environment": {},
        "verifier": {"command": ["bash", "/tests/test.sh"], "mount": "/tests",
                     "assets_path": "evaluation_assets/native_tests", "timeout_seconds": 90,
                     "env": {"UV_INDEX_URL": "https://example.invalid/simple", "UV_OFFLINE": "1"},
                     "reward_path": "/logs/verifier/reward.txt"},
        "oracle": {"command": ["bash", "/solution/solve.sh"], "mount": "/solution",
                   "assets_path": "evaluation_assets/native_oracle", "timeout_seconds": 90, "env": {}}
    }
    task = _read_json(turn / "task.json")
    task.update(environment=recipe, qualification={"admitted": False, "stage": "recipe_ready"},
                runtime_materialization_required=True)
    _write_json(turn / "task.json", task)
    profiles = _read_json(package / "runtime_profiles.json")
    profiles["profiles"][-1]["image_recipe"] = recipe
    _write_json(package / "runtime_profiles.json", profiles)
    _rewrite_binding(package, "runtime_profiles")
    return package, turn, profile["source_id"]


def fake_image_helpers(monkeypatch):
    calls = []
    image = {"Id": RAW_IMAGE, "Config": {"WorkingDir": "/root", "User": "1000"}}
    monkeypatch.setattr(materializer.native, "inspect_image", lambda _: image)
    def finalize(row, inputs, work, timeout, raw, aug, cfg, adaptations, bases):
        calls.append((row, inputs, raw))
        with tarfile.open(work / "safe-rootfs.tar", "w") as archive:
            data = b"public input\n"
            member = tarfile.TarInfo("root/input.txt")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return {"image": {"Id": SAFE_IMAGE, "Config": image["Config"]},
                "runtime_readback": {"python_executable": "/usr/local/bin/python3"},
                "adaptations": adaptations, "scrub": {"removed_paths": ["tests/private-answer"]}}
    def export(result, pool, work):
        archive = work / "safe-image.tar"
        archive.write_bytes(b"fake docker archive")
        return {"image_id": SAFE_IMAGE, "archive_path": str(archive), "archive_bytes": archive.stat().st_size,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    monkeypatch.setattr(materializer.native, "finalize_image", finalize)
    monkeypatch.setattr(materializer.native, "export_safe_image", export)
    def no_docker(*args, **kwargs):
        pytest.fail("unit materialization attempted a subprocess")
    monkeypatch.setattr(materializer.native.subprocess, "run", no_docker)
    monkeypatch.setattr(materializer.native.subprocess, "check_output", no_docker)
    return calls


def test_materialization_binds_actual_image_seed_env_and_invalidates_package(tmp_path, monkeypatch):
    package, turn, sid = package_fixture(tmp_path)
    calls = fake_image_helpers(monkeypatch)
    original_manifest = (package / "manifest.json").read_bytes()
    original_task = _read_json(turn / "task.json")
    original_profiles = _read_json(package / "runtime_profiles.json")
    result = materializer.materialize_package(
        package, [{"source_id": sid, "task_image_digest": RAW_IMAGE,
                   "recipe_sha256": original_task["environment"]["recipe_sha256"],
                   "context_sha256": original_task["environment"]["context_sha256"]}],
        streams=[], sources=[sid], work_root=tmp_path / "work", timeout=90)
    assert len(calls) == 1
    assert calls[0][0]["task_dir"] == turn
    assert calls[0][1]["tests"] == turn / "evaluation_assets/native_tests"
    assert result["qualification_changed"] is False
    assert (package / "manifest.json").read_bytes() != original_manifest
    task = _read_json(turn / "task.json")
    assert task["qualification"] == original_task["qualification"]
    assert task["source_basis"] == original_task["source_basis"]
    assert task["runtime_materialization_required"] is False
    assert task["runtime_materialization"]["status"] == "built_not_qualified"
    assert "write TOKEN" in task["agent_input"]["messages"][0]["content"]
    assert (turn / "workspace_seed/rootfs/root/input.txt").read_text() == "public input\n"
    assert not (turn / "workspace_seed/tests").exists()
    assert (turn / "evaluation_assets/source_verifier.py").is_file()
    profiles = _read_json(package / "runtime_profiles.json")
    assert profiles["profiles"][0] == original_profiles["profiles"][0]
    profile = profiles["profiles"][-1]
    assert profile["task_sandbox_image_digest"] == profile["verifier_image_digest"] == SAFE_IMAGE
    assert "image_build_required" not in profile
    _require_materialized_runtime_profile(profile, "turn2")
    env = profile["native_environment"]
    assert env["user"] == "1000" and env["cwd"] == "/root"
    assert env["verifier"]["env"]["UV_INDEX_URL"] == "https://example.invalid/simple"
    assert env["verifier"]["env"]["UV_OFFLINE"] == "1"
    assert _read_json(turn / "evaluation_assets/native_task.json")["verifier"] == env["verifier"]
    assert inspect_l2_package(package)[0].episode_count == 12
    # Old callers/checkpoints bound to the prior profile catalog are invalid.
    manifest = _read_json(package / "manifest.json")
    assert manifest["runtime_profiles"]["sha256"] == hashlib.sha256((package / "runtime_profiles.json").read_bytes()).hexdigest()
    assert manifest["runtime_profiles"]["sha256"] != json.loads(original_manifest)["runtime_profiles"]["sha256"]


def test_materialization_does_not_accept_missing_observed_image_or_implicit_selection(tmp_path, monkeypatch):
    package, _, sid = package_fixture(tmp_path)
    original = (package / "manifest.json").read_bytes()
    fake_image_helpers(monkeypatch)
    with pytest.raises(ValueError, match="explicit"):
        materializer.materialize_package(package, [], streams=[], sources=[], work_root=tmp_path / "work", timeout=90)
    with pytest.raises(ValueError, match="observed built image"):
        materializer.materialize_package(package, [{"source_id": sid, "task_image_digest": None}],
                                         streams=[], sources=[sid], work_root=tmp_path / "work", timeout=90)
    assert (package / "manifest.json").read_bytes() == original


def test_stale_profile_catalog_fails_before_materialization(tmp_path, monkeypatch):
    package, _, sid = package_fixture(tmp_path)
    path = package / "runtime_profiles.json"
    path.write_text(path.read_text() + "\n")
    calls = fake_image_helpers(monkeypatch)
    with pytest.raises(L2SpecError, match="SHA-256"):
        materializer.pending_targets(package, streams=[], sources=[sid])
    assert not calls


def test_materialization_cli_dry_run_never_builds_or_mutates(tmp_path, monkeypatch, capsys):
    package, _, sid = package_fixture(tmp_path)
    original = (package / "manifest.json").read_bytes()
    fake_image_helpers(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["build", "--package", str(package), "--source", sid,
                                    "--materialize-package", "--dry-run"])
    assert builder.main() == 0
    assert json.loads(capsys.readouterr().out)["materialization"]["executed"] is False
    assert (package / "manifest.json").read_bytes() == original


def test_materialization_cli_requires_bounded_selection_before_build(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["build", "--package", str(tmp_path), "--materialize-package"])
    with pytest.raises(SystemExit) as caught:
        builder.main()
    assert caught.value.code == 2


def built_record(turn, sid):
    recipe = _read_json(turn / "task.json")["environment"]
    return {"source_id": sid, "task_image_digest": RAW_IMAGE,
            "verifier_image_digest": RAW_IMAGE, "task_image": "raw:recipe",
            "recipe_sha256": recipe["recipe_sha256"], "context_sha256": recipe["context_sha256"]}


@pytest.mark.parametrize("mutation", ["source", "profile_recipe", "context", "private_tests", "verifier_env"])
def test_materialization_identity_mutations_fail_before_docker(tmp_path, monkeypatch, mutation):
    package, turn, sid = package_fixture(tmp_path)
    calls = fake_image_helpers(monkeypatch)
    if mutation == "source":
        task = _read_json(turn / "task.json")
        task["source_basis"]["source_task_id"] = "changed-source"
        _write_json(turn / "task.json", task)
    elif mutation == "profile_recipe":
        profiles = _read_json(package / "runtime_profiles.json")
        profiles["profiles"][-1]["image_recipe"]["recipe_sha256"] = "f" * 64
        _write_json(package / "runtime_profiles.json", profiles)
        _rewrite_binding(package, "runtime_profiles")
    elif mutation == "verifier_env":
        task = _read_json(turn / "task.json")
        task["environment"]["verifier"]["env"]["UV_INDEX_URL"] = "https://changed.invalid/simple"
        _write_json(turn / "task.json", task)
    elif mutation == "context":
        (turn / "environment/changed").write_text("mutation")
    else:
        (turn / "evaluation_assets/native_tests/test.sh").write_text("changed tests")
    with pytest.raises(ValueError):
        materializer.pending_targets(package, streams=["s"], sources=[])
    assert not calls


def test_materialization_rejects_built_recipe_identity_mismatch(tmp_path, monkeypatch):
    package, turn, sid = package_fixture(tmp_path)
    calls = fake_image_helpers(monkeypatch)
    built = built_record(turn, sid)
    built["recipe_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="built image recipe identity mismatch"):
        materializer.materialize_package(package, [built], streams=[], sources=[sid],
                                         work_root=tmp_path / "work", timeout=90)
    assert not calls


def test_failed_finalize_cannot_clear_pending_or_change_qualification(tmp_path, monkeypatch):
    package, turn, sid = package_fixture(tmp_path)
    before = {p: p.read_bytes() for p in [package / "manifest.json", package / "runtime_profiles.json", turn / "task.json"]}
    fake_image_helpers(monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("sanitization failed")
    monkeypatch.setattr(materializer.native, "finalize_image", fail)
    with pytest.raises(RuntimeError, match="sanitization failed"):
        materializer.materialize_package(package, [built_record(turn, sid)], streams=[], sources=[sid],
                                         work_root=tmp_path / "work", timeout=90)
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (turn / "evaluation_assets/source_verifier.py").exists()


def test_package_change_during_build_refuses_stale_binding(tmp_path, monkeypatch):
    package, turn, sid = package_fixture(tmp_path)
    original_task = (turn / "task.json").read_bytes()
    original_profiles = (package / "runtime_profiles.json").read_bytes()
    fake_image_helpers(monkeypatch)
    real = materializer.native.finalize_image
    def changing(*args, **kwargs):
        result = real(*args, **kwargs)
        path = package / "manifest.json"
        path.write_text(path.read_text() + "\n")
        return result
    monkeypatch.setattr(materializer.native, "finalize_image", changing)
    with pytest.raises(ValueError, match="package changed"):
        materializer.materialize_package(package, [built_record(turn, sid)], streams=[], sources=[sid],
                                         work_root=tmp_path / "work", timeout=90)
    assert (turn / "task.json").read_bytes() == original_task
    assert (package / "runtime_profiles.json").read_bytes() == original_profiles


def test_materialization_cli_returns_sanitized_digest_and_preserves_bound_profiles(tmp_path, monkeypatch, capsys):
    package, turn, sid = package_fixture(tmp_path)
    fake_image_helpers(monkeypatch)
    bound_profile = _read_json(package / "runtime_profiles.json")["profiles"][0]
    def build(selected, **kwargs):
        assert [record["source_id"] for _, record in selected] == [sid]
        return {"images": [built_record(turn, sid)], "qualification_changed": False}
    monkeypatch.setattr(builder, "build_selected", build)
    monkeypatch.setattr(sys, "argv", ["build", "--package", str(package), "--stream", "s",
                                    "--materialize-package", "--materialization-work", str(tmp_path / "work")])
    assert builder.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["images"][0]["task_image_digest"] == SAFE_IMAGE
    assert output["images"][0]["recipe_image_digest"] == RAW_IMAGE
    assert output["qualification_changed"] is False
    assert _read_json(package / "runtime_profiles.json")["profiles"][0] == bound_profile


def test_materialization_work_inside_package_rejected_before_build(tmp_path, monkeypatch):
    package, _, sid = package_fixture(tmp_path)
    def no_build(*args, **kwargs):
        pytest.fail("invalid work path started a build")
    monkeypatch.setattr(builder, "build_selected", no_build)
    monkeypatch.setattr(sys, "argv", ["build", "--package", str(package), "--source", sid,
                                    "--materialize-package", "--materialization-work", str(package / "private-build")])
    with pytest.raises(SystemExit) as caught:
        builder.main()
    assert caught.value.code == 2


@pytest.mark.parametrize("failing_file", ["runtime_profiles.json", "manifest.json"])
def test_publication_failure_restores_original_package_and_retry_works(tmp_path, monkeypatch, failing_file):
    package, turn, sid = package_fixture(tmp_path)
    fake_image_helpers(monkeypatch)
    seed = turn / "workspace_seed"
    seed.mkdir()
    (seed / "old.txt").write_text("old seed")
    before = {path.relative_to(package): path.read_bytes() for path in package.rglob("*") if path.is_file()}
    real = materializer._write
    def fail_once(path, document):
        if path == package / failing_file:
            raise OSError("injected package write failure")
        return real(path, document)
    monkeypatch.setattr(materializer, "_write", fail_once)
    with pytest.raises(OSError, match="injected package write failure"):
        materializer.materialize_package(package, [built_record(turn, sid)], streams=[], sources=[sid],
                                         work_root=tmp_path / "work", timeout=90)
    assert {path.relative_to(package): path.read_bytes() for path in package.rglob("*") if path.is_file()} == before
    assert materializer.pending_targets(package, streams=[], sources=[sid])
    monkeypatch.setattr(materializer, "_write", real)
    materializer.materialize_package(package, [built_record(turn, sid)], streams=[], sources=[sid],
                                     work_root=tmp_path / "work", timeout=90)
    assert inspect_l2_package(package)[0].episode_count == 12


def test_materialization_updates_source_catalog_runtime_without_admitting_source(tmp_path, monkeypatch):
    package, turn, sid = package_fixture(tmp_path)
    fake_image_helpers(monkeypatch)
    task = _read_json(turn / "task.json")
    row = {"source_id": sid, "source": task["source_basis"],
           "runtime": {"runtime_profile_id": None}, "qualification": task["qualification"]}
    catalog = package / "source_tasks.jsonl"
    catalog.write_text(json.dumps(row) + "\n")
    manifest = _read_json(package / "manifest.json")
    manifest["source_catalog"] = {"path": "source_tasks.jsonl", "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest()}
    _write_json(package / "manifest.json", manifest)
    materializer.materialize_package(package, [built_record(turn, sid)], streams=[], sources=[sid],
                                     work_root=tmp_path / "work", timeout=90)
    updated = json.loads(catalog.read_text())
    assert updated["qualification"] == row["qualification"]
    assert updated["source"] == row["source"]
    assert updated["runtime"]["task_image_digest"] == SAFE_IMAGE
    assert updated["runtime"]["native_environment"]["verifier"]["env"]["UV_OFFLINE"] == "1"
    assert _read_json(package / "manifest.json")["source_catalog"]["sha256"] == hashlib.sha256(catalog.read_bytes()).hexdigest()


def test_materialization_rejects_source_hash_drift_from_catalog(tmp_path, monkeypatch):
    package, turn, sid = package_fixture(tmp_path)
    fake_image_helpers(monkeypatch)
    task = _read_json(turn / "task.json")
    catalog = package / "source_tasks.jsonl"
    catalog.write_text(json.dumps({"source_id": sid, "source": task["source_basis"]}) + "\n")
    manifest = _read_json(package / "manifest.json")
    manifest["source_catalog"] = {"path": "source_tasks.jsonl", "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest()}
    _write_json(package / "manifest.json", manifest)
    task["source_basis"]["source_hash"] = "f" * 64
    _write_json(turn / "task.json", task)
    with pytest.raises(ValueError, match="source differs from package source catalog"):
        materializer.pending_targets(package, streams=[], sources=[sid])
