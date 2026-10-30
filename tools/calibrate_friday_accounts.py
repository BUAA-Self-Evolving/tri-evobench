"""Bounded per-account calibration sharing the live matrix request pacer.

User-authorized probes are independent requests, never retries of experiment
Interactions. Each tested rate shares its spacing/leases with native traffic.
The first 429 stops that account's rate tier and immediately restores 5RPM/1.
No active request, lease, worker, frozen configuration or result is replaced.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.probe_friday_capacity import numeric_rate_headers
from tools.run_friday_gateway import load_pool
from trievo_bench.friday_gateway import SharedPacer, check_gateway
from trievo_bench.provider_profiles import get_provider_profile


def append(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(event) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def set_limits(state, credential, target, expected, journal, reason):
    """CAS account policy; persist intent before commit, retaining all leases."""
    rpm, maximum = target
    if (not math.isfinite(rpm) or not 1 <= rpm <= 10
            or type(maximum) is not int or not 1 <= maximum <= 4):
        raise ValueError("outside authorized calibration envelope")
    identity = SharedPacer.identity(credential.value)
    with sqlite3.connect(Path(state).resolve().as_uri() + "?mode=rw", uri=True,
                         timeout=10, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT rpm,max_inflight FROM budgets WHERE identity=?",
                         (identity,)).fetchone()
        if row != tuple(expected):
            raise ValueError("live policy changed unexpectedly")
        now = time.time()
        event = dict(id=uuid.uuid4().hex, at=now, slot=credential.slot,
                     before=list(row), after=list(target), reason=reason)
        append(journal, {**event, "status": "intent"})
        db.execute("UPDATE budgets SET rpm=?,max_inflight=?,next_start=MAX(next_start,?) "
                   "WHERE identity=?", (rpm, maximum, now + 60 / rpm, identity))
        db.commit()
        append(journal, {**event, "status": "applied"})
    return tuple(target)


def diagnose(data, http_status, headers):
    if not isinstance(data, dict):
        data = {}
    try:
        business = int(data.get("status", http_status))
    except (ValueError, TypeError):
        business = http_status
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    message = str(data.get("message", "")) + " " + str(error.get("message", ""))
    return dict(accepted=http_status == 200 and business == 200 and bool(data.get("choices")),
                rate_limited=http_status == 429 or business == 429,
                http_status=http_status, business_status=business,
                mentions_rpm=bool(re.search(r"\bRPM\b|requests? per minute|每分钟.*请求", message, re.I)),
                mentions_tpm=bool(re.search(r"\bTPM\b|tokens? per minute|每分钟.*[Tt]oken", message, re.I)),
                mentions_concurrency=bool(re.search(r"concurr|in.?flight|并发|同时.*请求", message, re.I)),
                rate_headers=numeric_rate_headers(headers),
                reported_rpm_values=[int(x) for x in re.findall(
                    r"\bRPM\s*[:=：]?\s*(\d{1,8})(?!\d)", message, re.I)])


def request_probe(credential, pacer, lease, phase, sequence, output, token_cap=None):
    started = time.time()
    # Mixed latency exercises both request rate and actual overlapping calls.
    # Holding a completed response/lease open is never used to fake concurrency.
    tokens = token_cap or (16, 256, 2048)[(sequence - 1) % 3]
    prompt = ("Reply exactly OK." if tokens == 16 else
              "Write a numbered list of short factual sentences about arithmetic, "
              "starting at 1. Continue without a conclusion until the output limit. "
              "Do not use tools or code fences.")
    event = dict(origin="probe", slot=credential.slot, phase=phase,
                 sequence=sequence, start=started, max_tokens=tokens,
                 thinking="disabled", accepted=False, rate_limited=False)
    profile = get_provider_profile("friday", model="glm-53-meituan")
    payload = dict(model=profile.model, messages=[dict(role="user", content=prompt)],
                   max_tokens=tokens, stream=False, thinking=dict(type="disabled"))
    try:
        request = urllib.request.Request(profile.base_url + "/chat/completions",
                  data=json.dumps(payload).encode(),
                  headers={"Authorization": "Bearer " + credential.value,
                           "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=90)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            code, headers = response.code, response.headers
            deadline = time.monotonic() + 90
            chunks, count = [], 0
            while time.monotonic() < deadline:
                raw = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
                if sock:
                    sock.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = response.read1(65536)
                if not chunk:
                    break
                count += len(chunk)
                if count > 1024 * 1024:
                    raise ValueError("probe response size")
                chunks.append(chunk)
            else:
                raise TimeoutError("probe response deadline")
        try:
            data = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            data = {}
        event.update(diagnose(data, code, headers))
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        if isinstance(usage, dict):
            event["usage"] = {k: v for k, v in usage.items() if k in
                              {"prompt_tokens", "completion_tokens", "total_tokens"}
                              and type(v) is int}
        if event["rate_limited"]:
            try:
                delay = float(headers.get("Retry-After", "60"))
            except ValueError:
                delay = 60
            pacer.cooldown(pacer.identity(credential.value), max(60, min(600, delay)))
    except Exception as error:
        event["error_type"] = type(error).__name__
    finally:
        event["end"] = time.time()
        event["elapsed_seconds"] = event["end"] - started
        pacer.release(lease)
        append(output, event)
    return event


def summarize(rows, start, end):
    measured = [r for r in rows if start + 60 <= r["start"] < end]
    accepted = [r for r in measured if r["accepted"]]
    times = sorted(r["start"] for r in measured)
    overlap = max((sum(r["start"] <= t < r["end"] for r in rows)
                   for t in times), default=0)
    return dict(measurement_seconds=max(0, end - start - 60),
                records=len(measured), successful=len(accepted),
                rate_limits=sum(r["rate_limited"] for r in rows),
                failures=sum(not r["accepted"] for r in rows),
                measured_success_rpm=60 * len(accepted) / max(1, end - start - 60),
                observed_peak_completed_request_overlap=overlap,
                max_recorded_starts_60s=max((sum(t - 60 < x <= t for x in times)
                                            for t in times), default=0),
                probe_records=sum(r["origin"] == "probe" for r in rows),
                native_records=sum(r["origin"] == "native" for r in rows))


def choose(stages):
    """Prefer measured throughput; never promote an unexercised higher cap."""
    eligible = [s for s in stages if s["status"] == "observed" and
                not s["summary"]["failures"] and s["summary"]["records"] >= 4]
    if not eligible:
        return (5, 1)
    best = (5, 1)
    baseline = next((s["summary"]["measured_success_rpm"] for s in eligible
                     if s["rpm"] == 5 and s["cap"] == 1), 0)
    for s in sorted(eligible, key=lambda x: (x["rpm"], x["cap"])):
        z = s["summary"]
        if s["rpm"] == 10 and (z["measured_success_rpm"] < 9 or
                                      z["max_recorded_starts_60s"] < 9):
            continue
        if s["cap"] > 1 and z["observed_peak_completed_request_overlap"] < s["cap"]:
            continue
        if z["measured_success_rpm"] > baseline * 1.05:
            best = (8 if s["rpm"] == 10 else 5, s["cap"])
            baseline = z["measured_success_rpm"]
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id-file", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage-seconds", type=float, default=180)
    parser.add_argument("--validation-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.stage_seconds < 180 or args.validation_seconds < 180:
        parser.error("at least 60s settling plus 120s observation per stage")
    credentials = load_pool(args.app_id_file)
    profile = json.loads(args.profile.read_text())["friday_gateway"]
    check_gateway(profile["host_base_url"], credentials, profile["policy"], profile["model"])
    args.output.mkdir(parents=True, exist_ok=False)
    pacer = SharedPacer(args.state)
    current = {}
    with pacer.connection() as db:
        for c in credentials:
            row = db.execute("SELECT rpm,max_inflight FROM budgets WHERE identity=?",
                             (pacer.identity(c.value),)).fetchone()
            if row != (5, 1):
                raise ValueError("calibration requires the audited 5RPM/1 starting point")
            current[c.slot] = row
    slots = {c.slot: c for c in credentials}
    rejected, results, records, futures, sequence = {}, {c.slot: [] for c in credentials}, [], {}, Counter()
    journal = args.output / "budget-events.jsonl"
    traffic = args.state.with_suffix(".timings.jsonl").open()
    traffic.seek(0, 2)
    manifest = dict(started_at=datetime.now(timezone.utc).isoformat(), model=profile["model"],
                    user_authorized="Per-account 5/10RPM and inflight calibration, bounded rejection, optimize live experiment.",
                    stages=[[r, c] for r in (5, 10) for c in (1, 2, 4)],
                    stage_seconds=args.stage_seconds, validation_seconds=args.validation_seconds,
                    scope="Shared live pacer; synthetic mixed-output probes plus native workload. "
                    "No exact quota or indefinite zero429 guarantee; idle caps may remain unexercised.",
                    probe_token_caps=[16, 256, 2048], thinking="disabled", probe_retries=0,
                    max_probe_requests_per_slot=180, max_probe_completion_tokens_ceiling=835200,
                    probe_timeout_seconds=90, native_gateway_pid=80120)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    complete = False
    native_slots = set()

    def update(slot, target, reason):
        if current[slot] != tuple(target):
            try:
                current[slot] = set_limits(args.state, slots[slot], target, current[slot], journal, reason)
            except BaseException:
                # Commit may have succeeded before a receipt write failed.
                with pacer.connection() as db:
                    current[slot] = db.execute("SELECT rpm,max_inflight FROM budgets WHERE identity=?",
                        (pacer.identity(slots[slot].value),)).fetchone()
                raise

    def stop_rate(slot, rpm, reason):
        rejected[slot, rpm] = reason
        update(slot, (5, 1), reason)

    def ingest():
        while True:
            position = traffic.tell()
            line = traffic.readline()
            if not line or not line.endswith("\n"):
                traffic.seek(position)
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("pid") != 80120 or not row.get("dispatched"):
                continue
            row = dict(origin="native", slot=row["slot"], start=row["at"] - row["elapsed_seconds"]
                       + row["queue_seconds"], end=row["at"],
                       accepted=row.get("upstream_status") == 200 and row.get("business_status") in (None, 200),
                       rate_limited=row.get("upstream_status") == 429 or row.get("business_status") == 429)
            records.append(row)
        for future in list(futures):
            if future.done():
                records.append(future.result())
                del futures[future]

    def progress(phase):
        nonlocal native_slots
        m = json.loads(args.matrix.read_text())
        native_slots = {f"FRIDAY_APP_ID_{j['credential_slot']}" for j in m["jobs"]
                        if j.get("status") == "running"}
        event = dict(at=datetime.now(timezone.utc).isoformat(), phase=phase,
                     effective={s: list(v) for s, v in current.items()},
                     probes=dict(sequence), in_flight_probes=len(futures),
                     rate_limits=sum(r["rate_limited"] for r in records),
                     job_counts=dict(Counter(j.get("status", "pending") for j in m["jobs"])))
        append(args.output / "progress.jsonl", event)
        print(json.dumps(event), flush=True)

    def terminate(signum, frame):
        raise SystemExit("calibration interrupted; restoring conservative caps")

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    with ThreadPoolExecutor(max_workers=24) as executor:
        try:
            for rpm in (5, 10):
                for cap in (1, 2, 4):
                    phase = f"rpm{rpm}-inflight{cap}"
                    active = set()
                    for slot in slots:
                        if (slot, rpm) in rejected:
                            results[slot].append(dict(rpm=rpm, cap=cap, status="skipped_after_rejection"))
                            continue
                        previous = results[slot][-1] if results[slot] else None
                        if cap == 4 and (not previous or previous.get("summary", {}).get(
                                "observed_peak_completed_request_overlap", 0) < 2):
                            results[slot].append(dict(rpm=rpm, cap=cap, status="skipped_no_overlap_bottleneck"))
                            continue
                        update(slot, (rpm, cap), "authorized calibration " + phase)
                        active.add(slot)
                    if not active:
                        continue
                    started, last_print = time.time(), 0
                    ends = {}
                    while time.time() - started < args.stage_seconds and active:
                        ingest()
                        for slot in list(active):
                            relevant = [r for r in records if r["slot"] == slot and r["end"] >= started]
                            if any(r["rate_limited"] for r in relevant):
                                stop_rate(slot, rpm, "first429 stop " + phase)
                                active.remove(slot)
                                ends[slot] = time.time()
                                continue
                            own = sum(s == slot for s in futures.values())
                            probe_cap = max(1, cap - 1) if slot in native_slots else cap
                            if sequence[slot] >= 180 or own >= probe_cap:
                                continue
                            # Existing queued native clients poll every <=250ms.
                            # Offer them first admission; probes consume residual
                            # capacity instead of racing queued paid work.
                            with pacer.connection() as db:
                                next_start, cooldown = db.execute(
                                    "SELECT next_start,cooldown FROM budgets WHERE identity=?",
                                    (pacer.identity(slots[slot].value),)).fetchone()
                            if time.time() < max(next_start, cooldown) + 0.6:
                                continue
                            lease, _ = pacer.try_acquire(pacer.identity(slots[slot].value),
                                now=time.time(), lease_seconds=200)
                            if lease:
                                sequence[slot] += 1
                                future = executor.submit(request_probe, slots[slot], pacer, lease,
                                                         phase, sequence[slot], args.output / "probe-requests.jsonl",
                                                         16 if cap == 1 and slot in native_slots else None)
                                futures[future] = slot
                        if time.time() - last_print > 30:
                            progress(phase)
                            last_print = time.time()
                        time.sleep(0.2)
                    # Never cancel/replace a dispatched probe at phase changes.
                    while futures:
                        ingest()
                        for slot in list(active):
                            if any(r["rate_limited"] for r in records
                                   if r["slot"] == slot and r["end"] >= started):
                                stop_rate(slot, rpm, "first429 while draining " + phase)
                                active.remove(slot)
                                ends[slot] = time.time()
                        time.sleep(0.2)
                    ingest()
                    ended = time.time()
                    for slot in slots:
                        if slot not in active and slot not in ends:
                            continue
                        subset = [r for r in records if r["slot"] == slot and started <= r["start"] < ended]
                        summary = summarize(subset, started, ends.get(slot, started + args.stage_seconds))
                        results[slot].append(dict(rpm=rpm, cap=cap, status="rejected" if slot in ends else "observed",
                                                  started=started, ended=ended, summary=summary))
                    (args.output / "stages.json").write_text(json.dumps(results, indent=2) + "\n")
            selected = {s: choose(stages) for s, stages in results.items()}
            for slot, target in selected.items():
                update(slot, target, "selected measured throughput; 20percent RPM headroom for 10RPM pass")
            validation_start, last_print = time.time(), 0
            while time.time() - validation_start < args.validation_seconds:
                ingest()
                for slot in slots:
                    if any(r["rate_limited"] for r in records if r["slot"] == slot
                           and r["end"] >= validation_start):
                        update(slot, (5, 1), "429 in native validation; conservative fallback")
                        selected[slot] = (5, 1)
                if time.time() - last_print > 30:
                    progress("native-validation")
                    last_print = time.time()
                time.sleep(0.2)
            ingest()
            final = dict(manifest=manifest, finished_at=datetime.now(timezone.utc).isoformat(),
                         selected={s: list(v) for s, v in selected.items()}, stages=results,
                         native_validation_start=validation_start,
                         native_validation={s: summarize([r for r in records if r["slot"] == s and
                             r["start"] >= validation_start], validation_start, time.time()) for s in slots},
                         probe_outcomes=dict(Counter("429" if r["rate_limited"] else "accepted" if r["accepted"]
                             else "other_error" for r in records if r["origin"] == "probe")),
                         successful_probe_usage={k: sum(r.get("usage", {}).get(k, 0) for r in records
                             if r["origin"] == "probe") for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                         limitations=manifest["scope"])
            (args.output / "summary.json").write_text(json.dumps(final, indent=2) + "\n")
            complete = True
            progress("complete")
        finally:
            if not complete:
                for slot in slots:
                    update(slot, (5, 1), "incomplete calibration; conservative fallback")
            traffic.close()


if __name__ == "__main__":
    main()
