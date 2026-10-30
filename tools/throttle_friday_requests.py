"""Apply an audited, reduction-only override to the live shared request pacer.

Existing gateway processes read rpm/max_inflight from SQLite on each admission.
This operation preserves leases, cooldowns, frozen configurations and workers.
The gateway health policy remains its startup declaration; the receipt records
the effective override. Restarting under the old declaration fails closed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_friday_gateway import load_pool
from trievo_bench.friday_gateway import SharedPacer, check_gateway


def reduce_limits(state, credentials, *, rpm, max_inflight, expected_rpm,
                  expected_inflight, audit_dir, reason, now=None):
    """Atomically reduce all selected accounts, without deleting any live lease."""
    if (isinstance(rpm, bool) or not math.isfinite(rpm) or not 0 < rpm <= 120
            or type(max_inflight) is not int or not 1 <= max_inflight <= 16):
        raise ValueError("invalid request limits")
    if rpm > expected_rpm or max_inflight > expected_inflight:
        raise ValueError("only reductions are allowed")
    if rpm == expected_rpm and max_inflight == expected_inflight:
        raise ValueError("limits must be reduced")
    if not reason.strip():
        raise ValueError("an audit reason is required")
    accounts = [(c.slot, SharedPacer.identity(c.value)) for c in credentials]
    if (not accounts or len({i for _, i in accounts}) != len(accounts)
            or len({s for s, _ in accounts}) != len(accounts)):
        raise ValueError("credential pool must be nonempty and unique")
    state, audit_dir = Path(state).resolve(), Path(audit_dir)
    database = sqlite3.connect(state.as_uri() + "?mode=rw", uri=True,
                               timeout=10, isolation_level=None)
    try:
        database.execute("BEGIN IMMEDIATE")
        timestamp = time.time() if now is None else now
        before = []
        for slot, identity in accounts:
            row = database.execute(
                "SELECT rpm,max_inflight,next_start,cooldown FROM budgets WHERE identity=?",
                (identity,)).fetchone()
            if row is None or row[:2] != (expected_rpm, expected_inflight):
                raise ValueError("effective limits differ from expected; nothing changed")
            leases = database.execute(
                "SELECT COUNT(*) FROM leases WHERE identity=? AND expires>?",
                (identity, timestamp)).fetchone()[0]
            before.append(dict(slot=slot, rpm=row[0], max_inflight=row[1],
                               next_start=row[2], cooldown=row[3], active_leases=leases))
        receipt = dict(schema="friday_request_reduction/1.0",
                       at=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                       state=str(state), reason=reason, before=before,
                       effective=dict(rpm=rpm, max_inflight=max_inflight),
                       minimum_next_start=timestamp + 60 / rpm,
                       semantics="Existing leases drain; no retry, cancellation or worker change. "
                       "Health reports startup policy, not this override. "
                       "Old-policy gateway restart is rejected by register().")
        # Audit must exist durably before the transaction can mutate the budget.
        audit_dir.mkdir(parents=True, exist_ok=False)
        with (audit_dir / "intent.json").open("x") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
            handle.flush()
            import os
            os.fsync(handle.fileno())
        for _, identity in accounts:
            database.execute(
                "UPDATE budgets SET rpm=?,max_inflight=?,next_start=MAX(next_start,?) "
                "WHERE identity=?", (rpm, max_inflight, timestamp + 60 / rpm, identity))
        database.commit()
        # If receipt writing fails after commit, intent remains: inspect live DB,
        # never automatically rerun or undo an operation of uncertain outcome.
        applied = {**receipt, "committed": True,
                   "receipt_at": datetime.now(timezone.utc).isoformat()}
        with (audit_dir / "applied.json").open("x") as handle:
            json.dump(applied, handle, indent=2)
            handle.write("\n")
        return applied
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--app-id-file", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--rpm", type=float, required=True)
    parser.add_argument("--max-inflight", type=int, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text())["friday_gateway"]
    credentials = load_pool(args.app_id_file)
    check_gateway(profile["host_base_url"], credentials, profile["policy"], profile["model"])
    result = reduce_limits(args.state, credentials, rpm=args.rpm,
                           max_inflight=args.max_inflight,
                           expected_rpm=profile["policy"]["rpm"],
                           expected_inflight=profile["policy"]["max_inflight"],
                           audit_dir=args.audit_dir, reason=args.reason)
    print(json.dumps({"committed": result["committed"], "effective": result["effective"],
                      "slots": len(result["before"]), "audit_dir": str(args.audit_dir)}))


if __name__ == "__main__":
    main()
