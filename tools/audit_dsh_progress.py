"""Credential-free monitor; never edits matrix state, reports or checkpoints."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tools.run_new_package_matrix import report_for, process_start_ticks


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    plan = json.loads((root / "run_plan.json").read_text())
    state = json.loads((root / "matrix_status.json").read_text())
    strict_path = REPO.parent / "tri-evobench-offline-opencode-dcbcc00/tools/recover_reset_interact_timeout.py"
    spec = importlib.util.spec_from_file_location("strict_recovery", strict_path)
    strict = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strict)
    attention, active, matching = [], [], []
    for row in state["jobs"]:
        report_path, report = report_for(row)
        if report_path:
            matching.append(row["id"])
        if row.get("status") == "running":
            output = Path(row["output"])
            checkpoints = list(output.rglob("checkpoint.json"))
            traces = list(output.rglob("live_trace.jsonl"))
            files = checkpoints + traces
            last = max(files, key=lambda p: p.stat().st_mtime) if files else None
            active.append({"job_id": row["id"], "slot": row["credential_slot"],
                           "pid": row["pid"], "birth_matches": process_start_ticks(row["pid"]) == row.get("process_start_ticks"),
                           "last_durable_evidence": str(last) if last else None,
                           "evidence_age_seconds": int(datetime.now().timestamp() - last.stat().st_mtime) if last else None})
        if row.get("status") not in {"paused_error", "needs_attention"}:
            continue
        output = Path(row["output"])
        log = Path(row["log"]).read_text()
        if "DeepSeek Harness turn failed: error: Friday HTTP 429; no relay retry" in log:
            cause = "terminal_friday_429"
        elif "DeepSeek Harness turn failed: max-tokens:" in log:
            cause = "native_max_tokens_discarded_by_legacy_adapter"
        elif "baseline adapter timed out during interact" in log:
            cause = "adapter_watchdog_timeout_not_yet_proven_execution_limit"
        else:
            cause = "unclassified_requires_evidence"
        reports = sorted(output.glob("*/*/report.json"), key=lambda p: p.stat().st_mtime)
        source = json.loads(reports[-1].read_text()) if reports else None
        classification = None
        if source:
            try:
                classification = strict._classify_limit(source)[0]
            except ValueError as exc:
                classification = "strict_recovery_rejected: " + str(exc)
        phases = []
        for path in sorted(output.rglob("checkpoint.json")):
            data = json.loads(path.read_text())
            phases.append({"checkpoint": str(path.parent), **{key: data.get(key) for key in (
                "phase", "turn_id", "run_id", "interaction_index", "next_turn_index", "invalidated")}})
        trace_rows = []
        for path in output.rglob("live_trace.jsonl"):
            for line in path.read_text().splitlines():
                if line.strip():
                    data = json.loads(line)
                    trace_rows.append({"turn_id": data.get("turn_id"), "interaction_index": data.get("index"),
                                       "has_reply": bool(data.get("response")), "score": data.get("score"),
                                       "error_type": data.get("verifier_details", {}).get("type")})
        completed = [turn["turn_id"] for turn in source.get("turns", [])] if source else []
        failed_turn = next((p["turn_id"] for p in reversed(phases) if p["phase"] == "interact_started" and not p["invalidated"]), None)
        if not failed_turn and trace_rows:
            failed_turn = trace_rows[-1]["turn_id"]
        attention.append({"job_id": row["id"], "status": "needs_attention",
                          "recovery_exhausted": row.get("recovery_exhausted", False),
                          "cause": cause, "strict_classification": classification,
                          "failed_turn": failed_turn, "failed_role": row.get("turn_roles", {}).get(failed_turn),
                          "completed_prefix": completed, "checkpoint_boundaries": phases,
                          "trace_evidence": trace_rows,
                          "provider_requests_replayed": 0,
                          "retry_authorizations": [str(p) for p in root.glob("*authorization*.json") if row["id"].replace("/", "_") in p.name],
                          "disposition": "preserve paid attempts; recover only without replay; remaining turns pending, not zero"})
    audit = {"at": datetime.now(timezone.utc).isoformat(), "expected_cells": plan["expected_cells"],
             "expected_turns": plan["expected_turns"], "matching_cells": len(matching),
             "driver_pid": state["pid"], "driver_birth": process_start_ticks(state["pid"]),
             "active": active, "needs_attention": attention,
             "repair": {"native_limit_result_preserved": True, "new_worker_output_cap": 16384,
                        "image": "trievo-deepseek-harness:friday-20261011-deadline-v3",
                        "native_notification_wait_bounded": True,
                        "dsh_interaction_transport_grace_seconds": 60,
                        "deadline_is_exception_not_execution_limit": True,
                        "existing_workers_restarted": False}}
    destination = root / "monitor_audit.json"
    destination.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"matching": len(matching), "active": len(active), "needs_attention": len(attention),
                      "all_worker_births_match": all(p["birth_matches"] for p in active), "audit": str(destination)}))


if __name__ == "__main__":
    main()
