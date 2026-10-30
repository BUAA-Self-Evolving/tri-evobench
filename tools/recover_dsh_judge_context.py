"""One bounded saved-reply adjudication recovery; never call the Agent again."""
import fcntl
import json
import shutil

from tools.recover_dsh_reset_exceptions import REPO, digest, write_json
from tools.run_new_package_matrix import latest_checkpoint, process_start_ticks, save


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    with (root / "driver.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((root / "matrix_status.json").read_text())
        if process_start_ticks(state["pid"]) or any(j["status"] == "running" for j in state["jobs"]):
            raise ValueError("driver/workers still active")
        row = next(j for j in state["jobs"] if j["id"] == "l2/evolving/deepseek-harness/s05")
        ledger = root / "judge_context_authorization_l2_evolving_deepseek-harness_s05.json"
        if ledger.exists() or row["status"] != "paused_error":
            raise ValueError("already authorized or source not paused")
        source = latest_checkpoint(row)
        meta = json.loads((source / "checkpoint.json").read_text())
        evidence = json.loads((source / "evidence.json").read_text())
        prefix = json.loads((source / "completed_turns.json").read_text())
        if not (meta["phase"] == "adjudicate" and meta["turn_id"] == "turn61"
                and meta["next_turn_index"] == 60 and meta["interaction_index"] == 1
                and evidence["turn_id"] == "turn61" and evidence["reply"]["text"]
                and [t["turn_id"] for t in prefix] == row["turns"][:60]):
            raise ValueError("saved-reply boundary/prefix mismatch")
        # Resolve the report against the exact job output and run identity.
        from pathlib import Path
        report_path = Path(row["output"]) / row["flow_id"] / meta["run_id"] / "report.json"
        report = json.loads(report_path.read_text())
        if ("maximum context length" not in report.get("error", "")
                or report["turns"][:60] != prefix or report.get("cleanup_error")
                or report["turns"][-1]["evaluation_status"] != "evaluation_error"):
            raise ValueError("not the exact context-limit evaluation failure")
        names = ("checkpoint.json", "completed_turns.json", "workspace.zip", "baseline_state.zip", "evidence.json")
        source_hashes = {str(source / name): digest(source / name) for name in names}
        source_hashes[str(report_path)] = digest(report_path)
        target = source.parent / "judge_context_p0060-01"
        if target.exists():
            raise ValueError("derived checkpoint already exists")
        target.mkdir()
        for name in names:
            shutil.copyfile(source / name, target / name)
        meta.update(scored_exception_ledger=str(ledger.resolve()),
                    recovery_kind="saved_reply_judge_context_once", provider_requests_replayed=0)
        write_json(target / "checkpoint.json", meta)
        if latest_checkpoint(row) != target:
            raise ValueError("strict selector rejected derived adjudication")
        write_json(ledger, {"status": "authorized", "job_id": row["id"], "condition": "evolving",
            "max_attempts": 1, "provider_requests_replayed": 0, "checkpoint": str(target.resolve()),
            "source_sha256": source_hashes, "checkpoint_sha256": {n: digest(target / n) for n in names},
            "cause": "judge_context_limit_from_unchanged_workspace_inventory",
            "agent_reply_preserved": True, "completed_prefix_turns": 60,
            "repair": "omit unchanged workspace digests from judge prompt; preserve all changes and full raw evidence"})
        row.update(status="pending", checkpoint=str(target), recovery_mode="saved_reply_judge_context_once")
        save(root / "matrix_status.json", state)
        print(json.dumps({"admitted": row["id"], "saved_reply_turn": "turn61", "agent_requests_replayed": 0}))


if __name__ == "__main__":
    main()
