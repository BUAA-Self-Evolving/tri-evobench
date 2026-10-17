#!/usr/bin/env python3
"""Read-only W3 counts and released-placement diffs for the approved r3 pools."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any


AFFECTED_BENCHMARKS = {"skilllearnbench", "skillsbench", "sr_agents", "tau2_bench"}
# Native recovery only for SRA under W2-phase2c-resume.md; no new SRA Streams.
REQUESTED_STREAMS = {
    "intra_skilllearn_blocked_01": ("skilllearnbench", "blocked", ["skilllearn.earthquake-plate-calculation", "skilllearn.organize-messy-files", "skilllearn.dependency-vulnerability-check"]),
    "intra_skillsbench_interleaved_01": ("skillsbench", "interleaved", ["skillsbench.software-engineering", "skillsbench.industrial-physical-systems", "skillsbench.natural-science"]),
    "intra_tau2_telecom_delayed_01": ("tau2_bench", "delayed", ["tau2.telecom.mobile_data_troubleshooting", "tau2.telecom.mms_troubleshooting", "tau2.telecom.service_troubleshooting"]),
    "intra_tau2_airline_interleaved_01": ("tau2_bench", "interleaved", ["tau2.airline.booking_cancellation", "tau2.airline.policy_refusal", "tau2.airline.reservation_changes"]),
}


def load_records(root: Path, families: tuple[str, ...] = ("native", "sra", "tau2")) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    records, decisions = {}, {}
    for family in families:
        index = root / family / "INDEX.jsonl"
        if index.exists():
            for line in index.read_text().splitlines():
                item = json.loads(line)
                if item["source_id"] in decisions:
                    raise ValueError("duplicate INDEX source: " + item["source_id"])
                decisions[item["source_id"]] = item
        for file in (root / family).glob("*/pool_task.json"):
            item = json.loads(file.read_text())
            if item["source_id"] in records:
                raise ValueError("duplicate pool record source: " + item["source_id"])
            records[item["source_id"]] = item
    return records, decisions


def handoff(old_pool: Path, new_pool: Path, package: Path, r2_qualification: Path | None = None,
            r2_inventory: Path | None = None) -> dict[str, Any]:
    rechecks = {}
    if r2_qualification is not None:
        for line in r2_qualification.read_text().splitlines():
            row = json.loads(line)
            rechecks[(row["stream_id"], row["turn_id"])] = row
    old, _ = load_records(old_pool)
    new, decisions = load_records(new_pool)
    for sid, record in {**old, **new}.items():
        source = record["source"]
        if sid != source["benchmark_id"] + ":" + source["source_task_id"]:
            raise ValueError("pool source identity conflicts with source basis: " + sid)
    identities = {(r["source"]["benchmark_id"], r["source"]["source_task_id"]): sid
                  for sid, r in {**old, **new}.items()}
    used = set()
    placements = []
    package_placements = {}
    for file in sorted((package / "streams").glob("*/turn*/task.json")):
        task = json.loads(file.read_text())
        basis = task["source_basis"]
        if basis["benchmark_id"] not in AFFECTED_BENCHMARKS:
            continue
        sid = identities.get((basis["benchmark_id"], basis["source_task_id"]),
                             basis["benchmark_id"] + ":" + basis["source_task_id"])
        key = (task["stream_id"], task["turn_id"])
        if key in package_placements:
            raise ValueError("duplicate affected package placement: " + repr(key))
        package_placements[key] = sid
        if sid not in decisions:
            raise ValueError("affected r2 placement has no new INDEX decision: " + sid + " " + repr(key))
        if task["stream_id"].startswith("intra_"):
            used.add(sid)
        previous, candidate = old.get(sid, {}), new.get(sid, {})
        decision = decisions.get(sid, {})
        old_digest = previous.get("runtime", {}).get("task_image_digest")
        new_digest = candidate.get("runtime", {}).get("task_image_digest")
        new_admitted = decision.get("admitted", candidate.get("qualification", {}).get("admitted", False))
        placements.append({"stream_id": task["stream_id"], "turn_id": task["turn_id"], "source_id": sid,
                           "old_image": old_digest, "new_image": new_digest,
                           "environment_changed": old_digest != new_digest if new_digest is not None else None,
                           "environment_comparison": "observed_images" if new_digest is not None else ("recipe_only" if candidate.get("environment", {}).get("kind") == "dockerfile" else "not_materialized"),
                           "recipe_environment": candidate.get("environment"),
                           "family_sample_verified": decision.get("family_sample_verified", candidate.get("family_sample_verified")),
                           "resource_enforcement": candidate.get("runtime", {}).get("resource_enforcement", decision.get("resource_enforcement")),
                           "resource_readback": candidate.get("runtime", {}).get("resource_readback", decision.get("resource_readback")),
                           "date_dependent": candidate.get("date_dependent", candidate.get("runtime", {}).get("date_dependent", False)),
                           "old_admitted": previous.get("qualification", {}).get("admitted"),
                           "outcome_changed": (previous["qualification"]["admitted"] != new_admitted
                               if "admitted" in previous.get("qualification", {}) and decision.get("stage") != "recipe_ready" else None),
                           "new_admitted": new_admitted, "reason": decision.get("reason"),
                           "new_stage": decision.get("stage"),
                           "old_qualification": previous.get("qualification"),
                           "old_current_recheck": rechecks.get((task["stream_id"], task["turn_id"])),
                           "new_qualification": candidate.get("qualification"),
                           "agent_score_delta": None})
    if r2_inventory is not None:
        inventory = json.loads(r2_inventory.read_text())
        if isinstance(inventory, dict):
            inventory = inventory.get("turns", inventory.get("rows", []))
        expected_placements = {}
        for row in inventory:
            basis = row["source_basis"]
            if basis["benchmark_id"] not in AFFECTED_BENCHMARKS:
                continue
            key = (row["stream_id"], row["turn_id"])
            if key in expected_placements:
                raise ValueError("duplicate frozen affected placement: " + repr(key))
            expected_placements[key] = identities.get((basis["benchmark_id"], basis["source_task_id"]),
                                                      basis["benchmark_id"] + ":" + basis["source_task_id"])
        if package_placements != expected_placements:
            raise ValueError("r2 affected placement coverage/source identity differs from frozen inventory")
    unused = defaultdict(list)
    totals = Counter()
    recipes = Counter()
    samples = defaultdict(set)
    rejected = Counter()
    deferred = Counter()
    inventory_counts = Counter()
    for sid in sorted(new.keys() | decisions.keys()):
        row, decision = new.get(sid, {}), decisions.get(sid, {})
        admitted = decision.get("admitted", row.get("qualification", {}).get("admitted", False))
        if admitted and sid not in new:
            raise ValueError("admitted INDEX source lacks pool record: " + sid)
        cap = row.get("capability_hint", {}).get("capability_id", decision.get("capability_id", "unassigned"))
        benchmark = row.get("source", {}).get("benchmark_id", decision.get("benchmark_id", sid.split(":", 1)[0]))
        inventory_counts[benchmark] += 1
        recipe_ready = decision.get("stage", row.get("qualification", {}).get("stage")) == "recipe_ready"
        sample = decision.get("family_sample_verified", row.get("family_sample_verified"))
        if sample:
            samples[(benchmark, cap)].add(sample)
        if admitted or recipe_ready:
            if recipe_ready:
                if sid not in new or row.get("environment", {}).get("kind") != "dockerfile":
                    raise ValueError("recipe_ready source lacks materialized recipe: " + sid)
                recipes[(benchmark, cap)] += 1
            else:
                totals[(benchmark, cap)] += 1
            if sid not in used:
                unused[(benchmark, cap)].append(sid)
        elif str(decision.get("stage", "")).startswith("deferred"):
            deferred[(benchmark, cap)] += 1
        else:
            rejected[(benchmark, cap)] += 1
    counts = [{"benchmark_id": b, "capability_id": c, "admitted": totals[(b, c)], "recipe_ready": recipes[(b, c)],
               "available": totals[(b, c)] + recipes[(b, c)],
               "family_samples_verified": sorted(samples[(b, c)]),
               "rejected_with_capability_record": rejected[(b, c)], "deferred": deferred[(b, c)],
               "unused_in_r2_intra": len(unused[(b, c)]), "unused_source_ids": sorted(unused[(b, c)])}
              for b, c in sorted(totals.keys() | recipes.keys() | rejected.keys() | deferred.keys())]
    recommendations = {}
    for source in ("skilllearnbench", "skillsbench", "tau2_bench"):
        proposed = next((caps for benchmark, _, caps in REQUESTED_STREAMS.values() if benchmark == source), []) if source != "tau2_bench" else []
        choices = sorted((row for row in counts if row["benchmark_id"] == source and (not proposed or row["capability_id"] in proposed)),
                         key=lambda r: (-r["unused_in_r2_intra"], r["capability_id"]))[:3]
        deficit = sum(max(0, 5-r["unused_in_r2_intra"]) for r in choices) + 5*(3-len(choices))
        recommendations[source] = {"capability_triple": [r["capability_id"] for r in choices],
                                   "three_by_five_shortfall": deficit,
                                   "feasible": len(choices) == 3 and deficit == 0}
    requested_streams = {}
    by_capability = {(r["benchmark_id"], r["capability_id"]): r for r in counts}
    for stream_id, (benchmark, schedule, capabilities) in REQUESTED_STREAMS.items():
        availability = [{"capability_id": cap,
                         "unused_in_r2_intra": by_capability.get((benchmark, cap), {}).get("unused_in_r2_intra", 0),
                         "family_samples_verified": by_capability.get((benchmark, cap), {}).get("family_samples_verified", []),
                         "shortfall": max(0, 5 - by_capability.get((benchmark, cap), {}).get("unused_in_r2_intra", 0))}
                        for cap in capabilities]
        shortfall = sum(r["shortfall"] for r in availability)
        requested_streams[stream_id] = {"benchmark_id": benchmark, "schedule": schedule,
            "capability_triple": capabilities, "tasks_per_capability": 5, "availability": availability,
            "three_by_five_shortfall": shortfall, "feasible": shortfall == 0,
            "source_reservations": [], "provenance": ("W2-supervisor-notes.md recipe/sample scope; native proposed triple" if benchmark in {"skilllearnbench", "skillsbench"} else "W3-compose-r3.md New Streams table (2026-10-16)")}
    resource_counts = Counter(row.get("resource_enforcement", "unrecorded") for row in decisions.values())
    observed_resources = sorted({json.dumps({key: row.get("resource_readback", {}).get(key)
        for key in ("cpu_limit", "memory_limit_bytes", "pids_limit")}, sort_keys=True)
        for row in decisions.values() if isinstance(row.get("resource_readback"), dict)})
    return {"schema_version": "l2_r3_handoff/1.0", "counts": counts, "recommendations": recommendations,
            "inventory_counts": dict(inventory_counts), "requested_streams": requested_streams,
            "existing_sra_intra": {"streams": sorted({r["stream_id"] for r in placements
                if r["source_id"].startswith("sr_agents:") and r["stream_id"].startswith("intra_")}),
                "placements": sum(r["source_id"].startswith("sr_agents:") and r["stream_id"].startswith("intra_") for r in placements),
                "source_ids": sorted(sid for sid in used if sid.startswith("sr_agents:")),
                "new_streams_planned": False},
            "placement_coverage": {"affected_placements": len(placements),
                "by_benchmark": dict(Counter(r["source_id"].split(":", 1)[0] for r in placements)),
                "frozen_inventory_checked": r2_inventory is not None},
            "resource_enforcement_counts": dict(resource_counts),
            "observed_resources": [json.loads(value) for value in observed_resources],
            "placement_diffs": placements, "decisions": decisions,
            "note": "Recipe-ready sources count as available, not as individually red/green verified. Environment/fixture differences are not measured agent score differences. No Stream spec or codebook changed."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-pool", type=Path, required=True)
    parser.add_argument("--new-pool", type=Path, required=True)
    parser.add_argument("--r2-package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--r2-qualification", type=Path)
    parser.add_argument("--r2-inventory", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    result = handoff(args.old_pool, args.new_pool, args.r2_package, args.r2_qualification, args.r2_inventory)
    (args.out / "W3_HANDOFF.json").write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n")
    lines = ["# W3 r3 environment handoff", "", result["note"], "",
             "Unused means not used in any existing r2 intra_* Stream. No new BigCodeBench Streams are planned; SRA results cover the existing pool and released placements.", "",
             "| Source | Capability | Admitted | Recipe ready | Rejected | Deferred | Unused | Verified family samples |", "|---|---|---:|---:|---:|---:|---:|---|"]
    if result["resource_enforcement_counts"].get("declared_not_enforced"):
        lines[4:4] = ["**Resource waiver:** Qualification used an explicitly approved declared_only waiver. "
                      "Docker resource flags were requested but not enforced. New executions still need explicit host opt-in; "
                      "saved profile metadata is provenance, not authorization.",
                      "Observed controller values: `" + json.dumps(result["observed_resources"], sort_keys=True) + "`.", ""]
    lines += [f"| {r['benchmark_id']} | {r['capability_id']} | {r['admitted']} | {r['recipe_ready']} | {r['rejected_with_capability_record']} | {r['deferred']} | {r['unused_in_r2_intra']} | {', '.join(r['family_samples_verified']) or 'none'} |" for r in result["counts"]]
    lines += ["", "## Recommended triples", ""]
    lines += [f"- {b}: {', '.join(r['capability_triple']) or 'none'}; shortfall {r['three_by_five_shortfall']} tasks for one 3×5 Stream." for b, r in result["recommendations"].items()]
    lines += ["", "## Requested Stream readiness (no sources reserved)", ""]
    for stream_id, row in result["requested_streams"].items():
        values = "; ".join(f"{r['capability_id']}: {r['unused_in_r2_intra']} unused, {r['shortfall']} short" for r in row["availability"])
        lines.append(f"- {stream_id}: {values}; total shortfall {row['three_by_five_shortfall']}.")
    lines += ["", "## Existing BigCodeBench Streams", "",
              f"{len(result['existing_sra_intra']['streams'])} existing intra Streams; {result['existing_sra_intra']['placements']} placements; {len(result['existing_sra_intra']['source_ids'])} unique sources. No new BigCodeBench Streams."]
    lines += ["", "## Every affected r2 placement", "", "| Stream | Turn | Source | Environment changed | Old admitted | New admitted | Outcome changed | Current r2 fixtures OK | Reason |", "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['stream_id']} | {r['turn_id']} | {r['source_id']} | {r['environment_changed']} | {r['old_admitted']} | {r['new_admitted']} | {r['outcome_changed']} | {(r['old_current_recheck'] or {}).get('ok', 'not checked')} | {str(r['reason']).replace('|', '/')} |" for r in result["placement_diffs"]]
    (args.out / "W3_HANDOFF.md").write_text("\n".join(lines)+"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
