import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import l2_v5_build_task_images as builder


def make_recipe(root, sid="skillsbench:example"):
    task = root / sid.replace(":", "__")
    context = task / "environment"
    context.mkdir(parents=True)
    recipe = context / "Dockerfile"
    recipe.write_text("FROM busybox:stable\n")
    record = {"source_id": sid, "qualification": {"admitted": False, "stage": "recipe_ready"},
              "environment": {"kind": "dockerfile", "recipe_path": "environment/Dockerfile",
                              "context_path": "environment",
                              "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
                              "context_sha256": builder.tree_sha256(context)}}
    (task / "pool_task.json").write_text(json.dumps(record))
    return task, record


def test_recipe_build_returns_observed_image_without_changing_qualification(tmp_path):
    task, record = make_recipe(tmp_path)
    original = (task / "pool_task.json").read_bytes()
    digest = "sha256:" + "a" * 64
    with patch.object(builder.subprocess, "run") as run, \
            patch.object(builder.subprocess, "check_output", return_value=digest + "\n"):
        result = builder.build_selected([(task, record)], build_args=[], timeout=90)
    assert run.call_args.args[0][:2] == ["docker", "build"]
    assert result["images"][0]["task_image_digest"] == digest
    assert (task / "pool_task.json").read_bytes() == original
    assert result["qualification_changed"] is False


def test_changed_recipe_or_context_fails_before_build(tmp_path):
    task, record = make_recipe(tmp_path)
    (task / "environment/unrecorded.txt").write_text("changed")
    with patch.object(builder.subprocess, "run") as run, pytest.raises(ValueError, match="context hash"):
        builder.build_selected([(task, record)], build_args=[], timeout=90)
    run.assert_not_called()
    (task / "environment/Dockerfile").write_text("FROM changed\n")
    with pytest.raises(ValueError, match="Dockerfile hash"):
        builder.recipe_paths(task, record["environment"])


def test_stream_selection_uses_exact_source_identity(tmp_path):
    pool = tmp_path / "pool"
    make_recipe(pool)
    make_recipe(pool, "skillsbench:other")
    package = tmp_path / "package"
    for stream, source in (("intra_a", "example"), ("intra_b", "other")):
        turn = package / "streams" / stream / "turn1"
        turn.mkdir(parents=True)
        (turn / "task.json").write_text(json.dumps({"stream_id": stream,
            "source_basis": {"benchmark_id": "skillsbench", "source_task_id": source}}))
    selected = builder.select_recipes(package, pool, ["intra_a"])
    assert [r["source_id"] for _, r in selected] == ["skillsbench:example"]
    with pytest.raises(ValueError, match="sources not selected"):
        builder.select_recipes(package, pool, ["intra_a"], ["skillsbench:other"])
    with pytest.raises(ValueError, match="unknown streams"):
        builder.select_recipes(package, pool, ["missing"])


def test_dry_run_never_builds_and_deduplicated_context_builds_once(tmp_path):
    first = make_recipe(tmp_path, "skillsbench:first")
    second = make_recipe(tmp_path, "skillsbench:second")
    with patch.object(builder.subprocess, "run") as run:
        result = builder.build_selected([first, second], build_args=[], timeout=90, dry_run=True)
    run.assert_not_called()
    assert all(row["task_image_digest"] is None for row in result["images"])
    with patch.object(builder.subprocess, "run") as run, \
            patch.object(builder.subprocess, "check_output", return_value="sha256:" + "b" * 64):
        builder.build_selected([first, second], build_args=[], timeout=90)
    assert run.call_count == 1


def test_named_private_context_is_separate_and_hash_checked(tmp_path):
    task, record = make_recipe(tmp_path)
    private = task / "evaluation_assets/native_tests"
    private.mkdir(parents=True)
    (private / "test.sh").write_text("private test")
    record["environment"]["build_contexts"] = {"trievo_verifier": {
        "path": "evaluation_assets/native_tests", "sha256": builder.tree_sha256(private)}}
    result = builder.build_selected([(task, record)], build_args=[], timeout=90, dry_run=True)
    command = result["images"][0]["command"]
    assert "trievo_verifier=" + str(private) in command
    assert command[-1] == str(task / "environment")
    (private / "test.sh").write_text("changed")
    with pytest.raises(ValueError, match="named build context hash"):
        builder.build_selected([(task, record)], build_args=[], timeout=90, dry_run=True)


def test_shared_recipe_builds_once_for_two_sources_and_cannot_escape_pool(tmp_path):
    context = tmp_path / "environment"
    context.mkdir()
    recipe = context / "Dockerfile"
    recipe.write_text("FROM busybox:stable\n")
    selected = []
    for sid in ("sr_agents:first", "sr_agents:second"):
        task = tmp_path / sid.replace(":", "__")
        task.mkdir()
        record = {"source_id": sid, "environment": {
            "kind": "shared_dockerfile", "context_path": "../environment",
            "recipe_path": "../environment/Dockerfile",
            "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
            "context_sha256": builder.tree_sha256(context)}}
        (task / "pool_task.json").write_text(json.dumps(record))
        selected.append((task, record))
    assert len(builder.select_recipes(tmp_path)) == 2
    with patch.object(builder.subprocess, "run") as run, \
            patch.object(builder.subprocess, "check_output", return_value="sha256:" + "c" * 64):
        result = builder.build_selected(selected, build_args=[], timeout=90)
    assert run.call_count == 1
    assert len({row["task_image_digest"] for row in result["images"]}) == 1
    selected[0][1]["environment"]["context_path"] = "../../outside"
    with pytest.raises(ValueError, match="escapes"):
        builder.recipe_paths(*[selected[0][0], selected[0][1]["environment"]])
