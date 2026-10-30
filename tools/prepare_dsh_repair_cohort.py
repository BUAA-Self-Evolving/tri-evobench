"""Prepare a separate, once-only repair cohort without editing original evidence."""
from __future__ import annotations

import fcntl
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tools.run_new_package_matrix import load, save, now, report_for, latest_checkpoint, process_start_ticks
from tools.recover_dsh_reset_exceptions import digest, derive

ORIGINAL = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
REPAIR = REPO / "runs/dsh-deepseek-v4-flash-repair-20261015"


def claim_fresh(row, path):
    with Path(path).open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = json.load(stream)
        if (auth.get("status") != "authorized" or auth.get("max_attempts") != 1
                or auth.get("job_id") != row["id"]
                or auth.get("output") != str(Path(row["output"]).resolve())):
            raise ValueError("fresh repair authorization consumed or mismatched")
        if any(Path(row["output"]).rglob("checkpoint.json")):
            raise ValueError("fresh repair must not replay an existing checkpoint")
        for name, expected in auth["source_sha256"].items():
            if digest(name) != expected:
                raise ValueError("original repair evidence changed")
        auth.update(status="consumed", claimed_at=now())
        stream.seek(0)
        stream.truncate()
        json.dump(auth, stream, ensure_ascii=False, indent=2)


def prepare():
    with (ORIGINAL / "driver.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load(ORIGINAL / "matrix_status.json")
        if process_start_ticks(state.get("pid", 0)) or any(
                process_start_ticks(row.get("pid", 0)) for row in state["jobs"]):
            raise ValueError("original driver or worker still alive")
        if REPAIR.exists():
            raise ValueError("repair root already exists; inspect instead of preparing twice")
        plan = load(ORIGINAL / "run_plan.json")
        unfinished = [row for row in state["jobs"] if not report_for(row)[0]]
        if len(unfinished) != 23 or any(row.get("status") not in {"paused_error", "needs_attention"}
                                      for row in unfinished):
            raise ValueError("expected exactly 23 settled exceptional cells")
        REPAIR.mkdir()
        (REPAIR / "config").mkdir()
        for name in ("provider-profile.json", "tmp_deepseek-harness_docker.override.json"):
            shutil.copyfile(ORIGINAL / "config" / name, REPAIR / "config" / name)
        rows, records = [], []
        originals = {row["id"]: row for row in plan["jobs"]}
        for old in unfinished:
            row = dict(originals[old["id"]])
            output = REPAIR / row["id"]
            output.mkdir(parents=True)
            row.update(output=str(output), status="pending")
            sources = {str(ORIGINAL / "run_plan.json"): digest(ORIGINAL / "run_plan.json")}
            for name in ("checkpoint.json", "live_trace.jsonl", "report.json"):
                for path in Path(old["output"]).rglob(name):
                    sources[str(path)] = digest(path)
            if row["id"] == "l2/evolving/deepseek-harness/s05":
                frontiers = [(p, load(p)) for p in Path(old["output"]).rglob("checkpoint.json")]
                frontiers = [(p, m) for p, m in frontiers if m.get("turn_id") == row["turns"][88]
                             and m.get("phase") == "interact_started" and not m.get("invalidated")]
                if len(frontiers) != 1:
                    raise ValueError("turn89 needs one reply-free frontier")
                frontier, meta = frontiers[0]
                copied = output / "_checkpoints" / "source_turn89"
                shutil.copytree(frontier.parent, copied)
                run = Path(old["output"]) / row["flow_id"] / meta["run_id"]
                copied_run = output / row["flow_id"] / meta["run_id"]
                copied_run.mkdir(parents=True)
                for name in ("report.json", "error_workspace.zip", "baseline_state.zip"):
                    shutil.copyfile(run / name, copied_run / name)
                    sources[str(run / name)] = digest(run / name)
                row.update(status="paused_error", log=old["log"])
                checkpoint = derive(row, {"cause": "terminal_friday_429", "failed_turn": row["turns"][88]},
                                    REPAIR, terminal_stateful=True)
                if latest_checkpoint(row) != checkpoint:
                    raise ValueError("strict selector did not admit turn90 continuation")
                row.update(status="pending", checkpoint=str(checkpoint))
                row.pop("log")
                kind = "resume_turn90_preserve88_score_exception89_zero"
            else:
                save(output / "fresh_run_authorization.json", {
                    "status": "authorized", "max_attempts": 1, "job_id": row["id"],
                    "output": str(output), "created_at": now(), "source_sha256": sources,
                    "authorization": "user: 想办法修复这23格 尽量跑完全量",
                    "semantics": "independent complete replacement attempt; never splice old prefix",
                })
                kind = "independent_complete_replacement_once"
            rows.append(row)
            records.append({"job_id": row["id"], "kind": kind, "original_output": old["output"],
                            "repair_output": str(output), "source_sha256": sources})
        save(REPAIR / "job_ids.json", [r["id"] for r in rows])
        save(REPAIR / "matrix_status.json", {"status": "prepared", "pid": 0, "jobs": rows})
        save(REPAIR / "run_plan.json", {
            "created_at": now(), "original_root": str(ORIGINAL), "expected_cells": 23,
            "original_expected_cells": 87, "original_expected_turns": 1671,
            "fresh_cells": 22, "continuation_cells": 1, "max_attempts_per_cell": 1,
            "slot_order": [2, 3, 4, 5, 6, 1], "model": "deepseek-v4-flash",
            "harness_learning": plan["harness_learning"], "dataset": "L2_Demo_V2; V5 absent",
            "aggregation": "original 64 matching reports plus complete repair reports; no partial splicing",
            "jobs": rows, "provenance": records,
        })
        print(json.dumps({"root": str(REPAIR), "fresh_cells": 22, "resume_cells": 1,
                          "new_turns_requested": sum(len(r["turns"]) for r in rows) - 89}))


if __name__ == "__main__":
    prepare()
