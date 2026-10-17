"""Native filesystem tools share the terminal's cwd and alias interpretation."""
from pathlib import Path

import pytest

from baseline.adapters.skillopt.bridge import _execute_dsml_tool


def runtime():
    return {"environment_version": "l2_environment/1.0", "cwd": "/root", "task_directory": "/workspace/tasks/turn1",
            "path_mappings": [{"native_path": "/root/data", "workspace_relative_path": "data"}]}


def execute(workspace, operation, **arguments):
    return _execute_dsml_tool({"name": "filesystem", "arguments": {"operation": operation, **arguments}},
                              workspace, {}, task_runtime=runtime())


def test_relative_native_files_resolve_against_native_cwd(tmp_path):
    execute(tmp_path, "write_file", path="data/sub/../result.txt", content="learned")
    assert (tmp_path / "tasks/turn1/data/result.txt").read_text() == "learned"
    assert execute(tmp_path, "read_file", path="/root/data/result.txt") == "learned"
    assert execute(tmp_path, "read_file", path="data/result.txt") == "learned"
    assert not (tmp_path / "data").exists()
    with pytest.raises(ValueError, match="outside declared"):
        execute(tmp_path, "write_file", path="/root/data/../secret", content="bad")


def test_native_glob_and_grep_honor_search_directory(tmp_path):
    execute(tmp_path, "write_file", path="data/a.txt", content="needle current")
    prior = tmp_path / "tasks/turn0/b.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("needle prior")
    assert execute(tmp_path, "glob", path="data", pattern="*.txt") == "tasks/turn1/data/a.txt"
    grep = execute(tmp_path, "grep", path="data", pattern="needle")
    assert "needle current" in grep and "needle prior" not in grep
    assert "needle current" in execute(tmp_path, "grep", path="data/a.txt", pattern="needle")
    with pytest.raises(ValueError, match="pattern escapes"):
        execute(tmp_path, "glob", path="data", pattern="../../../*")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("needle private")
    (tmp_path / "tasks/turn1/data/escape.txt").symlink_to(outside)
    assert "escape.txt" not in execute(tmp_path, "glob", path="data", pattern="*.txt")
    assert "needle private" not in execute(tmp_path, "grep", path="data", pattern="needle")
