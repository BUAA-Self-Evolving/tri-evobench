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


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def jobs(repo: Path, root: Path, packages: Path | None = None):
    if packages is None:
        local_packages = repo.parent / "new-packages-20260903"
        packages = local_packages if local_packages.is_dir() else repo / "benchmarks/current"
    rows = []
    for level, package in (("l2", packages / "L2_Demo_V2"), ("l1", packages / "l1_arena")):
        manifests = sorted((package / "streams").glob("*/stream.json")) if level == "l2" else sorted(package.glob("stream-*/stream.json"), key=lambda p: int(p.parent.name.split("-")[-1]))
        if not manifests:
            raise ValueError(f"No {level} stream manifests found in {package}")
        for index, path in enumerate(manifests, 1):
            manifest = load(path)
            turns = manifest["ordering"]["turn_order"] if level == "l2" else [item["turn_id"] for item in manifest["turns"]]
            for agent in ("hermes", "skillopt", "trace2skill"):
                rows.append({"id": f"{level}/{agent}/s{index:02d}", "level": level, "agent": agent,
                             "package": str(package), "stream": path.parent.name,
                             "flow_id": manifest["stream_id"], "turns": turns,
                             "output": str(root / level / agent / f"s{index:02d}")})
    return rows


def report_for(row):
    output = Path(row["output"])
    candidates = list(output.glob("*/summary.json")) if row["level"] == "l1" else list(output.glob("*/*/report.json"))
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        report = load(path)
        turns = report.get("turns", [])
        if [turn.get("turn_id") for turn in turns] == row["turns"] and not report.get("error") and not report.get("cleanup_error"):
            if any(turn.get("final_category") == "error" for turn in turns):
                continue
            return path, report
    return None, None


def latest_checkpoint(row):
    candidates = []
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("invalidated"):
            continue
        if data.get("stream_id", data.get("flow_id")) != row["flow_id"]:
            continue
        if all((path.parent / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
            candidates.append((int(data["next_turn_index"]), path.stat().st_mtime, path.parent))
    return max(candidates)[2] if candidates else None


def command(repo, row, checkpoint, config_dir=None):
    config = f"tmp_hermes_cf_{row['level']}.override.json" if row["agent"] == "hermes" else f"tmp_{row['agent']}_docker.override.json"
    # Stream length must not silently determine the per-turn resource allowance.
    # This is an aggregate guard; model/tool/time limits remain unchanged.
    stream_token_limit = max(10_000_000, 3_000_000 * len(row["turns"]))
    args = [sys.executable, "-m", f"trievo_bench.{row['level']}_" + ("arena_compat" if row["level"] == "l1" else "compat"),
            "--package", row["package"], "--stream", row["stream"], "--output", row["output"],
            "--baseline", row["agent"], "--baseline-config", str((config_dir or repo) / config),
            "--max-total-tokens", str(stream_token_limit)]
    if row["level"] == "l1":
        args += ["--turns", ",".join(row["turns"]), "--backend", "docker", "--image", "trievo-ubuntu:22.04"]
    else:
        args += ["--image", "trievo-l2:0.1", "--browser-image", "trievo-l2-browser:0.1", "--allow-generated-code"]
    if checkpoint:
        args += ["--resume-checkpoint", str(checkpoint)]
    return args


def matrix(rows):
    cells = []
    for agent in ("hermes", "skillopt", "trace2skill"):
        for level in ("l1", "l2"):
            selected = [row for row in rows if row["agent"] == agent and row["level"] == level]
            scores = []
            passed = 0
            for row in selected:
                _, report = report_for(row)
                if report:
                    for turn in report["turns"]:
                        scores.append(float(turn.get("final_score", turn.get("score", 0))))
                        passed += bool(turn.get("passed"))
            cells.append({"agent": agent, "dataset": level, "completed_turns": len(scores),
                          "expected_turns": sum(len(row["turns"]) for row in selected), "passed_turns": passed,
                          "mean_score": sum(scores) / len(scores) if scores else None})
    return cells


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="runs/mx24/full")
    parser.add_argument("--packages", type=Path, help="directory containing l1_arena/ and L2_Demo_V2/")
    parser.add_argument("--config-dir", type=Path, help="directory generated by prepare_matrix_configs.py")
    parser.add_argument("--only", help="optional exact job id for bounded preflight")
    parser.add_argument("--gate", action="store_true", help="run the three shortest L2 cells before releasing the full matrix")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if not os.environ.get("TRIEVO_API_KEY"):
        raise SystemExit("TRIEVO_API_KEY is required")
    for name in ("OPENAI_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "TARGET_OPENAI_COMPATIBLE_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ[name] = os.environ["TRIEVO_API_KEY"]
    for name in ("OPENAI_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL", "TARGET_OPENAI_COMPATIBLE_BASE_URL", "DEEPSEEK_BASE_URL"):
        os.environ[name] = os.environ.get("TRIEVO_BASE_URL", "https://cf.api.fan/v1")
    os.environ["TRIEVO_THINKING"] = "disabled"
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
    all_rows = jobs(repo, root, args.packages.resolve() if args.packages else None)
    config_dir = args.config_dir.resolve() if args.config_dir else repo
    selected = [row for row in all_rows if row["id"] == args.only] if args.only else all_rows
    if args.gate:
        selected = selected[:3]
    if not selected:
        raise SystemExit("no matching job")
    state = {"status": "running", "pid": os.getpid(), "started_at": now(), "jobs": selected}
    status_path = root / "matrix_status.json"
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
            code = child.returncode
        report_path, report = report_for(row)
        if code != 0 or report is None:
            row.update(status="paused_error", returncode=code, finished_at=now())
            state.update(status="paused_error", updated_at=now(), matrix=matrix(all_rows))
            save(status_path, state)
            print(f"STOPPED {row['id']}: inspect {log}", flush=True)
            return 1
        row.update(status="completed", report=str(report_path), finished_at=now())
        state.update(updated_at=now(), matrix=matrix(all_rows))
        save(status_path, state)
        print(f"COMPLETED {row['id']}", flush=True)
    state.update(status="gate_completed" if (args.gate or args.only) else "completed", finished_at=now(), matrix=matrix(all_rows))
    save(status_path, state)
    save(root / "result_matrix.json", state["matrix"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
