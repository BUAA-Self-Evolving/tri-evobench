"""Select errored cells for a separate, once-only replacement pass.

Completed reports are never selected, including workers that exited nonzero
only after writing a complete matching report. Raw attempts stay immutable.
"""
from pathlib import Path
from tools.run_new_package_matrix import report_for


def collect_failed_cells(attempts):
    latest = {}
    completed = set()
    for matrix_path, rows in attempts:
        for row in rows:
            try:
                _, report = report_for(row)
            except Exception:
                report = None
            if row.get("status") == "completed" or report is not None:
                completed.add(row["id"])
                latest.pop(row["id"], None)
            elif row.get("status") in {"paused_error", "needs_attention", "blocked_recovery"} and row["id"] not in completed:
                latest[row["id"]] = {
                    "id": row["id"],
                    "source_matrix": str(Path(matrix_path).resolve()),
                    "source_output": row["output"],
                    "source_log": row.get("log"),
                    "replacement_scope": "full_failed_cell_in_fresh_root",
                    "max_replacement_attempts": 1,
                }
    return [latest[key] for key in sorted(latest)]
