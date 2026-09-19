"""Run the matched L2 reset/frozen control matrix with exact-turn resume."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trievo_bench.provider_profiles import (
    PROVIDER_PROFILES,
    configure_provider_environment,
    validate_generated_profile,
)


AGENTS = ("hermes", "skillopt", "trace2skill")
CONDITIONS = ("reset", "frozen")
EXCLUDED_STREAMS = {"sra_bigcodebench_pandas_dataframe_viz_001"}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def jobs(repo: Path, root: Path, package: Path):
    rows = []
    manifests = sorted((package / "streams").glob("*/stream.json"))
    manifests = [path for path in manifests if path.parent.name not in EXCLUDED_STREAMS]
    if len(manifests) != 5:
        raise ValueError(f"expected 5 non-SRA L2 streams, found {len(manifests)}")
    for stream_index, path in enumerate(manifests, 1):
        manifest = load(path)
        turns = manifest["ordering"]["turn_order"]
        for condition in CONDITIONS:
            for agent in AGENTS:
                rows.append({
                    "id": f"{condition}/{agent}/s{stream_index:02d}",
                    "condition": condition,
                    "agent": agent,
                    "package": str(package),
                    "stream": path.parent.name,
                    "flow_id": manifest["stream_id"],
                    "turns": turns,
                    "output": str(root / condition / agent / f"s{stream_index:02d}"),
                })
    return rows


def report_for(row):
    output = Path(row["output"])
    for path in sorted(output.glob("*/*/report.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        report = load(path)
        turns = report.get("turns", [])
        if report.get("condition") != row["condition"]:
            continue
        if [turn.get("turn_id") for turn in turns] != row["turns"]:
            continue
        if report.get("error") or report.get("cleanup_error"):
            continue
        if any(turn.get("final_category") == "error" for turn in turns):
            continue
        return path, report
    return None, None


def latest_checkpoint(row):
    candidates = []
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("invalidated") or data.get("stream_id") != row["flow_id"]:
            continue
        if data.get("baseline") != row["agent"] or data.get("condition") != row["condition"]:
            continue
        if all((path.parent / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
            candidates.append((int(data["next_turn_index"]), path.stat().st_mtime, path.parent))
    return max(candidates)[2] if candidates else None


def command(repo: Path, row, checkpoint: Path | None, config_dir: Path):
    config = "tmp_hermes_cf_l2.override.json" if row["agent"] == "hermes" else f"tmp_{row['agent']}_docker.override.json"
    token_limit = max(10_000_000, 3_000_000 * len(row["turns"]))
    args = [
        sys.executable, "-m", "trievo_bench.l2_compat",
        "--package", row["package"], "--stream", row["stream"],
        "--output", row["output"], "--baseline", row["agent"],
        "--baseline-config", str(config_dir / config),
        "--condition", row["condition"],
        "--max-total-tokens", str(token_limit),
        "--image", "trievo-l2:0.1", "--browser-image", "trievo-l2-browser:0.1",
        "--allow-generated-code",
    ]
    if checkpoint:
        args += ["--resume-checkpoint", str(checkpoint)]
    return args


def aggregate(rows):
    cells = []
    for condition in CONDITIONS:
        for agent in AGENTS:
            selected = [row for row in rows if row["condition"] == condition and row["agent"] == agent]
            turns = []
            reports = []
            for row in selected:
                _, report = report_for(row)
                if report:
                    reports.append(report)
                    turns.extend(report["turns"])
            cells.append({
                "condition": condition,
                "agent": agent,
                "completed_streams": len(reports),
                "expected_streams": len(selected),
                "completed_turns": len(turns),
                "expected_turns": sum(len(row["turns"]) for row in selected),
                "passed_turns": sum(bool(turn.get("passed")) for turn in turns),
                "mean_score": sum(float(turn.get("final_score", 0)) for turn in turns) / len(turns) if turns else None,
                "tokens": sum(int(report.get("token_budget", {}).get("used", 0)) for report in reports),
                "api_calls": sum(int(report.get("token_budget", {}).get("calls", 0)) for report in reports),
            })
    return cells


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="runs/mx25/l2-controls")
    parser.add_argument("--package", type=Path, default=Path("benchmarks/current/L2_Demo_V4"))
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--provider-mode", choices=sorted(PROVIDER_PROFILES), default="packycode")
    parser.add_argument("--gate", action="store_true", help="run both conditions and all agents on the shortest stream")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        configure_provider_environment(args.provider_mode, os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    root = (repo / args.root).resolve()
    package = (repo / args.package).resolve() if not args.package.is_absolute() else args.package.resolve()
    config_dir = args.config_dir.resolve() if args.config_dir else repo
    try:
        validate_generated_profile(config_dir, args.provider_mode)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    root.mkdir(parents=True, exist_ok=True)
    global _driver_lock
    _driver_lock = (root / "driver.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        try: msvcrt.locking(_driver_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: raise SystemExit("another driver holds this output root")
    else:
        import fcntl
        try: fcntl.flock(_driver_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: raise SystemExit("another driver holds this output root")
    all_rows = jobs(repo, root, package)
    selected = [row for row in all_rows if row["stream"] == "airs_sick_textual_entailment_accuracy_001"] if args.gate else all_rows
    status_path = root / "matrix_status.json"
    state = {"status": "running", "pid": os.getpid(), "started_at": now(), "gate": args.gate,
             "provider_mode": args.provider_mode, "jobs": selected}
    save(status_path, state)
    for row in selected:
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
            child = subprocess.Popen(command(repo, row, checkpoint, config_dir), cwd=repo, stdout=stream, stderr=subprocess.STDOUT)
            row["pid"] = child.pid
            state.update(current_job=row["id"], updated_at=now())
            save(status_path, state)
            while child.poll() is None:
                time.sleep(2)
        report_path, report = report_for(row)
        if child.returncode != 0 or report is None:
            row.update(status="paused_error", returncode=child.returncode, finished_at=now())
            state.update(status="paused_error", updated_at=now(), matrix=aggregate(all_rows))
            save(status_path, state)
            print(f"STOPPED {row['id']}: inspect {log}", flush=True)
            return 1
        row.update(status="completed", report=str(report_path), finished_at=now())
        state.update(updated_at=now(), matrix=aggregate(all_rows))
        save(status_path, state)
        print(f"COMPLETED {row['id']}", flush=True)
    state.update(status="gate_completed" if args.gate else "completed", finished_at=now(), matrix=aggregate(all_rows))
    save(status_path, state)
    save(root / "result_matrix.json", state["matrix"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
