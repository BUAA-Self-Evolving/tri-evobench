"""Non-final matched-cell summary; never zero-fill pending probes/cells."""
import collections
import json
from datetime import datetime, timezone

from tools.recover_dsh_reset_exceptions import REPO, write_json
from tools.run_new_package_matrix import report_for, _turn_score


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    state = json.loads((root / "matrix_status.json").read_text())
    repair_root = root.parent / "dsh-deepseek-v4-flash-repair-20261015"
    if (repair_root / "matrix_status.json").is_file():
        repairs = {r["id"]: r for r in json.loads((repair_root / "matrix_status.json").read_text())["jobs"]}
        state["jobs"] = [repairs.get(row["id"], row) for row in state["jobs"]]
    groups = collections.defaultdict(list)
    details = []
    for row in state["jobs"]:
        path, report = report_for(row)
        item = {"job_id": row["id"], "stream": row["stream"], "level": row["level"],
                "condition": row["condition"], "baseline": row["agent"],
                "matching_complete": path is not None, "status": row["status"]}
        if path:
            turns = report["turns"]
            # L1 scores every turn. L2 formal probe accuracy uses only the
            # benchmark's explicit probe role, never application/learning turns.
            selected = [t for t in turns if row["level"] == "l1"
                        or row.get("turn_roles", {}).get(t["turn_id"]) == "probe"]
            scores = [_turn_score(t) for t in selected]
            if any(v is None for v in scores):
                raise ValueError("matched report contains an unscored selected turn")
            item.update(report=str(path), scored_turns=len(scores),
                        mean_score=sum(scores) / len(scores) if scores else None,
                        passed_fraction=sum(bool(t.get("passed")) for t in selected) / len(selected) if selected else None)
        groups[(row["level"], row["condition"])].append(item)
        details.append(item)
    summary = []
    for (level, condition), cells in sorted(groups.items()):
        matched = [c for c in cells if c["matching_complete"]]
        scored = [c for c in matched if c["mean_score"] is not None]
        summary.append({"level": level, "condition": condition,
            "matching_cells": len(matched), "planned_cells": len(cells),
            "matched_stream_macro_mean_score": sum(c["mean_score"] for c in scored) / len(scored) if scored else None,
            "matched_stream_macro_pass_fraction": sum(c["passed_fraction"] for c in scored) / len(scored) if scored else None})
    result = {"at": datetime.now(timezone.utc).isoformat(), "final": False,
        "model": "deepseek-v4-flash", "baseline": "deepseek-harness",
        "dataset": "L1 23 streams; L2 Demo V2 six streams; V5 absent",
        "learning": "native learner disabled; evolving only retains workspace",
        "algorithm": "equal weight per fully matching stream; L1 all turns, L2 explicit probe roles; nonmatching cells excluded, not zero-filled",
        "limitation": "coverage differs across conditions; this is not a full-benchmark ranking; partial prefixes and exceptional overlays remain separate",
        "groups": summary, "cells": details}
    write_json(root / "partial_matched_summary.json", result)
    print(json.dumps({"final": False, "groups": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
