"""Detach the approved launcher from the calling terminal's process group."""
import fcntl
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repair", action="store_true")
    group.add_argument("--opencode-matched", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = repo / "runs/dsh-deepseek-v4-flash-full-20261011"
    extra = []
    launcher_flags = []
    if args.opencode_matched:
        from tools.prepare_dsh_opencode_matched import ROOT, ORIGINAL, REPAIR
        from tools.run_new_package_matrix import load, process_start_ticks
        # Only inspect our three cohorts. Unrelated Docker resources are retained.
        for cohort in (ORIGINAL, REPAIR, ROOT):
            with (cohort / 'driver.lock').open('a+b') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SystemExit('existing cohort driver still owns its lock')
            state = load(cohort / 'matrix_status.json')
            for row in [state, *state['jobs']]:
                pid = row.get('pid', 0)
                if process_start_ticks(pid):
                    command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True)
                    if 'trievo_bench.' in command or 'run_new_package_matrix.py' in command:
                        raise SystemExit('existing cohort driver or worker still alive')
        root = ROOT
        from tools.run_new_package_matrix import report_for
        state = load(root / 'matrix_status.json')
        def authorized_pending(row):
            if row.get('status', 'pending') != 'pending' or report_for(row)[0] is not None:
                return False
            if load(Path(row['output']) / 'fresh_run_authorization.json').get('status') == 'authorized':
                return True
            checkpoint = row.get('checkpoint')
            if checkpoint:
                meta = load(Path(checkpoint) / 'checkpoint.json')
                ledger = meta.get('scored_exception_ledger')
                return bool(ledger and load(Path(ledger)).get('status') == 'authorized')
            return False
        safe = [row for row in state['jobs'] if authorized_pending(row)]
        if not safe:
            raise SystemExit('no safely authorized pending cell; inspect evidence')
        launcher_flags = ['--opencode-matched']
        extra = ['--root', str(root), '--config-dir', str(root / 'config')]
    if args.repair:
        from tools.prepare_dsh_repair_cohort import REPAIR, ORIGINAL
        from tools.run_new_package_matrix import load, process_start_ticks
        state = load(ORIGINAL / "matrix_status.json")
        if process_start_ticks(state.get("pid", 0)) or any(
                process_start_ticks(row.get("pid", 0)) for row in state["jobs"]):
            raise SystemExit("original driver or worker still alive")
        root = REPAIR
        extra = ["--root", str(root), "--config-dir", str(root / "config"),
                 "--job-ids-file", str(root / "job_ids.json"),
                 "--friday-slot-order", "2,3,4,5,6,1"]
    with (root / "driver.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("an existing driver owns this output root")
    with (root / "driver.log").open("a") as log:
        process = subprocess.Popen(["zsh", str(repo / "tools/start_dsh_friday.zsh"), *launcher_flags, *extra],
            cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    print(json.dumps({"detached_driver_pid": process.pid, "root": str(root)}))


if __name__ == "__main__":
    main()
