#!/usr/bin/env python3
"""Read-only W3 counts and released-placement diffs for the approved r3 pools."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any


def load_records(root: Path, families: tuple[str, ...] = ("native", "sra", "tau2")) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    records, decisions = {}, {}
    for family in families:
        index = root / family / "INDEX.jsonl"
        if index.exists():
            for line in index.read_text().splitlines():
                item = json.loads(line)
                decisions[item["source_id"]] = item
        for file in (root / family).glob("*/pool_task.json"):
            item = json.loads(file.read_text())
            records[item["source_id"]] = item
    return records, decisions


def handoff(old_pool: Path, new_pool: Path, package: Path, r2_qualification: Path | None = None) -> dict[str, Any]:
    rechecks = {}
    if r2_qualification is not None:
        for line in r2_qualification.read_text().splitlines():
            row = json.loads(line)
            rechecks[(row["stream_id"], row["turn_id"])] = row
    old, _ = load_records(old_pool)
    new, decisions = load_records(new_pool)
    identities = {(r["source"]["benchmark_id"], r["source"]["source_task_id"]): sid
                  for sid, r in {**old, **new}.items()}
    used = set()
    placements = []
    for file in sorted((package / "streams").glob("*/turn*/task.json")):
        task = json.loads(file.read_text())
        basis = task["source_basis"]
        sid = identities.get((basis["benchmark_id"], basis["source_task_id"]))
        if sid is None:
            continue
        if task["stream_id"].startswith("intra_"):
            used.add(sid)
        previous, candidate = old.get(sid, {}), new.get(sid, {})
        decision = decisions.get(sid, {})
        old_digest = previous.get("runtime", {}).get("task_image_digest")
        new_digest = candidate.get("runtime", {}).get("task_image_digest")
        new_admitted = decision.get("admitted", candidate.get("qualification", {}).get("admitted", False))
        placements.append({"stream_id": task["stream_id"], "turn_id": task["turn_id"], "source_id": sid,
                           "old_image": old_digest, "new_image": new_digest,
                           "environment_changed": old_digest != new_digest,
                           "new_admitted": new_admitted, "reason": decision.get("reason"),
                           "new_stage": decision.get("stage"),
                           "old_qualification": previous.get("qualification"),
                           "old_current_recheck": rechecks.get((task["stream_id"], task["turn_id"])),
                           "new_qualification": candidate.get("qualification"),
                           "agent_score_delta": None})
    unused = defaultdict(list)
    totals = Counter()
    rejected = Counter()
    for sid, row in new.items():
        admitted = decisions.get(sid, {}).get("admitted", row.get("qualification", {}).get("admitted", False))
        cap = row.get("capability_hint", {}).get("capability_id", "unassigned")
        benchmark = row["source"]["benchmark_id"]
        if admitted:
            totals[(benchmark, cap)] += 1
            if sid not in used:
                unused[(benchmark, cap)].append(sid)
        else:
            rejected[(benchmark, cap)] += 1
    counts = [{"benchmark_id": b, "capability_id": c, "admitted": totals[(b, c)],
               "rejected_with_capability_record": rejected[(b, c)],
               "unused_in_r2_intra": len(unused[(b, c)]), "unused_source_ids": sorted(unused[(b, c)])}
              for b, c in sorted(totals.keys() | rejected.keys())]
    recommendations = {}
    for source in ("skilllearnbench", "skillsbench", "sr_agents", "tau2_bench"):
        choices = sorted((row for row in counts if row["benchmark_id"] == source),
                         key=lambda r: (-r["unused_in_r2_intra"], r["capability_id"]))[:3]
        deficit = sum(max(0, 5-r["unused_in_r2_intra"]) for r in choices) + 5*(3-len(choices))
        recommendations[source] = {"capability_triple": [r["capability_id"] for r in choices],
                                   "three_by_five_shortfall": deficit,
                                   "feasible": len(choices) == 3 and deficit == 0}
    return {"schema_version": "l2_r3_handoff/1.0", "counts": counts, "recommendations": recommendations,
            "placement_diffs": placements, "decisions": decisions,
            "note": "Environment/fixture differences are not measured agent score differences. No Stream spec or codebook changed."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-pool", type=Path, required=True)
    parser.add_argument("--new-pool", type=Path, required=True)
    parser.add_argument("--r2-package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--r2-qualification", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    result = handoff(args.old_pool, args.new_pool, args.r2_package, args.r2_qualification)
    (args.out / "W3_HANDOFF.json").write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n")
    lines = ["# W3 r3 environment handoff", "", result["note"], "",
             "Unused means not used in any existing r2 intra_* Stream. Recommendations do not reserve sources for either new SRA/tau Stream.", "",
             "| Source | Capability | Admitted | Unused |", "|---|---|---:|---:|"]
    lines += [f"| {r['benchmark_id']} | {r['capability_id']} | {r['admitted']} | {r['unused_in_r2_intra']} |" for r in result["counts"]]
    lines += ["", "## Recommended triples", ""]
    lines += [f"- {b}: {', '.join(r['capability_triple']) or 'none'}; shortfall {r['three_by_five_shortfall']} tasks for one 3×5 Stream." for b, r in result["recommendations"].items()]
    lines += ["", "## Every affected r2 placement", "", "| Stream | Turn | Source | Environment changed | New admitted | Current r2 fixtures OK | Reason |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['stream_id']} | {r['turn_id']} | {r['source_id']} | {r['environment_changed']} | {r['new_admitted']} | {(r['old_current_recheck'] or {}).get('ok', 'not checked')} | {str(r['reason']).replace('|', '/')} |" for r in result["placement_diffs"]]
    (args.out / "W3_HANDOFF.md").write_text("\n".join(lines)+"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
