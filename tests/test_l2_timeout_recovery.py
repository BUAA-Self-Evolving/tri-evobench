from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tests.test_l2_contract_runtime import _package, _write_json
from tools.recover_l2_taskstream_timeout import admit_timeout


def _failed_run(root: Path) -> tuple[Path, Path, Path]:
    package = _package(root, turns=2)
    stream_manifest = package / "streams" / "stream-a" / "stream.json"
    job_output = root / "out" / "l2" / "reset" / "hermes" / "s01"
    run_dir = job_output / "stream-a" / "20260907T000000Z-deadbeef"
    run_dir.mkdir(parents=True)
    turn1_checkpoint = job_output / "_checkpoints" / "key" / "oldrun" / "t0001"
    turn1 = {
        "condition": "reset",
        "stream_id": "stream-a",
        "turn_id": "turn1",
        "turn_index": 1,
        "sequence_role": "learning",
        "turn_score": 1.0,
        "evaluation_status": "passed",
        "passed": True,
        "final_score": 1.0,
        "final_category": "pass",
        "interaction_count": 1,
        "trace_file": str(run_dir / "trace.jsonl"),
        "evolution": {"completed": True, "events": []},
        "checkpoint": str(turn1_checkpoint),
    }
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": "20260907T000000Z-deadbeef",
        "stream_id": "stream-a",
        "baseline": "hermes",
        "condition": "reset",
        "turns": [turn1],
        "success": False,
        "error": "BaselineProtocolError: baseline adapter timed out during interact",
        "token_budget": {"limit": 60000000, "used": 123, "calls": 4},
        "work_limits": {
            "max_model_calls": 24,
            "max_tool_calls": 24,
            "terminal_timeout_seconds": 120,
            "episode_timeout_seconds": 900,
            "max_output_tokens": 12000,
            "evolution_timeout_seconds": 120,
            "evolution_max_model_calls": 4,
        },
        "run_configuration": {"evaluation_error_policy": "stop"},
    })
    (run_dir / "trace.jsonl").write_text("", encoding="utf-8")
    with zipfile.ZipFile(run_dir / "error_workspace.zip", "w") as bundle:
        bundle.writestr("seed.txt", "seed")
        bundle.writestr("inputs/turn2.txt", "turn2")
        bundle.writestr("partial-timeout.txt", "partial")
    return run_dir, stream_manifest, job_output


def test_admit_timeout_creates_next_checkpoint_without_saved_reply(tmp_path):
    run_dir, stream_manifest, job_output = _failed_run(tmp_path)

    receipt = admit_timeout(run_dir, stream_manifest, job_output=job_output)

    checkpoint = Path(receipt["checkpoint"])
    assert checkpoint.name == "t0002"
    assert (checkpoint / "workspace.zip").is_file()
    assert (checkpoint / "baseline_state.zip").is_file()
    assert not (checkpoint / "evidence.json").exists()
    completed = json.loads((checkpoint / "completed_turns.json").read_text(encoding="utf-8"))
    assert [turn["turn_id"] for turn in completed] == ["turn1", "turn2"]
    assert completed[-1]["turn_score"] == 0.0
    assert completed[-1]["evaluation_status"] == "failed"
    assert completed[-1]["final_category"] == "execution_limit"
    assert receipt["workspace_changes"] == ["partial-timeout.txt"]
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["turns"][-1]["checkpoint"] == str(checkpoint)
    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trace_lines) == 2
    interaction = json.loads(trace_lines[0])
    assert interaction["response"] == ""
    assert interaction["workspace_changes"] == ["partial-timeout.txt"]


def test_admit_timeout_rejects_cleanup_error_report(tmp_path):
    run_dir, stream_manifest, job_output = _failed_run(tmp_path)
    report_path = run_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["cleanup_error"] = "RuntimeError: docker rm failed"
    _write_json(report_path, report)

    with pytest.raises(ValueError, match="cleanup_error"):
        admit_timeout(run_dir, stream_manifest, job_output=job_output)


def test_admit_timeout_rejects_non_timeout_report(tmp_path):
    run_dir, stream_manifest, job_output = _failed_run(tmp_path)
    report_path = run_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["error"] = "L2AgentRunError: evaluation_error in turn2"
    _write_json(report_path, report)

    with pytest.raises(ValueError, match="not a baseline interact timeout"):
        admit_timeout(run_dir, stream_manifest, job_output=job_output)


def test_admit_timeout_rejects_nested_timeout_text(tmp_path):
    run_dir, stream_manifest, job_output = _failed_run(tmp_path)
    report_path = run_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["error"] = "RuntimeError: adapter failed after BaselineProtocolError: baseline adapter timed out during interact"
    _write_json(report_path, report)

    with pytest.raises(ValueError, match="not a baseline interact timeout"):
        admit_timeout(run_dir, stream_manifest, job_output=job_output)


def test_admit_timeout_rejects_saved_same_turn_trace(tmp_path):
    run_dir, stream_manifest, job_output = _failed_run(tmp_path)
    with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as trace:
        trace.write(json.dumps({
            "turn_id": "turn2",
            "interaction_index": 1,
            "response": "first failed answer",
        }) + "\n")

    with pytest.raises(ValueError, match="first-interaction timeouts"):
        admit_timeout(run_dir, stream_manifest, job_output=job_output)
