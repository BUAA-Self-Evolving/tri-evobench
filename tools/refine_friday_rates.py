"""Refine the stable 5RPM..failed10RPM interval, stopping at first rejection.

Run only after calibrate_friday_accounts has completed. Retains its shared-pacer,
privacy, no-replay and audited CAS contracts. Tests 6,7,8,9RPM at at most two
inflight calls, then applies 10% rate headroom and validates native traffic.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sqlite3
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.calibrate_friday_accounts import append, request_probe, set_limits, summarize
from tools.run_friday_gateway import load_pool
from trievo_bench.friday_gateway import SharedPacer


def select_rate(stages, base_cap):
    best, cap = 5.0, base_cap
    for stage in stages:
        z = stage["summary"]
        if stage["status"] != "observed" or z["failures"]:
            continue
        if z["measured_success_rpm"] < stage["rpm"] * 0.9:
            continue
        best = max(best, round(stage["rpm"] * 0.9, 1))
        if z["observed_peak_completed_request_overlap"] >= 2:
            cap = 2
    return best, cap


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--app-id-file", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--prior-summary", type=Path, required=True)
    p.add_argument("--matrix", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    prior = json.loads(args.prior_summary.read_text())
    credentials = load_pool(args.app_id_file)
    pacer = SharedPacer(args.state)
    slots = {c.slot: c for c in credentials}
    current = {}
    with pacer.connection() as db:
        for s, c in slots.items():
            row = db.execute("SELECT rpm,max_inflight FROM budgets WHERE identity=?",
                             (pacer.identity(c.value),)).fetchone()
            if row != tuple(prior["selected"][s]) or row[0] != 5 or row[1] not in (1, 2):
                raise ValueError("requires completed primary calibration and its 5RPM policies")
            current[s] = row
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = dict(started_at=datetime.now(timezone.utc).isoformat(),
                    authorization="User requested finding fastest low429 configuration for each account; refine measured interval.",
                    prior_summary=str(args.prior_summary), rates=[6, 7, 8, 9],
                    stage_seconds=180, settling_seconds=60, validation_seconds=300,
                    maximum_inflight=2, probe_request_cap_per_slot=120,
                    stop="First429 stops all higher rates for that account; no retries.",
                    margin="10percent below highest observed tier sustaining >=90percent target successful RPM.",
                    scope="Combined native and synthetic shared-budget load; bounded observations, not exact upstream quota.")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rows, futures, stages, blocked, seq = [], {}, {s: [] for s in slots}, set(), Counter()
    base_caps = {s: c[1] for s, c in current.items()}
    traffic = args.state.with_suffix(".timings.jsonl").open()
    traffic.seek(0, 2)
    complete = False

    def update(s, target, why):
        if current[s] == tuple(target):
            return
        try:
            current[s] = set_limits(args.state, slots[s], target, current[s],
                                     args.output / "budget-events.jsonl", why)
        except BaseException:
            with pacer.connection() as db:
                current[s] = db.execute("SELECT rpm,max_inflight FROM budgets WHERE identity=?",
                                       (pacer.identity(slots[s].value),)).fetchone()
            raise

    def ingest():
        while True:
            offset = traffic.tell()
            line = traffic.readline()
            if not line or not line.endswith("\n"):
                traffic.seek(offset)
                break
            r = json.loads(line)
            if r.get("pid") != 80120 or not r.get("dispatched"):
                continue
            rows.append(dict(origin="native", slot=r["slot"], start=r["at"] - r["elapsed_seconds"]
                + r["queue_seconds"], end=r["at"], accepted=r.get("upstream_status") == 200
                and r.get("business_status") in (None, 200), rate_limited=r.get("upstream_status") == 429
                or r.get("business_status") == 429))
        for f in list(futures):
            if f.done():
                rows.append(f.result())
                del futures[f]

    def status(phase):
        m = json.loads(args.matrix.read_text())
        e = dict(at=datetime.now(timezone.utc).isoformat(), phase=phase,
                 effective=current, probe_counts=dict(seq),
                 rate_limits=sum(r["rate_limited"] for r in rows),
                 counts=dict(Counter(j.get("status", "pending") for j in m["jobs"])))
        append(args.output / "progress.jsonl", e)
        print(json.dumps(e), flush=True)
        return {f"FRIDAY_APP_ID_{j['credential_slot']}" for j in m["jobs"] if j.get("status") == "running"}

    def terminate(signum, frame):
        raise SystemExit("refinement interrupted")

    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)
    with ThreadPoolExecutor(max_workers=12) as executor:
        try:
            for rpm in (6, 7, 8, 9):
                active = set(slots) - blocked
                if not active:
                    break
                for s in active:
                    update(s, (rpm, 2), "authorized intermediate-rate test")
                start, last = time.time(), 0
                native = status(f"rpm{rpm}-cap2")
                stopped = {}

                def observe():
                    ingest()
                    for s in list(active):
                        if any(r["rate_limited"] for r in rows if r["slot"] == s and r["end"] >= start):
                            blocked.add(s)
                            active.remove(s)
                            stopped[s] = time.time()
                            update(s, (5, base_caps[s]), "first429 stops this account's higher rate tests")

                while time.time() - start < 180 and active:
                    observe()
                    for s in active:
                        if seq[s] >= 120 or sum(v == s for v in futures.values()) >= (1 if s in native else 2):
                            continue
                        with pacer.connection() as db:
                            nxt, cool = db.execute("SELECT next_start,cooldown FROM budgets WHERE identity=?",
                                (pacer.identity(slots[s].value),)).fetchone()
                        if time.time() < max(nxt, cool) + 0.6:
                            continue
                        lease, _ = pacer.try_acquire(pacer.identity(slots[s].value), now=time.time(), lease_seconds=200)
                        if lease:
                            seq[s] += 1
                            f = executor.submit(request_probe, slots[s], pacer, lease,
                                f"rpm{rpm}-cap2", seq[s], args.output / "probe-requests.jsonl")
                            futures[f] = s
                    if time.time() - last >= 30:
                        native = status(f"rpm{rpm}-cap2")
                        last = time.time()
                    time.sleep(0.2)
                while futures:
                    observe()
                    time.sleep(0.2)
                ingest()
                end = time.time()
                for s in set(active) | set(stopped):
                    relevant = [r for r in rows if r["slot"] == s and start <= r["start"] < end]
                    stages[s].append(dict(rpm=rpm, cap=2, started=start, ended=end,
                        status="rejected" if s in stopped else "observed",
                        summary=summarize(relevant, start, stopped.get(s, start + 180))))
                (args.output / "stages.json").write_text(json.dumps(stages, indent=2) + "\n")
            selected = {s: select_rate(stages[s], base_caps[s]) for s in slots}
            for s in slots:
                update(s, selected[s], "highest measured intermediate rate minus10percent headroom")
            start, last = time.time(), 0
            while time.time() - start < 300:
                ingest()
                for s in slots:
                    if any(r["rate_limited"] for r in rows if r["slot"] == s and r["end"] >= start):
                        selected[s] = (5, base_caps[s])
                        update(s, selected[s], "native validation429; rollback to prior proven configuration")
                if time.time() - last >= 30:
                    status("native-validation")
                    last = time.time()
                time.sleep(0.2)
            ingest()
            result = dict(manifest=manifest, finished_at=datetime.now(timezone.utc).isoformat(),
                          selected=selected, stages=stages, native_validation_started=start,
                          native_validation={s: summarize([r for r in rows if r["slot"] == s
                              and r["start"] >= start], start, time.time()) for s in slots},
                          probe_outcomes=dict(Counter("429" if r["rate_limited"] else "accepted" if r["accepted"]
                              else "other_error" for r in rows if r["origin"] == "probe")),
                          successful_probe_usage={k: sum(r.get("usage", {}).get(k, 0) for r in rows
                              if r["origin"] == "probe") for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
            (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
            complete = True
            status("complete")
        finally:
            if not complete:
                for s in slots:
                    update(s, (5, base_caps[s]), "incomplete refinement conservative rollback")
            traffic.close()


if __name__ == "__main__":
    main()
