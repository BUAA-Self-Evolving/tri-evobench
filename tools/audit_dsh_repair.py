"""Read-only composite evidence audit for the original matrix and repair cohort."""
import collections
import json
from pathlib import Path

from tools.prepare_dsh_repair_cohort import ORIGINAL, REPAIR
from tools.run_new_package_matrix import load, save, now, report_for, process_start_ticks


def main():
    original = load(ORIGINAL / "matrix_status.json")
    state = load(REPAIR / "matrix_status.json")
    repairs = {row["id"]: row for row in state["jobs"]}
    cells, workers = [], []
    for old in original["jobs"]:
        path, report = report_for(old)
        row, source = old, "original"
        if old["id"] in repairs:
            if path:
                raise ValueError("repair cohort unexpectedly includes original matching cell")
            row, source = repairs[old["id"]], "repair"
            path, report = report_for(row)
        cells.append({"job_id": row["id"], "source": source, "matching": path is not None,
                      "report": str(path) if path else None, "status": row["status"]})
        if row.get("status") == "running":
            files = [p for pattern in ("checkpoint.json", "live_trace.jsonl")
                     for p in Path(row["output"]).rglob(pattern)]
            latest = max(files, key=lambda p: p.stat().st_mtime) if files else None
            workers.append({"job_id": row["id"], "pid": row.get("pid"),
                "birth_matches": bool(row.get("pid")) and process_start_ticks(row["pid"]) == row.get("process_start_ticks"),
                "slot": row.get("credential_slot"), "slot_env_name": row.get("credential_env"),
                "last_durable_evidence": str(latest) if latest else None,
                "evidence_age_seconds": max(0, __import__("time").time() - latest.stat().st_mtime) if latest else None})
    audit = {"at": now(), "expected": 87, "matching": sum(c["matching"] for c in cells),
             "repair_status_counts": dict(collections.Counter(r["status"] for r in repairs.values())),
             "driver_pid": state["pid"], "driver_birth": process_start_ticks(state["pid"]),
             "workers": workers, "cells": cells,
             "final": all(c["matching"] for c in cells),
             "selection": "original64 plus predeclared repair23; never choose best score or splice fresh prefixes"}
    save(REPAIR / "composite_audit.json", audit)
    print(json.dumps({k: v for k, v in audit.items() if k not in {"cells", "selection"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
