"""Serial, checkpoint-aware driver for the 2026-09-03 package matrix.

The evaluator owns fail-fast decisions; this driver never retries a failed
paid run blindly. A heartbeat can repair the issue and restart this command.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

L2_CONDITIONS = ("reset", "frozen", "evolving")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def _l1_manifests(package: Path):
    return sorted(
        package.glob("stream-*/stream.json"),
        key=lambda p: int(p.parent.name.split("-")[-1]),
    )


def _l2_manifests(package: Path):
    return sorted((package / "streams").glob("*/stream.json"))


def _resolve_l1_package(packages: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    return packages / "l1_arena"


def _resolve_l2_package(packages: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if _l2_manifests(packages):
        return packages
    candidates = sorted(
        child for child in packages.iterdir()
        if child.is_dir() and child.name != "l1_arena" and _l2_manifests(child)
    ) if packages.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return packages / "L2_Demo_V2"
    names = ", ".join(str(path) for path in candidates)
    raise ValueError(f"multiple L2 packages found; pass --l2-package explicitly: {names}")


def _turn_roles(manifest: dict):
    roles = {}
    for item in manifest.get("turns", []):
        if isinstance(item, dict) and item.get("turn_id") and item.get("sequence_role"):
            roles[str(item["turn_id"])] = str(item["sequence_role"])
    return roles


def jobs(
    repo: Path,
    root: Path,
    packages: Path | None = None,
    *,
    l1_package: Path | None = None,
    l2_package: Path | None = None,
    l2_conditions: tuple[str, ...] | list[str] | None = None,
    level: str | None = None,
):
    if level not in (None, "l1", "l2"):
        raise ValueError(f"unknown level: {level}")
    if packages is None:
        local_packages = repo.parent / "new-packages-20260903"
        packages = local_packages if local_packages.is_dir() else repo / "benchmarks/current"
    conditions = tuple(l2_conditions or ("evolving",))
    unknown = sorted(set(conditions) - set(L2_CONDITIONS))
    if unknown:
        raise ValueError(f"unknown L2 condition(s): {unknown}")
    rows = []
    selected_levels = (level,) if level else ("l2", "l1")
    for level in selected_levels:
        package = (_resolve_l2_package(packages, l2_package) if level == "l2"
                   else _resolve_l1_package(packages, l1_package))
        manifests = _l2_manifests(package) if level == "l2" else _l1_manifests(package)
        if not manifests:
            raise ValueError(f"No {level} stream manifests found in {package}")
        for index, path in enumerate(manifests, 1):
            manifest = load(path)
            turns = manifest["ordering"]["turn_order"] if level == "l2" else [item["turn_id"] for item in manifest["turns"]]
            row_conditions = conditions if level == "l2" else (None,)
            for condition in row_conditions:
                for agent in ("hermes", "skillopt", "trace2skill"):
                    if condition:
                        job_id = f"{level}/{condition}/{agent}/s{index:02d}"
                        output = root / level / condition / agent / f"s{index:02d}"
                    else:
                        job_id = f"{level}/{agent}/s{index:02d}"
                        output = root / level / agent / f"s{index:02d}"
                    rows.append({"id": job_id, "level": level, "agent": agent,
                                 "package": str(package), "stream": path.parent.name,
                                 "flow_id": manifest["stream_id"], "turns": turns,
                                 "condition": condition, "turn_roles": _turn_roles(manifest),
                                 "output": str(output)})
    return rows


def _turn_score(turn: dict):
    return turn.get("turn_score", turn.get("final_score", turn.get("score")))


def _report_condition(report: dict, turns: list[dict]):
    condition = report.get("condition", report.get("run_condition"))
    turn_conditions = {turn.get("condition") for turn in turns if turn.get("condition")}
    if len(turn_conditions) > 1:
        return "__mixed__"
    if condition and turn_conditions and condition not in turn_conditions:
        return "__mixed__"
    return condition or (next(iter(turn_conditions)) if turn_conditions else None)


def _report_matches_row(row: dict, report: dict) -> bool:
    turns = report.get("turns", [])
    if report.get("stream_id", report.get("flow_id")) != row["flow_id"]:
        return False
    if [turn.get("turn_id") for turn in turns] != row["turns"]:
        return False
    if report.get("error") or report.get("cleanup_error"):
        return False
    if row["level"] == "l2":
        condition = _report_condition(report, turns)
        # Reports produced before the condition field existed are evolving runs.
        if condition is None:
            if row.get("condition") != "evolving":
                return False
        elif condition != row.get("condition"):
            return False
    for turn in turns:
        if turn.get("final_category") == "error":
            return False
        if turn.get("evaluation_status") == "evaluation_error":
            return False
        if _turn_score(turn) is None:
            return False
    return True


def report_for(row):
    output = Path(row["output"])
    candidates = list(output.glob("*/summary.json")) if row["level"] == "l1" else list(output.glob("*/*/report.json"))
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        report = load(path)
        if _report_matches_row(row, report):
            return path, report
    return None, None


def report_for_interrupted_retry(row, stop_after_turn):
    output = Path(row["output"])
    if stop_after_turn not in row["turns"]:
        return None, None
    end = row["turns"].index(stop_after_turn) + 1
    prefix = {**row, "turns": row["turns"][:end]}
    for path in sorted(output.glob("*/*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        report = load(path)
        if report.get("stopped_after_turn") == stop_after_turn and _report_matches_row(prefix, report):
            return path, report
    return None, None


def latest_checkpoint(row):
    candidates = []
    unsafe_frontier = (-1, 0)
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("invalidated"):
            continue
        if data.get("stream_id", data.get("flow_id")) != row["flow_id"]:
            continue
        if row["level"] == "l2":
            checkpoint_condition = data.get("condition", data.get("run_condition"))
            if checkpoint_condition is None:
                if row.get("condition") != "evolving":
                    continue
            elif checkpoint_condition != row.get("condition"):
                continue
            if data.get("phase") == "interact_started":
                if _started_marker_has_same_run_pending(row, path.parent, data):
                    continue
                unsafe_frontier = max(
                    unsafe_frontier,
                    (int(data.get("next_turn_index", 0)), 2),
                )
                continue
        if all((path.parent / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
            index = int(data["next_turn_index"])
            pending = data.get("phase") == "adjudicate"
            if row["level"] == "l2":
                position = (index, int(pending))
                try:
                    turns = load(path.parent / "completed_turns.json")
                    prefix = {**row, "turns": row["turns"][:index]}
                    report = {"stream_id": row["flow_id"], "condition": checkpoint_condition, "turns": turns}
                    if not (0 <= index <= len(row["turns"]) and isinstance(turns, list)
                            and len(turns) == index and _report_matches_row(prefix, report)):
                        # An errored final Turn is not completed progress. Its
                        # saved adjudication reply is the earliest safe resume.
                        if isinstance(turns, list) and any(
                            isinstance(turn, dict) and (turn.get("evaluation_status") == "evaluation_error"
                                                       or _turn_score(turn) is None)
                            for turn in turns
                        ):
                            position = (max(0, index - 1), 1)
                        raise ValueError("invalid completed prefix")
                    if pending:
                        evidence = load(path.parent / "evidence.json")
                        if not (index < len(row["turns"]) and isinstance(evidence, dict)
                                and evidence.get("turn_id") == row["turns"][index]
                                and evidence.get("turn_index") == index
                                and isinstance(evidence.get("reply"), dict)):
                            raise ValueError("invalid adjudication evidence")
                except (OSError, ValueError, TypeError, AttributeError):
                    unsafe_frontier = max(unsafe_frontier, position)
                    continue
            stage = int(pending) if row["level"] == "l2" else 0
            candidates.append((index, stage, path.stat().st_mtime, path.parent))
    selected = max(candidates) if candidates else None
    if row["level"] == "l2" and unsafe_frontier > (selected[:2] if selected else (-1, 0)):
        raise ValueError("no safe L2 checkpoint preserves the latest Agent execution; inspect saved adjudication evidence")
    return selected[3] if selected else None


def _run_dir_for_retry(row, value):
    candidate = Path(value).expanduser()
    if candidate.exists():
        run_dir = candidate.resolve()
        if run_dir.is_file():
            run_dir = run_dir.parent
    else:
        matches = [
            path.parent
            for path in Path(row["output"]).glob(f"{row['flow_id']}/{value}/report.json")
        ]
        if len(matches) != 1:
            raise ValueError(f"retry run not found or ambiguous under row output: {value}")
        run_dir = matches[0].resolve()
    output = Path(row["output"]).resolve()
    if output != run_dir and output not in run_dir.parents:
        raise ValueError("retry run path is outside the selected row output")
    return run_dir


def _checkpoint_preserves_prefix(row, checkpoint, expected_index):
    checkpoint = Path(checkpoint).resolve()
    output = Path(row["output"]).resolve()
    if output != checkpoint and output not in checkpoint.parents:
        raise ValueError("retry checkpoint path is outside the selected row output")
    if not all((checkpoint / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
        raise ValueError("retry checkpoint is missing required archives")
    data = load(checkpoint / "checkpoint.json")
    if data.get("invalidated"):
        raise ValueError("retry checkpoint is invalidated")
    if data.get("non_resumable"):
        raise ValueError("retry checkpoint is non-resumable")
    turns = load(checkpoint / "completed_turns.json")
    condition = data.get("condition", data.get("run_condition"))
    report = {"stream_id": row["flow_id"], "condition": condition, "turns": turns}
    prefix = {**row, "turns": row["turns"][:expected_index]}
    if data.get("stream_id", data.get("flow_id")) != row["flow_id"]:
        raise ValueError("retry checkpoint stream identity mismatch")
    if data.get("baseline", row["agent"]) != row["agent"]:
        raise ValueError("retry checkpoint baseline identity mismatch")
    if condition != row.get("condition"):
        raise ValueError("retry checkpoint condition mismatch")
    if int(data.get("next_turn_index", -1)) != expected_index:
        raise ValueError("retry checkpoint turn frontier mismatch")
    if not (isinstance(turns, list) and len(turns) == expected_index and _report_matches_row(prefix, report)):
        raise ValueError("retry checkpoint does not preserve the completed prefix")


def _find_completed_prefix_checkpoint(row, expected_index):
    candidates = []
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("phase") in {"adjudicate", "interact_started"}:
            continue
        if int(data.get("next_turn_index", -1)) != expected_index:
            continue
        try:
            _checkpoint_preserves_prefix(row, path.parent, expected_index)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        candidates.append((path.stat().st_mtime, path.parent))
    return max(candidates)[1] if candidates else None


def _find_interrupted_checkpoint(row, run_id, turn_id):
    expected_index = row["turns"].index(turn_id)
    candidates = []
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("phase") != "interact_started":
            if (
                data.get("phase") == "adjudicate"
                and data.get("run_id") == run_id
                and data.get("turn_id") == turn_id
                and int(data.get("next_turn_index", -1)) == expected_index
            ):
                evidence_path = path.parent / "evidence.json"
                if evidence_path.is_file():
                    evidence = load(evidence_path)
                    if isinstance(evidence, dict) and isinstance(evidence.get("reply"), dict):
                        raise ValueError("interrupted retry has saved adjudication evidence")
            continue
        if data.get("run_id") != run_id or data.get("turn_id") != turn_id:
            continue
        if data.get("invalidated") or _started_marker_has_same_run_pending(row, path.parent, data):
            raise ValueError("interrupted retry has saved adjudication evidence")
        if int(data.get("next_turn_index", -1)) != expected_index:
            continue
        if int(data.get("interaction_index", -1)) != 1:
            raise ValueError("interrupted retry only supports the first Interaction")
        candidates.append((path.stat().st_mtime, path.parent))
    if not candidates:
        return None
    checkpoint = max(candidates)[1]
    _checkpoint_preserves_prefix(row, checkpoint, expected_index)
    return checkpoint


def _started_marker_has_same_run_pending(row, checkpoint, data):
    turn_id = data.get("turn_id")
    turn_index = int(data.get("next_turn_index", -1))
    interaction_index = int(data.get("interaction_index", -1))
    if not (0 <= turn_index < len(row["turns"]) and row["turns"][turn_index] == turn_id):
        return False
    pending = Path(checkpoint).parent / f"p{turn_index:04d}-{interaction_index:02d}"
    metadata_path = pending / "checkpoint.json"
    evidence_path = pending / "evidence.json"
    if not (metadata_path.is_file() and evidence_path.is_file()):
        return False
    if not all((pending / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
        return False
    try:
        metadata = load(metadata_path)
        evidence = load(evidence_path)
        _checkpoint_preserves_prefix(row, pending, turn_index)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return (
        metadata.get("phase") == "adjudicate"
        and metadata.get("stream_id") == row["flow_id"]
        and metadata.get("condition") == row.get("condition")
        and metadata.get("run_id") == data.get("run_id")
        and metadata.get("turn_id") == turn_id
        and int(metadata.get("next_turn_index", -1)) == turn_index
        and int(metadata.get("interaction_index", -1)) == interaction_index
        and evidence.get("turn_id") == turn_id
        and evidence.get("turn_index") == turn_index
        and int(evidence.get("interaction_index", -1)) == interaction_index
        and isinstance(evidence.get("reply"), dict)
    )


def _valid_timeout_recovery_receipt(run_dir, report, turn_id, target_index):
    receipt_path = Path(run_dir) / "timeout-recovery.json"
    if not receipt_path.is_file():
        return False
    try:
        receipt = load(receipt_path)
    except (OSError, ValueError, TypeError):
        return False
    turns = report.get("turns", [])
    if not (isinstance(turns, list) and len(turns) > target_index and isinstance(turns[target_index], dict)):
        return False
    target = turns[target_index]
    return (
        receipt.get("status") == "admitted"
        and receipt.get("turn_id") == turn_id
        and int(receipt.get("turn_index", -1)) == target_index + 1
        and Path(str(receipt.get("source_report", ""))).resolve() == (Path(run_dir) / "report.json").resolve()
        and str(receipt.get("checkpoint", ""))
        and str(report.get("error", "")) == "recovered_interact_timeout: admitted as execution_limit; resume from checkpoint"
        and target.get("turn_id") == turn_id
        and target.get("final_category") == "execution_limit"
        and target.get("evaluation_status") == "failed"
        and target.get("turn_score") == 0.0
        and target.get("evolution", {}).get("completed") is False
        and {"type": "evolution.skipped", "reason": "runtime_unavailable"} in target.get("evolution", {}).get("events", [])
    )


def prepare_interrupted_retry(row, retry_run, authorization_path):
    if row["level"] != "l2":
        raise ValueError("interrupted retry is only supported for L2 rows")
    if row.get("condition") != "reset":
        raise ValueError("interrupted retry is only supported for reset L2 rows")
    if authorization_path is None:
        raise ValueError("--retry-authorization is required with --retry-interrupted-run")
    auth_path = Path(authorization_path).resolve()
    auth = load(auth_path)
    if auth.get("status") != "authorized_not_consumed":
        raise ValueError("interrupted retry authorization is already consumed or invalid")
    if auth.get("job") != row["id"]:
        raise ValueError("interrupted retry authorization job mismatch")
    if int(auth.get("allowed_additional_attempts", 0)) != 1:
        raise ValueError("interrupted retry authorization must allow exactly one attempt")
    run_dir = _run_dir_for_retry(row, retry_run)
    report = load(run_dir / "report.json")
    run_id = str(report.get("run_id", ""))
    turn_id = str(auth.get("turn_id", ""))
    if run_id != str(auth.get("failed_run_id", "")):
        raise ValueError("interrupted retry authorization failed_run_id mismatch")
    if report.get("stream_id") != row["flow_id"] or report.get("baseline") != row["agent"]:
        raise ValueError("interrupted retry report identity mismatch")
    if _report_condition(report, report.get("turns", [])) != row.get("condition"):
        raise ValueError("interrupted retry report condition mismatch")
    if turn_id not in row["turns"]:
        raise ValueError("interrupted retry turn is not in the selected row")
    target_index = row["turns"].index(turn_id)
    turns = report.get("turns", [])
    if _report_matches_row(row, report):
        raise ValueError("interrupted retry would replay a valid completed report")
    valid_receipt = _valid_timeout_recovery_receipt(run_dir, report, turn_id, target_index)
    if len(turns) > target_index and not valid_receipt:
        raise ValueError("interrupted retry would replay an already recorded turn")
    checkpoint = _find_interrupted_checkpoint(row, run_id, turn_id)
    if checkpoint is None:
        preserve_valid_turns = auth.get("preserve_valid_turns", [])
        if not isinstance(preserve_valid_turns, list) or len(preserve_valid_turns) != target_index:
            raise ValueError("legacy interrupted retry requires an authorized completed prefix")
        if str(report.get("error", "")) != "BaselineProtocolError: baseline adapter timed out during interact" and not valid_receipt:
            raise ValueError("interrupted retry report is not an interact interruption")
        checkpoint = _find_completed_prefix_checkpoint(row, target_index)
        if checkpoint is None:
            raise ValueError("no exact completed-prefix checkpoint for interrupted retry")
    consumed = {
        **auth,
        "status": "consumed",
        "consumed_at": now(),
        "consumed_by_pid": os.getpid(),
        "checkpoint": str(checkpoint),
        "source_run_dir": str(run_dir),
        "job": row["id"],
        "stream_id": row["flow_id"],
        "baseline": row["agent"],
        "condition": row["condition"],
    }
    save(auth_path, consumed)
    return checkpoint, run_id, turn_id


def command(repo, row, checkpoint, config_dir=None, *, retry_interrupted_run=None, retry_authorization=None, stop_after_turn=None):
    config_root = Path(config_dir or repo)
    if row["agent"] == "hermes":
        config = f"tmp_hermes_cf_{row['level']}.override.json"
    elif row["agent"] in {"skillopt", "trace2skill"} and row["level"] == "l2":
        config = f"tmp_{row['agent']}_docker_l2.override.json"
        if not (config_root / config).is_file():
            config = f"tmp_{row['agent']}_docker.override.json"
    else:
        config = f"tmp_{row['agent']}_docker.override.json"
    # Stream length must not silently determine the per-turn resource allowance.
    # This is an aggregate guard; model/tool/time limits remain unchanged.
    stream_token_limit = max(10_000_000, 3_000_000 * len(row["turns"]))
    args = [sys.executable, "-m", f"trievo_bench.{row['level']}_" + ("arena_compat" if row["level"] == "l1" else "compat"),
            "--package", row["package"], "--stream", row["stream"], "--output", row["output"],
            "--baseline", row["agent"], "--baseline-config", str(config_root / config),
            "--max-total-tokens", str(stream_token_limit)]
    if row["level"] == "l1":
        args += ["--turns", ",".join(row["turns"]), "--backend", "docker", "--image", "trievo-ubuntu:22.04"]
    else:
        args += ["--condition", row["condition"], "--image", "trievo-l2:0.1", "--browser-image", "trievo-l2-browser:0.1", "--allow-generated-code"]
    if checkpoint:
        args += ["--resume-checkpoint", str(checkpoint)]
    if retry_interrupted_run:
        args += ["--retry-interrupted-run", str(retry_interrupted_run)]
    if retry_authorization:
        args += ["--retry-authorization", str(retry_authorization)]
    if retry_interrupted_run:
        args += ["--retry-job", row["id"]]
    if stop_after_turn:
        args += ["--stop-after-turn", str(stop_after_turn)]
    return args


def l2_reports_to_aggregation_records(report_rows):
    records = []
    seen_conditions = set()
    for row, report in report_rows:
        if row.get("level") != "l2":
            continue
        turns = report.get("turns", [])
        if not _report_matches_row(row, report):
            raise ValueError(f"report does not match row: {row.get('id')}")
        condition = row.get("condition")
        seen_conditions.add(condition)
        roles = row.get("turn_roles", {})
        for index, turn in enumerate(turns, 1):
            turn_id = str(turn["turn_id"])
            stream_id = str(turn.get("stream_id", row["flow_id"]))
            if stream_id != row["flow_id"]:
                raise ValueError(f"stream identity mismatch for {row.get('id')}/{turn_id}")
            sequence_role = turn.get("sequence_role", roles.get(turn_id))
            if not sequence_role:
                raise ValueError(f"missing sequence_role for {row.get('id')}/{turn_id}")
            records.append({
                "condition": condition,
                "stream_id": stream_id,
                "turn_id": turn_id,
                "turn_index": int(turn.get("turn_index", index)),
                "sequence_role": sequence_role,
                "turn_score": float(_turn_score(turn)),
                "evaluation_status": turn.get("evaluation_status", "scored"),
            })
    missing = set(L2_CONDITIONS) - seen_conditions
    if missing:
        raise ValueError(f"missing required L2 condition report(s): {sorted(missing)}")
    return records


def matched_l2_aggregations(rows):
    """Return formal matched aggregates only for fully completed baseline cells."""

    from trievo_bench.evolution_aggregation import aggregate_matched_runs

    result = {}
    for agent in ("hermes", "skillopt", "trace2skill"):
        selected = [row for row in rows if row["level"] == "l2" and row["agent"] == agent]
        if {row.get("condition") for row in selected} != set(L2_CONDITIONS):
            continue
        report_rows = []
        for row in selected:
            _, report = report_for(row)
            if report is None:
                break
            report_rows.append((row, report))
        else:
            result[agent] = aggregate_matched_runs(
                l2_reports_to_aggregation_records(report_rows)
            )
    return result


def matrix(rows):
    cells = []
    for agent in ("hermes", "skillopt", "trace2skill"):
        datasets = [("l1", None)] + [("l2", condition) for condition in L2_CONDITIONS if any(row["level"] == "l2" and row.get("condition") == condition for row in rows)]
        for level, condition in datasets:
            selected = [row for row in rows if row["agent"] == agent and row["level"] == level and row.get("condition") == condition]
            scores = []
            passed = 0
            for row in selected:
                _, report = report_for(row)
                if report:
                    for turn in report["turns"]:
                        scores.append(float(_turn_score(turn)))
                        passed += bool(turn.get("passed"))
            dataset = level if condition is None else f"{level}:{condition}"
            cells.append({"agent": agent, "dataset": dataset, "condition": condition, "completed_turns": len(scores),
                          "expected_turns": sum(len(row["turns"]) for row in selected), "passed_turns": passed,
                          "mean_score": sum(scores) / len(scores) if scores else None})
    return cells


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="runs/mx24/full")
    parser.add_argument("--packages", type=Path, help="legacy parent directory containing l1_arena/ and one L2 package")
    parser.add_argument("--l1-package", type=Path, help="explicit L1 package directory")
    parser.add_argument("--l2-package", type=Path, help="explicit L2 package directory")
    parser.add_argument("--level", choices=("l1", "l2"), help="select one level; default runs both L1 and L2")
    parser.add_argument("--l2-condition", action="append", choices=L2_CONDITIONS, help="L2 condition to run; repeat for reset/frozen/evolving")
    parser.add_argument("--config-dir", type=Path, help="directory generated by prepare_matrix_configs.py")
    parser.add_argument("--only", help="optional exact job id for bounded preflight")
    parser.add_argument("--gate", action="store_true", help="run the three shortest L2 cells before releasing the full matrix")
    parser.add_argument("--retry-interrupted-run", help="exact failed run_id or run/report path for one authorized L2 interrupted-turn retry")
    parser.add_argument("--retry-authorization", type=Path, help="one-shot authorization ledger JSON for --retry-interrupted-run")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if not os.environ.get("TRIEVO_API_KEY"):
        raise SystemExit("TRIEVO_API_KEY is required")
    for name in ("OPENAI_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "TARGET_OPENAI_COMPATIBLE_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ[name] = os.environ["TRIEVO_API_KEY"]
    for name in ("OPENAI_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL", "TARGET_OPENAI_COMPATIBLE_BASE_URL", "DEEPSEEK_BASE_URL"):
        os.environ[name] = os.environ.get("TRIEVO_BASE_URL", "https://cf.api.fan/v1")
    os.environ["TRIEVO_THINKING"] = "disabled"
    # Keep transport retries inside one model call so a relay failure never
    # consumes another benchmark Interaction or repeats workspace side effects.
    os.environ.setdefault("TRIEVO_API_TIMEOUT_SECONDS", "150")
    os.environ.setdefault("TRIEVO_API_MAX_RETRIES", "4")
    root = (repo / args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Retain an OS-owned lock for this process; stale files do not block resume.
    global _driver_lock
    _driver_lock = (root / "driver.lock").open("a+b")
    _driver_lock.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(_driver_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("another matrix driver already holds this output root")
    else:
        import fcntl
        try:
            fcntl.flock(_driver_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another matrix driver already holds this output root")
    all_rows = jobs(
        repo,
        root,
        args.packages.resolve() if args.packages else None,
        l1_package=args.l1_package.resolve() if args.l1_package else None,
        l2_package=args.l2_package.resolve() if args.l2_package else None,
        l2_conditions=tuple(args.l2_condition) if args.l2_condition else None,
        level=args.level,
    )
    config_dir = args.config_dir.resolve() if args.config_dir else repo
    selected = [row for row in all_rows if row["id"] == args.only] if args.only else all_rows
    if args.gate:
        selected = selected[:3]
    if not selected:
        raise SystemExit("no matching job")
    if args.retry_interrupted_run and len(selected) != 1:
        raise SystemExit("--retry-interrupted-run requires one selected job")
    state = {"status": "running", "pid": os.getpid(), "started_at": now(), "jobs": selected}
    status_path = root / "matrix_status.json"
    save(status_path, state)
    for row in selected:
        retry_run_id = None
        stop_after_turn = None
        if args.retry_interrupted_run:
            checkpoint, retry_run_id, stop_after_turn = prepare_interrupted_retry(
                row,
                args.retry_interrupted_run,
                args.retry_authorization,
            )
        else:
            report_path, report = report_for(row)
            if report:
                row.update(status="completed", report=str(report_path))
                continue
            checkpoint = latest_checkpoint(row)
        output = Path(row["output"])
        output.mkdir(parents=True, exist_ok=True)
        log = output / f"driver-{int(time.time())}.log"
        row.update(status="running", started_at=now(), checkpoint=str(checkpoint) if checkpoint else None, log=str(log))
        with log.open("w", encoding="utf-8") as stream:
            child = subprocess.Popen(
                command(
                    repo,
                    row,
                    checkpoint,
                    config_dir,
                    retry_interrupted_run=retry_run_id,
                    retry_authorization=args.retry_authorization,
                    stop_after_turn=stop_after_turn,
                ),
                cwd=repo,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            row["pid"] = child.pid
            state.update(current_job=row["id"], updated_at=now())
            save(status_path, state)
            while child.poll() is None:
                time.sleep(2)
            code = child.returncode
        if stop_after_turn:
            report_path, report = report_for_interrupted_retry(row, stop_after_turn)
        else:
            report_path, report = report_for(row)
        if code != 0 or report is None:
            row.update(status="paused_error", returncode=code, finished_at=now())
            state.update(status="paused_error", updated_at=now(), matrix=matrix(all_rows))
            save(status_path, state)
            print(f"STOPPED {row['id']}: inspect {log}", flush=True)
            return 1
        if stop_after_turn:
            row.update(status="interrupted_retry_completed", report=str(report_path), finished_at=now())
            state.update(status="interrupted_retry_completed", updated_at=now(), matrix=matrix(all_rows))
            save(status_path, state)
            print(f"RETRY_COMPLETED {row['id']} through {stop_after_turn}", flush=True)
            return 0
        row.update(status="completed", report=str(report_path), finished_at=now())
        state.update(updated_at=now(), matrix=matrix(all_rows))
        save(status_path, state)
        print(f"COMPLETED {row['id']}", flush=True)
    aggregations = matched_l2_aggregations(all_rows)
    state.update(
        status="gate_completed" if (args.gate or args.only) else "completed",
        finished_at=now(),
        matrix=matrix(all_rows),
        l2_evolution_aggregations=aggregations,
    )
    save(status_path, state)
    save(root / "result_matrix.json", state["matrix"])
    if aggregations:
        save(root / "l2_evolution_aggregations.json", aggregations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
