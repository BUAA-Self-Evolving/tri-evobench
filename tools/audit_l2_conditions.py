"""Audit the completed L2 reset/frozen matrix and aggregate matched probes.

This is read-only with respect to run evidence.  It validates reports and
traces, then writes derived JSON summaries under the condition-run root.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tools.audit_final_matrix import _audit_l2


AGENTS = ("hermes", "skillopt", "trace2skill")
CONDITIONS = ("reset", "frozen")
EXCLUDED_STREAM = "sra_bigcodebench_pandas_dataframe_viz_001"


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("l2_evolution_reference", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reasoning_values(value: Any) -> list[int]:
    found: list[int] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "reasoning_tokens":
                found.append(int(item or 0))
            else:
                found.extend(_reasoning_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_reasoning_values(item))
    return found


def _trace_rows(paths: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"missing trace {path}")
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("runs/mx25/l2-controls"))
    parser.add_argument("--evolving-root", type=Path, default=Path("runs/mx24/full"))
    parser.add_argument("--package", type=Path, default=Path("benchmarks/current/L2_Demo_V4"))
    args = parser.parse_args()
    root = args.root.resolve()
    package = args.package.resolve()
    status_path = root / "matrix_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    jobs = status.get("jobs", [])
    if status.get("status") != "completed" or len(jobs) != 30:
        raise RuntimeError("condition matrix must be completed with exactly 30 jobs")
    if any(job.get("status") != "completed" for job in jobs):
        raise RuntimeError("condition matrix contains a non-completed job")

    roles: dict[str, dict[str, str]] = {}
    expected_turns: dict[str, list[str]] = {}
    for stream_path in sorted((package / "streams").glob("*/stream.json")):
        stream = json.loads(stream_path.read_text(encoding="utf-8"))
        stream_id = stream["stream_id"]
        if stream_id == EXCLUDED_STREAM:
            continue
        roles[stream_id] = {item["turn_id"]: item["sequence_role"] for item in stream["turns"]}
        expected_turns[stream_id] = [item["turn_id"] for item in stream["turns"]]

    cells: dict[tuple[str, str], dict[str, Any]] = {}
    control_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    native_records = 0
    trace_files: set[str] = set()
    fragmented: list[dict[str, Any]] = []
    reasoning_fields = 0
    skip_events = 0
    state_pairs = 0
    report_paths: list[str] = []
    for job in jobs:
        condition, agent = job["condition"], job["agent"]
        if condition not in CONDITIONS or agent not in AGENTS:
            raise RuntimeError(f"unexpected job identity {job['id']}")
        report_path = Path(job["report"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_paths.append(str(report_path))
        if report.get("condition") != condition or report.get("baseline") != agent:
            raise RuntimeError(f"{job['id']}: report identity mismatch")
        if report.get("stream_id") != job["stream"]:
            raise RuntimeError(f"{job['id']}: report stream mismatch")
        if report.get("error") or report.get("cleanup_error"):
            raise RuntimeError(f"{job['id']}: report contains framework/cleanup error")
        turns = report.get("turns", [])
        if [turn.get("turn_id") for turn in turns] != expected_turns[job["stream"]]:
            raise RuntimeError(f"{job['id']}: turn order mismatch")
        audited = _audit_l2(job, report)
        native_records += int(audited["native_records"])
        trace_files.update(audited["unique_trace_files"])
        fragmented.extend(audited["fragmented_turns"])
        rows = _trace_rows(audited["unique_trace_files"])
        values = _reasoning_values(rows)
        reasoning_fields += len(values)
        if any(values):
            raise RuntimeError(f"{job['id']}: non-zero reasoning token usage")

        cell = cells.setdefault((condition, agent), {
            "condition": condition, "agent": agent, "streams": 0, "turns": 0,
            "passed_turns": 0, "score_sum": 0.0, "tokens": 0, "api_calls": 0,
            "per_stream": {},
        })
        cell["streams"] += 1
        cell["turns"] += len(turns)
        cell["tokens"] += int(report.get("token_budget", {}).get("used", 0) or 0)
        cell["api_calls"] += int(report.get("token_budget", {}).get("calls", 0) or 0)
        stream_scores: list[float] = []
        for turn in turns:
            if turn.get("condition") != condition:
                raise RuntimeError(f"{job['id']} {turn.get('turn_id')}: condition mismatch")
            evolution = turn.get("evolution", {})
            if not evolution.get("completed"):
                raise RuntimeError(f"{job['id']} {turn.get('turn_id')}: skip barrier incomplete")
            events = evolution.get("events", [])
            if not any(item.get("type") in {"evolution.skipped", "evolution_skipped"} and item.get("reason") == condition for item in events):
                raise RuntimeError(f"{job['id']} {turn.get('turn_id')}: missing condition skip event")
            skip_events += 1
            usage = evolution.get("usage", {})
            if any(int(usage.get(key, 0) or 0) for key in ("input_tokens", "output_tokens", "cached_read_tokens", "cached_write_tokens", "reasoning_tokens", "total_tokens", "api_calls")):
                raise RuntimeError(f"{job['id']} {turn.get('turn_id')}: non-zero evolution usage")
            before = evolution.get("state_before", {}).get("digest")
            after = evolution.get("state_after", {}).get("digest")
            if not before or before != after or evolution.get("changed_files") != []:
                raise RuntimeError(f"{job['id']} {turn.get('turn_id')}: learning state changed")
            state_pairs += 1
            score = float(turn["final_score"])
            stream_scores.append(score)
            cell["score_sum"] += score
            cell["passed_turns"] += int(bool(turn["passed"]))
            control_records[agent].append({
                "condition": condition,
                "stream_id": job["stream"],
                "turn_id": turn["turn_id"],
                "turn_index": int(turn["turn_index"]),
                "sequence_role": roles[job["stream"]][turn["turn_id"]],
                "turn_score": score,
            })
        cell["per_stream"][job["stream"]] = {
            "turns": len(stream_scores),
            "passed_turns": sum(bool(turn["passed"]) for turn in turns),
            "mean_score": sum(stream_scores) / len(stream_scores),
        }

    expected_cell_turns = 34
    for key, cell in cells.items():
        if cell["streams"] != 5 or cell["turns"] != expected_cell_turns:
            raise RuntimeError(f"cell {key} is incomplete: {cell}")
        cell["mean_score"] = cell.pop("score_sum") / cell["turns"]
        cell["pass_rate"] = cell["passed_turns"] / cell["turns"]
        cell["tokens_per_turn"] = cell["tokens"] / cell["turns"]

    evolving_status = json.loads((args.evolving_root.resolve() / "matrix_status.json").read_text(encoding="utf-8"))
    evolving_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    evolving_sources: list[str] = []
    for job in evolving_status.get("jobs", []):
        if job.get("level") != "l2" or job.get("stream") not in roles:
            continue
        report_path = Path(job["report"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if [turn.get("turn_id") for turn in report.get("turns", [])] != expected_turns[job["stream"]]:
            raise RuntimeError(f"{job['id']}: evolving turn order mismatch")
        evolving_sources.append(str(report_path))
        for turn_index, turn in enumerate(report["turns"], 1):
            evolving_records[job["agent"]].append({
                "condition": "evolving",
                "stream_id": job["stream"],
                "turn_id": turn["turn_id"],
                "turn_index": int(turn.get("turn_index", turn_index)),
                "sequence_role": roles[job["stream"]][turn["turn_id"]],
                "turn_score": float(turn["final_score"]),
            })

    reference = _load_module(package / "contract" / "evolution_reference.py")
    formal = {
        "schema_version": "trievo-l2-formal-probe-matrix/1.0",
        "scope": {"streams": sorted(roles), "turns_per_condition_per_agent": 34, "excluded": [EXCLUDED_STREAM]},
        "agents": {},
    }
    for agent in AGENTS:
        records = control_records[agent] + evolving_records[agent]
        formal["agents"][agent] = reference.aggregate_reference_runs(records)

    audit = {
        "schema_version": "trievo-l2-condition-audit/1.0",
        "status": "passed",
        "scope": {"jobs": 30, "turns": 204, "streams": sorted(roles), "conditions": list(CONDITIONS), "agents": list(AGENTS)},
        "checks": {
            "condition_and_turn_identity": "passed",
            "native_verifier_evidence": {"status": "passed", "turns": native_records},
            "thinking_disabled": {"status": "passed", "reasoning_fields_checked": reasoning_fields, "nonzero": 0},
            "evolution_skipped": {"status": "passed", "turns": skip_events, "usage_tokens": 0, "api_calls": 0},
            "learning_state_unchanged": {"status": "passed", "turns": state_pairs, "changed_files": 0},
            "trace_references": {"status": "passed", "unique_files": len(trace_files), "fragmented_turn_chains": len(fragmented)},
            "framework_and_cleanup_errors": 0,
            "workspace_semantics": {
                "status": "passed",
                "basis": "condition-matched report identity plus previously passed real-Docker isolation/continuity gate and full-run checkpoint continuity",
                "reset": "fresh seed workspace/runtime per turn",
                "frozen": "persistent workspace/runtime with learning update skipped",
            },
        },
        "cells": [cells[key] for key in sorted(cells)],
        "reports": report_paths,
        "traces": sorted(trace_files),
        "fragmented_turn_trace_chains": fragmented,
        "formal_probe_output": str(root / "formal_probe_matrix.json"),
        "evolving_sources": evolving_sources,
    }
    _write_json(root / "final_audit.json", audit)
    _write_json(root / "formal_probe_matrix.json", formal)
    print(json.dumps({
        "status": "passed", "jobs": 30, "turns": 204, "native_records": native_records,
        "trace_files": len(trace_files), "reasoning_fields": reasoning_fields,
        "cells": audit["cells"], "formal_gains": {agent: formal["agents"][agent]["gains"] for agent in AGENTS},
        "outputs": [str(root / "final_audit.json"), str(root / "formal_probe_matrix.json")],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
