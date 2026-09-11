import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.run_new_package_matrix import jobs


def _packages(root):
    for index in range(23):
        path = root / f"l1_arena/stream-{index + 1}/stream.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"stream_id": f"l1-{index}", "turns": [{"turn_id": "t1"}]}))
    for index, count in enumerate((10, 40, 50, 60, 60, 70)):
        path = root / f"l2/streams/s{index}/stream.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"stream_id": f"l2-{index}", "ordering": {"turn_order": [f"t{n}" for n in range(count)]}}))
    return root


def test_default_retains_mixed_matrix_and_l2_selects_exact_full_matrix(tmp_path):
    packages = _packages(tmp_path / "packages")
    kwargs = {"l2_conditions": ("reset", "frozen", "evolving")}
    legacy = jobs(tmp_path, tmp_path / "out", packages, **kwargs)
    assert len(legacy) == 123
    selected = jobs(tmp_path, tmp_path / "out", packages, level="l2", **kwargs)
    assert selected == [row for row in legacy if row["level"] == "l2"]
    assert len(selected) == 54
    assert sum(len(row["turns"]) for row in selected) == 2610
    assert {row["level"] for row in selected} == {"l2"}
    assert len({row["id"] for row in selected}) == 54
    assert len(jobs(tmp_path, tmp_path / "out", packages)) == 87


def test_l2_only_does_not_require_l1_package(tmp_path):
    packages = _packages(tmp_path / "packages")
    rows = jobs(tmp_path, tmp_path / "out", packages / "l2", level="l2")
    assert len(rows) == 18


def test_l1_only_does_not_resolve_ambiguous_l2_packages(tmp_path):
    packages = _packages(tmp_path / "packages")
    second = packages / "other/streams/s1/stream.json"
    second.parent.mkdir(parents=True)
    second.write_text("{}")
    rows = jobs(tmp_path, tmp_path / "out", packages, level="l1")
    assert len(rows) == 69
    assert {row["level"] for row in rows} == {"l1"}


def test_invalid_level_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="unknown level"):
        jobs(tmp_path, tmp_path / "out", level="typo")


def test_cli_level_selects_before_gate_without_paid_calls(tmp_path):
    packages = _packages(tmp_path / "packages")
    root = tmp_path / "out"
    # Valid existing reports make the CLI reuse results without launching agents.
    for agent in ("hermes", "skillopt", "trace2skill"):
        path = root / f"l2/reset/{agent}/s01/run/stream/report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"stream_id": "l2-0", "condition": "reset", "turns": [
                {
                    "turn_id": f"t{n}",
                    "final_score": 0,
                    "turn_score": 0,
                    "passed": False,
                    "evaluation_status": "failed",
                    "condition": "reset",
                }
                for n in range(10)
            ]}))
    import os

    result = subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[1] / "tools/run_new_package_matrix.py"),
        "--root", str(root), "--packages", str(packages / "l2"), "--level", "l2",
        "--l2-condition", "reset", "--l2-condition", "frozen", "--l2-condition", "evolving", "--gate",
    ], env={**os.environ, "TRIEVO_API_KEY": "test-not-a-real-key"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    status = json.loads((root / "matrix_status.json").read_text())
    assert status["status"] == "gate_completed"
    assert len(status["jobs"]) == 3
    assert all(row["level"] == "l2" and row["status"] == "completed" for row in status["jobs"])
