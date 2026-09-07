"""Admit a reset TaskStream interact timeout as a scored work-limit turn.

This recovery is intentionally narrow: it consumes a failed run whose baseline
adapter timed out during ``interact`` after workspace effects were captured, and
creates the next turn-boundary checkpoint without calling the baseline,
verifier, Judge, or model provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from trievo_bench.evaluation_outcome import BoundedFeedback, EvaluationOutcome
from trievo_bench.l2_agent import (
    _changed_workspace_files,
    _taskstream_request,
    _under,
    _workspace_digest,
    _work_limits,
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _archive_digest(archive: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    with zipfile.ZipFile(archive) as bundle:
        for name in sorted(bundle.namelist()):
            if name.endswith("/"):
                continue
            digests[name] = hashlib.sha256(bundle.read(name)).hexdigest()
    return digests


def _copy_transition(task: dict[str, Any], turn_dir: Path, workspace: Path) -> None:
    transition = task.get("workspace_transition", {})
    if not isinstance(transition, dict) or transition.get("mode") not in {"continue", "continue_with_patch"}:
        raise ValueError("unsupported TaskStream workspace transition")
    for operation in transition.get("operations", []):
        if not isinstance(operation, dict) or operation.get("op") != "copy":
            raise ValueError("TaskStream timeout recovery supports copy transitions only")
        source = _under(turn_dir, str(operation.get("source", "")))
        target_name = str(operation.get("target", "")).removeprefix("agent_workspace/")
        target = _under(workspace, target_name)
        if target.exists():
            raise ValueError(f"TaskStream transition target already exists: {target_name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def _workspace_before_turn(stream_dir: Path, manifest: dict[str, Any], turn_id: str) -> dict[str, str]:
    agent_workspace = _under(stream_dir, str(manifest.get("agent_workspace", "agent_workspace")))
    if not agent_workspace.is_dir():
        raise ValueError("TaskStream agent_workspace seed is missing")
    with tempfile.TemporaryDirectory(prefix="trievo-timeout-before-") as tmp:
        workspace = Path(tmp) / "workspace"
        shutil.copytree(agent_workspace, workspace)
        task = _load(stream_dir / turn_id / "task.json")
        _copy_transition(task, stream_dir / turn_id, workspace)
        return _workspace_digest(workspace)


def admit_timeout(run_dir: Path, stream_manifest: Path, *, job_output: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    stream_manifest = stream_manifest.resolve()
    report_path = run_dir / "report.json"
    error_workspace = run_dir / "error_workspace.zip"
    report = _load(report_path)
    manifest = _load(stream_manifest)
    if report.get("schema_version") != "l2-agent-runtime-0.2-taskstream":
        raise ValueError("report is not an L2 TaskStream report")
    if report.get("condition") != "reset":
        raise ValueError("timeout admission is only safe for reset TaskStream runs")
    if report.get("cleanup_error"):
        raise ValueError("timeout admission is unsafe when cleanup_error is present")
    error = str(report.get("error", ""))
    if error != "BaselineProtocolError: baseline adapter timed out during interact":
        raise ValueError("report error is not a baseline interact timeout")
    if not error_workspace.is_file():
        raise ValueError("failed run has no error_workspace.zip")
    stream_id = str(report.get("stream_id", ""))
    if manifest.get("stream_id") != stream_id:
        raise ValueError("stream manifest does not match report")
    turn_order = manifest.get("ordering", {}).get("turn_order")
    if not isinstance(turn_order, list) or not all(isinstance(item, str) for item in turn_order):
        raise ValueError("TaskStream ordering.turn_order is invalid")
    turns = list(report.get("turns", []))
    turn_index = len(turns)
    if turn_index >= len(turn_order):
        raise ValueError("report already contains all turns")
    if [turn.get("turn_id") for turn in turns] != turn_order[:turn_index]:
        raise ValueError("report turns are not the stream prefix")
    turn_id = turn_order[turn_index]
    trace_path = run_dir / "trace.jsonl"
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("turn_id") == turn_id:
                raise ValueError(
                    "timeout recovery only supports first-interaction timeouts with no saved same-turn trace"
                )
    run_id = str(report.get("run_id", ""))
    if not run_id:
        raise ValueError("report has no run_id")
    output_root = (job_output.resolve() if job_output else run_dir.parents[1])
    stream_key = hashlib.sha256(stream_id.encode("utf-8")).hexdigest()[:10]
    checkpoint = output_root / "_checkpoints" / stream_key / run_id[-8:] / f"t{turn_index + 1:04d}"
    if checkpoint.exists():
        raise ValueError(f"checkpoint already exists: {checkpoint}")
    staging_checkpoint = checkpoint.with_name(checkpoint.name + ".tmp-recovery")
    if staging_checkpoint.exists():
        raise ValueError(f"staging checkpoint already exists: {staging_checkpoint}")
    before_files = _workspace_before_turn(stream_manifest.parent, manifest, turn_id)
    after_files = _archive_digest(error_workspace)
    limits = _work_limits({"options": {"l2_agent_limits": report.get("work_limits", {})}})
    task = _load(stream_manifest.parent / turn_id / "task.json")
    request = _taskstream_request(task, limits)
    verification = {
        "category": "execution_limit",
        "score": 0.0,
        "details": {
            "type": "work_limit",
            "reason": "episode_timeout",
            "detail": error,
        },
    }
    outcome = EvaluationOutcome.scored(
        turn_score=0.0,
        passed=False,
        bounded_feedback=BoundedFeedback(
            failed_dimensions=("resource_limit",),
            observable_deficiency="The task attempt exhausted its declared public work budget.",
            public_result_category="fail",
        ),
        audit_details={"work_limit": verification},
    )
    record = {
        "turn_id": turn_id,
        "interaction_index": 1,
        "request": request,
        "response": "",
        "tool_calls": [],
        "events": [{"type": "work_limit.reached", "reason": "episode_timeout"}],
        "usage": {},
        "workspace_changes": _changed_workspace_files(before_files, after_files),
        "verifier": verification,
        "score": 0.0,
        "passed": False,
        "evaluation_status": outcome.evaluation_status.value,
        "bounded_feedback": outcome.bounded_feedback_mapping(),
    }
    evolution = {
        "completed": False,
        "events": [{"type": "evolution.skipped", "reason": "runtime_unavailable"}],
        "usage": {},
        "state_before": {},
        "state_after": {},
        "changed_files": [],
    }
    trace_suffix = (
        json.dumps(record, ensure_ascii=False)
        + "\n"
        + json.dumps({"record_type": "evolution", "turn_id": turn_id, "evolution": evolution}, ensure_ascii=False)
        + "\n"
    )
    turn = {
        "condition": "reset",
        "stream_id": stream_id,
        "turn_id": turn_id,
        "turn_index": turn_index + 1,
        "sequence_role": str(task.get("sequence_role", "")),
        "turn_score": 0.0,
        "evaluation_status": outcome.evaluation_status.value,
        "passed": False,
        "final_score": 0.0,
        "final_category": "execution_limit",
        "interaction_count": 1,
        "trace_file": str(run_dir / "trace.jsonl"),
        "evolution": evolution,
        "checkpoint": str(checkpoint),
    }
    recovered_turns = [*turns, turn]
    staging_checkpoint.mkdir(parents=True)
    shutil.copy2(error_workspace, staging_checkpoint / "workspace.zip")
    with zipfile.ZipFile(staging_checkpoint / "baseline_state.zip", "w", compression=zipfile.ZIP_DEFLATED):
        pass
    _save(staging_checkpoint / "completed_turns.json", recovered_turns)
    _save(staging_checkpoint / "checkpoint.json", {
        "stream_id": stream_id,
        "baseline": report.get("baseline"),
        "condition": "reset",
        "evaluation_error_policy": report.get("run_configuration", {}).get("evaluation_error_policy", "stop"),
        "turn_id": turn_id,
        "completed_turn_index": turn_index,
        "next_turn_index": turn_index + 1,
        "token_budget": report.get("token_budget", {}),
        "recovery": {
            "type": "reset_taskstream_interact_timeout",
            "source_report": str(report_path),
            "source_error_workspace": str(error_workspace),
        },
    })
    trace_original = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
    trace_tmp = trace_path.with_suffix(trace_path.suffix + ".tmp")
    trace_tmp.write_text(trace_original + trace_suffix, encoding="utf-8")
    trace_tmp.replace(trace_path)
    staging_checkpoint.replace(checkpoint)
    report["turns"] = recovered_turns
    report["error"] = "recovered_interact_timeout: admitted as execution_limit; resume from checkpoint"
    report["success"] = False
    report["token_budget"] = report.get("token_budget", {})
    _save(report_path, report)
    receipt = {
        "status": "admitted",
        "turn_id": turn_id,
        "turn_index": turn_index + 1,
        "checkpoint": str(checkpoint),
        "source_report": str(report_path),
        "source_error_workspace": str(error_workspace),
        "workspace_changes": record["workspace_changes"],
        "token_budget": report.get("token_budget", {}),
    }
    _save(run_dir / "timeout-recovery.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--stream-manifest", type=Path, required=True)
    parser.add_argument("--job-output", type=Path)
    args = parser.parse_args()
    print(json.dumps(admit_timeout(args.run_dir, args.stream_manifest, job_output=args.job_output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
