"""Record exhausted one-shot 429 continuation without changing raw evidence."""
import fcntl
import json
from pathlib import Path

from tools.recover_dsh_reset_exceptions import REPO, digest, write_json
from tools.run_new_package_matrix import process_start_ticks, save


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    with (root / "driver.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((root / "matrix_status.json").read_text())
        if process_start_ticks(state["pid"]) or any(j["status"] == "running" for j in state["jobs"]):
            raise ValueError("driver/workers still active")
        row = next(j for j in state["jobs"] if j["id"] == "l2/evolving/deepseek-harness/s05")
        ledger_path = root / "scored_exception_authorization_l2_evolving_deepseek-harness_s05_terminal_friday_429.json"
        ledger = json.loads(ledger_path.read_text())
        if ledger["status"] != "consumed":
            raise ValueError("one-shot authorization not consumed")
        report_path = max(Path(row["output"]).glob("*/*/report.json"), key=lambda p: p.stat().st_mtime)
        report = json.loads(report_path.read_text())
        marker = "DeepSeek Harness turn failed: error: Friday HTTP 429; no relay retry"
        if marker not in report.get("error", "") or [t["turn_id"] for t in report["turns"]] != row["turns"][:88]:
            raise ValueError("expected recurrent terminal 429 after 88 preserved turns")
        frontiers = []
        for path in Path(row["output"]).rglob("checkpoint.json"):
            meta = json.loads(path.read_text())
            if meta.get("run_id") == report["run_id"] and meta.get("turn_id") == "turn89":
                if meta.get("phase") in {"reply_received", "adjudicate"}:
                    raise ValueError("saved reply must not be counted as missing")
                if meta.get("phase") == "interact_started":
                    frontiers.append(path)
        if len(frontiers) != 1:
            raise ValueError("exact failed request frontier required")
        attention = {"job_id": row["id"], "status": "needs_attention",
            "cause": "terminal_friday_429", "recovery_exhausted": True,
            "matching_complete": False, "provider_requests_replayed": 0,
            "preserved_prefix_turns": 88, "exception_score_overlay": {"turn89": 0.0},
            "pending_turns": row["turns"][89:],
            "scoring": "preserve prefix scores; only exceptional turn89 zero; unrun turns pending",
            "source_sha256": {str(p): digest(p) for p in
                (report_path, frontiers[0], Path(row["log"]), ledger_path)}}
        destination = root / "rate_limit_exhausted_needs_attention.json"
        write_json(destination, attention)
        row.update(status="needs_attention", recovery_reason="terminal Friday 429 recurred after one cause-specific continuation",
                   recovery_exhausted=True, attention_record=str(destination))
        save(root / "matrix_status.json", state)
        print(json.dumps({"preserved_turns": 88, "exception_zero": "turn89", "pending_turns": 11,
                          "additional_retries": 0, "matching_complete": False}))


if __name__ == "__main__":
    main()
