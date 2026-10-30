"""Audit all selected reports, then emit six macro scores and normalized curves."""
import collections
import math
from pathlib import Path

from tools.prepare_dsh_repair_cohort import ORIGINAL, REPAIR
from tools.recover_dsh_reset_exceptions import digest
from tools.run_new_package_matrix import load, save, now, report_for, _turn_score, process_start_ticks


def average(values):
    return sum(values) / len(values) if values else None


def main():
    plan = load(REPAIR / "run_plan.json")
    state = load(REPAIR / "matrix_status.json")
    original = load(ORIGINAL / "matrix_status.json")
    assert not process_start_ticks(state["pid"])
    assert len(state["jobs"]) == 23 and all(r["status"] == "completed" for r in state["jobs"])
    source_hashes = {}
    for item in plan["provenance"]:
        source_hashes.update(item["source_sha256"])
    for path, expected in source_hashes.items():
        assert digest(path) == expected, f"source changed: {path}"
    claims = list(REPAIR.rglob("fresh_run_authorization.json"))
    assert len(claims) == 22 and all(load(p)["status"] == "consumed" for p in claims)
    continuation = list(REPAIR.glob("scored_exception_authorization_*.json"))
    assert len(continuation) == 1 and load(continuation[0])["status"] == "consumed"
    repairs = {r["id"]: r for r in state["jobs"]}
    groups = collections.defaultdict(list)
    cells, exceptions, turns_total = [], [], 0
    for old in original["jobs"]:
        row = repairs.get(old["id"], old)
        path, report = report_for(row)
        assert path is not None, row["id"]
        assert [t["turn_id"] for t in report["turns"]] == row["turns"]
        assert not report.get("error") and not report.get("cleanup_error")
        turns_total += len(report["turns"])
        points = []
        for index, turn in enumerate(report["turns"], 1):
            score = _turn_score(turn)
            assert score is not None and math.isfinite(score) and 0 <= score <= 1
            if turn.get("exception_accounting"):
                exceptions.append({"job_id": row["id"], "turn_id": turn["turn_id"],
                                   **turn["exception_accounting"]})
            if row["level"] == "l1" or row["turn_roles"].get(turn["turn_id"]) == "probe":
                points.append({"x": index / len(row["turns"]), "score": score,
                               "passed": bool(turn.get("passed")), "turn_id": turn["turn_id"]})
        assert points
        item = {"job_id": row["id"], "level": row["level"], "condition": row["condition"],
                "baseline": row["agent"], "stream": row["stream"], "report": str(path),
                "report_sha256": digest(path), "source": "repair" if row["id"] in repairs else "original",
                "turns": len(row["turns"]), "scored_probes": len(points),
                "mean_score": average([p["score"] for p in points]),
                "pass_fraction": average([int(p["passed"]) for p in points]), "points": points}
        cells.append(item)
        groups[(row["level"], row["condition"], row["agent"])].append(item)
        if row["id"] == "l2/evolving/deepseek-harness/s05":
            prefix = load(Path(row["output"]) / "_checkpoints/scored_exception_evolving/t0089/completed_turns.json")
            assert len(prefix) == 89 and report["turns"][:89] == prefix
    assert len(cells) == 87 and turns_total == 1671
    summary, curves = [], []
    for (level, condition, baseline), streams in sorted(groups.items()):
        assert len(streams) == (23 if level == "l1" else 6)
        summary.append({"level": level, "condition": condition, "baseline": baseline,
                        "streams": len(streams), "score": average([s["mean_score"] for s in streams]),
                        "pass_fraction": average([s["pass_fraction"] for s in streams])})
        # At every actual normalized probe position, average cumulative probe
        # score inside each stream first, then give eligible streams equal weight.
        xs = sorted({0.0, 1.0, *(p["x"] for s in streams for p in s["points"])})
        curve = []
        for x in xs:
            stream_means = [average([p["score"] for p in s["points"] if p["x"] <= x]) for s in streams]
            available = [v for v in stream_means if v is not None]
            curve.append({"x": x, "score": average(available), "streams_with_observed_probe": len(available)})
        assert abs(curve[-1]["score"] - summary[-1]["score"]) < 1e-12
        curves.append({"level": level, "condition": condition, "baseline": baseline, "curve": curve})
    result = {"at": now(), "final": True, "matching_cells": 87, "turns": turns_total,
              "model": "deepseek-v4-flash", "baseline": "deepseek-harness",
              "dataset": "L1 local23; L2_Demo_V2 six; V5 absent",
              "learning": "native learner unsupported/skipped; evolving persists workspace only",
              "selection": "original64 + predeclared repair23; 22 independent complete replacement attempts, one exact prefix continuation; no score-based selection",
              "algorithm": "L1 all turns; L2 explicit probe roles. Mean probe score per stream, then unweighted stream mean per level/condition/baseline.",
              "source_files_verified": len(source_hashes), "exceptions": exceptions,
              "groups": summary, "cells": cells}
    save(REPAIR / "final_summary.json", result)
    save(REPAIR / "normalized_accuracy_curves.json", {
        "algorithm": "x=original turn ordinal / stream total turns. At each union x, cumulative observed selected-probe mean per stream, then macro mean over streams having a probe. No unseen probes are zero-filled; report coverage at every x. Endpoint equals final macro score.",
        "curves": curves})
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    colors = {"reset": "#64748b", "frozen": "#2563eb", "evolving": "#d97706"}
    for axis, level in zip(axes, ("l1", "l2")):
        for item in curves:
            if item["level"] != level:
                continue
            data = [p for p in item["curve"] if p["score"] is not None]
            axis.step([p["x"] for p in data], [p["score"] for p in data], where="post",
                      label=item["condition"], color=colors[item["condition"]])
        axis.set(title=level.upper(), xlabel="Normalized original turn (turn / stream length)", xlim=(0, 1), ylim=(0, 1))
        axis.yaxis.set_major_formatter(PercentFormatter(1))
        axis.grid(alpha=.2)
        axis.legend()
    axes[0].set_ylabel("Cumulative macro probe score")
    fig.suptitle("DSH × deepseek-v4-flash | 87 cells / 1671 turns")
    fig.text(.5, .01, "L1: all turns. L2: Demo V2 explicit probes. Unobserved streams excluded until first probe; see JSON coverage.", ha="center", fontsize=8)
    fig.tight_layout(rect=(0, .04, 1, .95))
    fig.savefig(REPAIR / "normalized_accuracy_curves.png", dpi=160)
    fig.savefig(REPAIR / "normalized_accuracy_curves.svg")
    plt.close(fig)
    lines = ["# DSH × deepseek-v4-flash final results", "", "87/87 cells, 1671/1671 turns audited.", "",
             "| Level | Reset | Frozen | Evolving |", "|---|---:|---:|---:|"]
    for level in ("l1", "l2"):
        values = [next(g["score"] for g in summary if g["level"] == level and g["condition"] == c) for c in ("reset", "frozen", "evolving")]
        lines.append("| " + level.upper() + " | " + " | ".join(f"{v:.2%}" for v in values) + " |")
    lines += ["", result["algorithm"], "", result["selection"], "", result["learning"], "", result["dataset"],
              "", "Curve algorithm and exact values: normalized_accuracy_curves.json. Every source evidence hash was verified; final_summary.json contains all87 report hashes and stream scores."]
    (REPAIR / "FINAL_RESULTS.md").write_text("\n".join(lines) + "\n")
    print({"matching": 87, "turns": turns_total, "source_hashes_verified": len(source_hashes), "groups": summary})


if __name__ == "__main__":
    main()
