"""Bounded RPM probes; persist only public slot labels and numeric evidence.

Each attempt is a new tiny request, never a retry of a benchmark request.
Success is a measured lower bound, not discovery of an account's exact quota.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trievo_bench.provider_profiles import get_provider_profile, provider_credentials


def numeric_rate_headers(headers) -> dict[str, str]:
    """Never save arbitrary headers, which can contain credential echoes."""
    allowed = {
        "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
        "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
    }
    return {
        name.lower(): value for name, value in headers.items()
        if name.lower() in allowed and re.fullmatch(r"[0-9 .;=,smhd-]{1,100}", value)
    }


def probe_request(credential, sequence: int, timeout: float) -> dict:
    profile = get_provider_profile("friday")
    started = time.monotonic()
    result = {"sequence": sequence, "started_at": datetime.now(timezone.utc).isoformat()}
    payload = {
        "model": profile.model,
        "messages": [{"role": "user", "content": f"Reply OK. Capacity probe {sequence}."}],
        "max_tokens": 8, "stream": False, "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        profile.base_url + "/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + credential.value, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            result["http_status"] = response.code
            result["rate_headers"] = numeric_rate_headers(response.headers)
            body = response.read(1024 * 1024)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        status = data.get("status", result["http_status"])
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        result["business_status"] = status
        result["accepted"] = result["http_status"] == 200 and status == 200 and bool(data.get("choices"))
        error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
        result["rate_limited"] = (result["http_status"] == 429 or status == 429
                                  or error_data.get("code") == "rate_limit_exceeded")
        # Extract only numeric RPM declarations; never persist body/message text.
        message = str(data.get("message", "")) + " " + str(error_data.get("message", ""))
        result["limit_mentions_rpm"] = bool(re.search(r"\bRPM\b|requests? per minute|每分钟.*请求", message, re.IGNORECASE))
        result["limit_mentions_tpm"] = bool(re.search(r"\bTPM\b|tokens? per minute", message, re.IGNORECASE))
        result["reported_rpm_values"] = [int(value) for value in re.findall(
            r"(?:\bRPM\b|requests? per minute|每分钟(?:请求)?(?:数|限制)?)\s*[:=：]?\s*(\d{1,8})(?!\d)",
            message, re.IGNORECASE,
        )]
        usage = data.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {key: value for key, value in usage.items()
                               if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                               and type(value) is int}
    except Exception as error:
        # Exception strings can contain URLs, headers or upstream response text.
        result.update(accepted=False, rate_limited=False, error_type=type(error).__name__)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def probe_slot(credential, requests: int, target_rpm: float, timeout: float) -> dict:
    attempts = []
    started = time.monotonic()
    for sequence in range(1, requests + 1):
        due = started + (sequence - 1) * 60 / target_rpm
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        # Never catch up delayed requests with an artificial burst.
        result = probe_request(credential, sequence, timeout)
        attempts.append(result)
        if not result["accepted"]:
            break
        started = max(started, time.monotonic() - sequence * 60 / target_rpm)
    timestamps = [datetime.fromisoformat(row["started_at"]).timestamp()
                  for row in attempts if row["accepted"]]
    return {
        "slot_name": credential.slot, "target_rpm": target_rpm,
        "attempts": attempts, "accepted_requests": sum(row["accepted"] for row in attempts),
        "rate_limited": any(row["rate_limited"] for row in attempts),
        "max_accepted_starts_in_60_seconds": max((
            sum(start <= timestamp < start + 60 for timestamp in timestamps)
            for start in timestamps
        ), default=0),
        "quota_conclusion": "unknown; bounded observations only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id-file", type=Path, help="optional private newline-delimited pool; never copied to output")
    parser.add_argument("--output", type=Path, required=True, help="new public evidence JSON; refuses overwrite")
    parser.add_argument("--requests-per-slot", type=int, default=1)
    parser.add_argument("--target-rpm", type=float, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    args = parser.parse_args()
    if not 1 <= args.requests_per_slot <= 120 or not 1 <= args.target_rpm <= 120 or not 0 < args.timeout_seconds <= 60:
        parser.error("probe limits: requests 1..120, target RPM [1,120], timeout (0,60]")
    if args.output.exists():
        parser.error("probe output already exists; choose a new evidence path")
    environment = dict(os.environ)
    if args.app_id_file:
        try:
            values = [line.strip() for line in args.app_id_file.read_text().splitlines() if line.strip()]
        except OSError:
            parser.error("cannot read private App ID pool")
        environment = {key: value for key, value in environment.items() if not key.startswith("FRIDAY_APP_ID")}
        environment.update({f"FRIDAY_APP_ID_{i}": value for i, value in enumerate(values, 1)})
    credentials = provider_credentials("friday", environment)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve before any paid request, so an existing evidence file is never replaced.
    with args.output.open("x", encoding="utf-8") as output:
        report = {
            "schema": "friday_capacity_probe/1.0", "started_at": datetime.now(timezone.utc).isoformat(),
            "model": get_provider_profile("friday").model, "thinking": "disabled", "max_tokens": 8,
            "requests_per_slot_cap": args.requests_per_slot, "target_rpm": args.target_rpm,
            "request_retries": 0, "max_inflight_per_slot": 1,
            "limitations": "Tiny-request RPM observations; no exact quota, TPM, native parallelism or shared traffic guarantee.",
        }
        with ThreadPoolExecutor(max_workers=min(8, len(credentials))) as executor:
            futures = [executor.submit(probe_slot, credential, args.requests_per_slot, args.target_rpm, args.timeout_seconds)
                       for credential in credentials]
            report["slots"] = [future.result() for future in futures]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "slots": [
        {key: slot[key] for key in ("slot_name", "accepted_requests", "rate_limited")}
        | {"first_attempt": slot["attempts"][0]} for slot in report["slots"]
    ]}, ensure_ascii=False, indent=2))
    return 0 if all(slot["accepted_requests"] == args.requests_per_slot for slot in report["slots"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
