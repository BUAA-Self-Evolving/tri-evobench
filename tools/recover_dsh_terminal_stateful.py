"""Admit only terminal native failures with verified post-error state, once."""
import fcntl
import json

from tools.recover_dsh_reset_exceptions import REPO, derive, write_json
from tools.run_new_package_matrix import latest_checkpoint, process_start_ticks, save


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    with (root / "driver.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((root / "matrix_status.json").read_text())
        if process_start_ticks(state["pid"]) or any(x["status"] == "running" for x in state["jobs"]):
            raise ValueError("driver/workers still active")
        audit = json.loads((root / "monitor_audit.json").read_text())
        admitted, rejected = [], []
        for row in state["jobs"]:
            if row["status"] != "paused_error" or row["level"] != "l2":
                continue
            evidence = next(x for x in audit["needs_attention"] if x["job_id"] == row["id"])
            try:
                checkpoint = derive(row, evidence, root, terminal_stateful=True)
                if latest_checkpoint(row) != checkpoint:
                    raise ValueError("strict next-turn checkpoint selector rejected")
            except ValueError as exc:
                rejected.append({"job_id": row["id"], "reason": str(exc)})
                continue
            row.update(status="pending", checkpoint=str(checkpoint),
                       recovery_mode="paid_exception_zero_then_stateful_next_turn")
            admitted.append(row["id"])
        save(root / "matrix_status.json", state)
        write_json(root / "terminal_stateful_recovery_audit.json", {"admitted": admitted, "rejected": rejected})
        print(json.dumps({"admitted": admitted, "rejected": rejected}))


if __name__ == "__main__":
    main()
