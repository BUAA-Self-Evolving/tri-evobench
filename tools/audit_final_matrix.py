"""Audit a completed matrix and replay L1 native checks from exact checkpoints.

The replay is deliberately judge-only: it restores the post-turn workspace
archive, validates it against the recorded final-interaction file manifest,
and invokes the package-owned deterministic checker in Docker.  It never
calls an agent, user simulator, or LLM judge and never rewrites the original
reports or traces.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any

from trievo_bench.arena_runtime import ArenaVerifier
from trievo_bench.l1_arena_compat import load_l1_arena_stream
from trievo_bench.sandbox import DockerSandbox, _make_readonly_export_traversable


REPLAY_VERSION = 1


class ReplayDockerSandbox(DockerSandbox):
    """Type-compatible read-only sandbox over an extracted checkpoint."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def export_workspace(self) -> Path:
        _make_readonly_export_traversable(self.root)
        return self.root

    def workspace_path(self) -> Path:
        return self.root

    def cleanup_export(self, path: Path) -> None:
        return None

    def close(self) -> None:
        return None


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _safe_extract(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe checkpoint entry: {info.filename}")
            destination = target.joinpath(*relative.parts)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.read(info))


def _workspace_manifest(root: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            result[path.relative_to(root).as_posix()] = (len(data), hashlib.sha256(data).hexdigest())
    return result


def _recorded_manifest(turn: dict[str, Any]) -> dict[str, tuple[int, str]]:
    files = turn["interactions"][-1].get("sandbox", {}).get("files", [])
    result: dict[str, tuple[int, str]] = {}
    for item in files:
        path = str(item["path"])
        prefix = "/workspace/"
        if path.startswith(prefix):
            path = path[len(prefix):]
        result[path] = (int(item["size"]), str(item["sha256"]))
    return result


def _checkpoint_for(job: dict[str, Any], number: int) -> Path:
    candidates: list[Path] = []
    root = Path(job["output"]) / "_checkpoints"
    for path in root.glob(f"**/t{number:04d}/checkpoint.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            data.get("flow_id") == job["flow_id"]
            and data.get("baseline") == job["agent"]
            and data.get("completed_turn_index") == number - 1
        ):
            candidates.append(path.parent)
    if len(candidates) != 1:
        raise RuntimeError(f"{job['id']} turn {number}: expected one checkpoint, found {len(candidates)}")
    return candidates[0]


def _regrade_turn(
    job: dict[str, Any],
    turn_number: int,
    turn_spec: Any,
    original_turn: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    destination = output / "l1_regrade" / job["agent"] / job["id"].split("/")[-1] / f"turn{turn_number:04d}.json"
    if destination.is_file():
        cached = json.loads(destination.read_text(encoding="utf-8"))
        if cached.get("replay_version") == REPLAY_VERSION and cached.get("status") == "ok":
            return cached

    checkpoint = _checkpoint_for(job, turn_number)
    archive = checkpoint / "workspace.zip"
    if not archive.is_file():
        raise FileNotFoundError(archive)
    final_interaction = original_turn["interactions"][-1]
    primary_type = turn_spec.verifier["evaluation"]["primary_evaluation"]["type"]
    result: dict[str, Any] = {
        "replay_version": REPLAY_VERSION,
        "status": "ok",
        "job_id": job["id"],
        "flow_id": job["flow_id"],
        "turn_id": original_turn["turn_id"],
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "primary_evaluation_type": primary_type,
        "original": {
            "score": original_turn.get("score"),
            "passed": original_turn.get("passed"),
            "final_interaction_score": final_interaction.get("score"),
            "final_interaction_passed": final_interaction.get("passed"),
        },
    }
    if primary_type == "deterministic_response_check":
        result["evidence_mode"] = "original_deterministic_response_check"
        result["regraded"] = {
            "score": original_turn.get("score"),
            "passed": original_turn.get("passed"),
            "feedback": final_interaction.get("feedback"),
            "details": final_interaction.get("verifier_details", {}),
        }
        _write_json_atomic(destination, result)
        return result

    with tempfile.TemporaryDirectory(prefix="trievo-l1-regrade-") as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        _safe_extract(archive, workspace)
        actual = _workspace_manifest(workspace)
        recorded = _recorded_manifest(original_turn)
        if actual != recorded:
            missing = sorted(set(recorded) - set(actual))[:10]
            extra = sorted(set(actual) - set(recorded))[:10]
            changed = sorted(path for path in set(actual) & set(recorded) if actual[path] != recorded[path])[:10]
            raise RuntimeError(
                f"{job['id']} {original_turn['turn_id']}: checkpoint/final-interaction workspace mismatch "
                f"missing={missing} extra={extra} changed={changed}"
            )
        sandbox = ReplayDockerSandbox(workspace)
        checked = ArenaVerifier().evaluate(turn_spec.verifier, str(final_interaction.get("response", "")), sandbox, {})
        serialized = checked.details
        text = json.dumps(serialized, ensure_ascii=False)
        if "PermissionError" in text:
            raise RuntimeError(f"{job['id']} {original_turn['turn_id']}: permission error survived replay")
        for evidence in serialized.get("verifier_results", {}).values():
            if evidence.get("type") == "exec_verifier_timeout" or evidence.get("returncode") in {125, 126, 127}:
                raise RuntimeError(f"{job['id']} {original_turn['turn_id']}: verifier infrastructure failure: {evidence}")
        result["evidence_mode"] = "exact_post_turn_checkpoint_native_replay"
        result["workspace_file_count"] = len(actual)
        result["regraded"] = {
            "score": checked.score,
            "passed": checked.passed,
            "feedback": checked.feedback,
            "details": serialized,
        }
    _write_json_atomic(destination, result)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_l2(job: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    traces: dict[str, list[dict[str, Any]]] = {}
    for trace_path in (Path(job["output"]) / job["flow_id"]).glob("*/trace.jsonl"):
        if trace_path.is_file() and trace_path.stat().st_size:
            traces[str(trace_path)] = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    native_records = 0
    evolution_limit_exhausted = 0
    fragmented_turns: list[dict[str, Any]] = []
    used_trace_files: set[str] = set()
    for turn in report["turns"]:
        trace_path = Path(turn["trace_file"])
        if not trace_path.is_file() or not trace_path.stat().st_size:
            raise RuntimeError(f"missing L2 trace: {trace_path}")
        key = str(trace_path)
        if key not in traces:
            traces[key] = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        candidates: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        for source, records in traces.items():
            for item in records:
                if item.get("turn_id") == turn["turn_id"] and "interaction_index" in item:
                    candidates.setdefault(int(item["interaction_index"]), []).append((source, item))
        expected_indices = list(range(1, int(turn["interaction_count"]) + 1))
        if sorted(candidates) != expected_indices:
            raise RuntimeError(
                f"{job['id']} {turn['turn_id']}: L2 trace interaction chain mismatch "
                f"expected={expected_indices} actual={sorted(candidates)}"
            )
        selected: list[tuple[str, dict[str, Any]]] = []
        for index in expected_indices:
            choices = candidates[index]
            valid = [
                pair for pair in choices
                if isinstance(pair[1].get("verifier"), dict)
                and isinstance(pair[1]["verifier"].get("native_result"), dict)
            ]
            if index == expected_indices[-1]:
                score_matches = [
                    pair for pair in valid
                    if abs(float(pair[1].get("score", 0.0)) - float(turn["final_score"])) < 1e-9
                ]
                valid = score_matches or valid
                valid.sort(key=lambda pair: (pair[0] == key, Path(pair[0]).stat().st_mtime), reverse=True)
            if not valid:
                raise RuntimeError(f"{job['id']} {turn['turn_id']} interaction{index}: missing native verifier evidence")
            selected.append(valid[0])
        selected_files = list(dict.fromkeys(source for source, _ in selected))
        used_trace_files.update(selected_files)
        if len(selected_files) > 1 or len([item for item in traces[key] if item.get("turn_id") == turn["turn_id"] and "interaction_index" in item]) != len(expected_indices):
            fragmented_turns.append({
                "job_id": job["id"],
                "turn_id": turn["turn_id"],
                "trace_files": selected_files,
            })
        final = selected[-1][1]
        verifier = final["verifier"]
        native = verifier["native_result"]
        if native.get("schema_version") != "l2_native_verifier_result/1.0":
            raise RuntimeError(f"{job['id']} {turn['turn_id']}: unexpected native schema")
        native_records += 1
        evolution = turn.get("evolution", {})
        if not evolution.get("completed"):
            events = evolution.get("events", [])
            if not any(item.get("type") == "evolution.limit_exhausted" for item in events):
                raise RuntimeError(f"{job['id']} {turn['turn_id']}: unexplained evolution failure")
            evolution_limit_exhausted += 1
    return {
        "native_records": native_records,
        "unique_trace_files": sorted(used_trace_files),
        "evolution_limit_exhausted": evolution_limit_exhausted,
        "fragmented_turns": fragmented_turns,
    }


def _artifact(path: Path, base: Path, with_hash: bool) -> dict[str, Any]:
    item = {"path": str(path), "relative_path": os.path.relpath(path, base), "bytes": path.stat().st_size}
    if with_hash:
        item["sha256"] = _sha256(path)
    return item


def _mean(total: float, count: int) -> float:
    return total / count if count else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("runs/mx24/full"))
    parser.add_argument("--package", type=Path, default=Path("benchmarks/current/l1_arena"))
    parser.add_argument("--output", type=Path, default=Path("runs/mx24/final_audit"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--job", help="optional matrix job id for a bounded smoke replay")
    parser.add_argument("--turn", type=int, help="optional one-based turn number for a bounded smoke replay")
    parser.add_argument("--hash-artifacts", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    package = args.package.resolve()
    output = args.output.resolve()
    status_path = root / "matrix_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("status") != "completed" or len(status.get("jobs", [])) != 87:
        raise RuntimeError("matrix is not a completed 87-job run")
    if any(job.get("status") != "completed" for job in status["jobs"]):
        raise RuntimeError("matrix contains non-completed jobs")

    selected_l1 = [job for job in status["jobs"] if job["level"] == "l1" and (not args.job or job["id"] == args.job)]
    if args.job and not selected_l1:
        raise ValueError("--job must identify an L1 matrix job")
    tasks: list[tuple[dict[str, Any], int, Any, dict[str, Any]]] = []
    reports: dict[str, dict[str, Any]] = {}
    for job in selected_l1:
        report = json.loads(Path(job["report"]).read_text(encoding="utf-8"))
        reports[job["id"]] = report
        if [turn["turn_id"] for turn in report["turns"]] != job["turns"]:
            raise RuntimeError(f"{job['id']}: report turn order mismatch")
        spec = load_l1_arena_stream(package, job["stream"], job["turns"], "docker", "trievo-ubuntu:22.04")
        for number, (turn_spec, original_turn) in enumerate(zip(spec.flows[0].turns, report["turns"]), 1):
            if args.turn and number != args.turn:
                continue
            tasks.append((job, number, turn_spec, original_turn))

    regraded: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(_regrade_turn, job, number, turn_spec, original_turn, output): (job["id"], original_turn["turn_id"])
            for job, number, turn_spec, original_turn in tasks
        }
        for future in as_completed(futures):
            key = futures[future]
            regraded[key] = future.result()
            print(f"AUDITED {key[0]} {key[1]}", flush=True)

    if args.job or args.turn:
        print(json.dumps({"status": "smoke_complete", "turns": len(regraded)}, ensure_ascii=False))
        return

    cells: dict[tuple[str, str], dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    l2_native_records = 0
    l2_fragmented_turns: list[dict[str, Any]] = []
    evolution_limit_exhausted: list[dict[str, str]] = []
    for job in status["jobs"]:
        report_path = Path(job["report"])
        report = reports.get(job["id"]) or json.loads(report_path.read_text(encoding="utf-8"))
        if [turn["turn_id"] for turn in report["turns"]] != job["turns"]:
            raise RuntimeError(f"{job['id']}: report turn order mismatch")
        cell = cells.setdefault((job["level"], job["agent"]), {
            "level": job["level"], "agent": job["agent"], "jobs": 0, "turns": 0,
            "passed_turns": 0, "score_sum": 0.0, "token_count": 0, "api_calls": 0,
            "raw_passed_turns": 0, "raw_score_sum": 0.0, "regraded_changed_turns": 0,
        })
        cell["jobs"] += 1
        cell["turns"] += len(report["turns"])
        budget = report.get("token_budget", {})
        cell["token_count"] += int(budget.get("used", 0) or 0)
        cell["api_calls"] += int(budget.get("calls", 0) or 0)
        artifacts[str(report_path)] = _artifact(report_path, root, args.hash_artifacts)
        if job["level"] == "l1":
            trace_path = report_path.parent / "trace.jsonl"
            if not trace_path.is_file() or not trace_path.stat().st_size:
                raise RuntimeError(f"missing L1 trace: {trace_path}")
            artifacts[str(trace_path)] = _artifact(trace_path, root, args.hash_artifacts)
            for turn in report["turns"]:
                replay = regraded[(job["id"], turn["turn_id"])]
                grade = replay["regraded"]
                cell["passed_turns"] += int(bool(grade["passed"]))
                cell["score_sum"] += float(grade["score"])
                cell["raw_passed_turns"] += int(bool(turn["passed"]))
                cell["raw_score_sum"] += float(turn["score"])
                if bool(grade["passed"]) != bool(turn["passed"]) or abs(float(grade["score"]) - float(turn["score"])) > 1e-9:
                    cell["regraded_changed_turns"] += 1
                evolution = turn.get("evolution", {})
                if not evolution.get("completed"):
                    if any(item.get("type") == "evolution.limit_exhausted" for item in evolution.get("events", [])):
                        evolution_limit_exhausted.append({"job_id": job["id"], "turn_id": turn["turn_id"]})
                    else:
                        raise RuntimeError(f"{job['id']} {turn['turn_id']}: unexplained evolution failure")
        else:
            audited = _audit_l2(job, report)
            l2_native_records += audited["native_records"]
            l2_fragmented_turns.extend(audited["fragmented_turns"])
            for trace in audited["unique_trace_files"]:
                path = Path(trace)
                artifacts[str(path)] = _artifact(path, root, args.hash_artifacts)
            for turn in report["turns"]:
                cell["passed_turns"] += int(bool(turn["passed"]))
                cell["score_sum"] += float(turn["final_score"])
                cell["raw_passed_turns"] += int(bool(turn["passed"]))
                cell["raw_score_sum"] += float(turn["final_score"])

    expected = {("l1", agent): 423 for agent in ("hermes", "skillopt", "trace2skill")}
    expected.update({("l2", agent): 134 for agent in ("hermes", "skillopt", "trace2skill")})
    for key, count in expected.items():
        if cells.get(key, {}).get("turns") != count:
            raise RuntimeError(f"cell {key}: expected {count} turns, got {cells.get(key)}")
    for cell in cells.values():
        cell["mean_score"] = _mean(cell.pop("score_sum"), cell["turns"])
        cell["pass_rate"] = _mean(cell["passed_turns"], cell["turns"])
        cell["raw_mean_score"] = _mean(cell.pop("raw_score_sum"), cell["turns"])
        cell["raw_pass_rate"] = _mean(cell["raw_passed_turns"], cell["turns"])

    result = {
        "schema_version": "trievo-final-matrix-audit/1.0",
        "status": "passed",
        "source_matrix": str(status_path),
        "jobs": len(status["jobs"]),
        "total_agent_turns": sum(len(job["turns"]) for job in status["jobs"]),
        "cells": [cells[key] for key in sorted(cells)],
        "l1_regrade": {
            "reason": "original Docker bind export was not traversable under daemon user-namespace remapping",
            "method": "exact post-turn workspace checkpoint plus package-owned native verifier in Docker; no agent/LLM calls",
            "turns_regraded": len(regraded),
            "permission_errors_after_regrade": 0,
        },
        "l2_native_records": l2_native_records,
        "l2_fragmented_turn_trace_chains": l2_fragmented_turns,
        "bounded_evolution_failures": evolution_limit_exhausted,
        "artifacts": sorted(artifacts.values(), key=lambda item: item["relative_path"]),
    }
    _write_json_atomic(output / "matrix.json", result)
    lines = [
        "# Tri-EvoBench final matrix audit",
        "",
        f"Status: **{result['status']}**  ",
        f"Jobs: **{result['jobs']}/87**  ",
        f"Agent-turns: **{result['total_agent_turns']}/1671**  ",
        f"L2 native evidence records: **{l2_native_records}/402**",
        "",
        "| Level | Agent | Jobs | Turns | Passed | Pass rate | Mean score | Tokens | API calls |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cell in result["cells"]:
        lines.append(
            f"| {cell['level'].upper()} | {cell['agent']} | {cell['jobs']} | {cell['turns']} | "
            f"{cell['passed_turns']} | {cell['pass_rate']:.4f} | {cell['mean_score']:.4f} | "
            f"{cell['token_count']} | {cell['api_calls']} |"
        )
    lines += [
        "",
        "## L1 permission-repair impact",
        "",
        "| Agent | Raw passed | Regraded passed | Raw mean | Regraded mean | Changed turns |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cell in (item for item in result["cells"] if item["level"] == "l1"):
        lines.append(
            f"| {cell['agent']} | {cell['raw_passed_turns']} | {cell['passed_turns']} | "
            f"{cell['raw_mean_score']:.4f} | {cell['mean_score']:.4f} | {cell['regraded_changed_turns']} |"
        )
    lines += [
        "",
        "L1 scores are repaired, evidence-preserving native regrades from each exact post-turn checkpoint. "
        "The replay made no agent, user-simulator, or LLM calls and did not modify original reports or traces.",
        "",
        f"One bounded self-evolution timeout is retained as an agent outcome: `{evolution_limit_exhausted}`.",
        "",
        "See `matrix.json` for the complete artifact manifest and per-turn files under `l1_regrade/`.",
    ]
    (output / "matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(output), "turns": result["total_agent_turns"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
