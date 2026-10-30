import json
import sqlite3

import pytest

from tools import calibrate_friday_accounts as calibration
from trievo_bench.friday_gateway import SharedPacer
from trievo_bench.provider_profiles import ProviderCredential


def test_authorized_tuning_preserves_leases_cooldown_and_rejects_stale_policy(tmp_path):
    p = SharedPacer(tmp_path / "state.sqlite")
    c = ProviderCredential("slot1", "private-test-key")
    identity = p.identity(c.value)
    p.register(identity, 5, 1)
    lease, _ = p.try_acquire(identity, now=100, lease_seconds=9999999999)
    with sqlite3.connect(p.path) as db:
        db.execute("UPDATE budgets SET cooldown=9999999999 WHERE identity=?", (identity,))
    journal = tmp_path / "events.jsonl"
    assert calibration.set_limits(p.path, c, (10, 2), (5, 1), journal, "authorized test") == (10, 2)
    with sqlite3.connect(p.path) as db:
        assert db.execute("SELECT id FROM leases").fetchall() == [(lease,)]
        assert db.execute("SELECT cooldown FROM budgets").fetchone() == (9999999999,)
    with pytest.raises(ValueError, match="unexpectedly"):
        calibration.set_limits(p.path, c, (5, 1), (5, 1), journal, "stale")
    with pytest.raises(ValueError, match="envelope"):
        calibration.set_limits(p.path, c, (11, 1), (10, 2), journal, "bad")
    assert "private-test-key" not in journal.read_text()


def stage(rpm, cap, throughput, peak, **changes):
    return dict(rpm=rpm, cap=cap, status="observed", summary=dict(
        failures=0, records=20, measured_success_rpm=throughput,
        observed_peak_completed_request_overlap=peak,
        max_recorded_starts_60s=10) | changes)


def test_selection_demands_actual_concurrency_and_sustained_rate():
    baseline = stage(5, 1, 4, 1)
    assert calibration.choose([baseline, stage(10, 4, 10, 2)]) == (5, 1)
    assert calibration.choose([baseline, stage(10, 2, 8, 2)]) == (5, 1)
    assert calibration.choose([baseline, stage(10, 2, 10, 2, failures=1)]) == (5, 1)
    assert calibration.choose([baseline, stage(5, 2, 5, 2), stage(10, 2, 10, 2)]) == (8, 2)


def test_error_diagnostics_keep_types_and_numbers_only():
    d = calibration.diagnose({"error": {"message": "private-key RPM: 7 concurrent requests exceeded"}},
                             429, {"Retry-After": "60", "X-App-Id": "private-key"})
    assert d["rate_limited"] and d["mentions_rpm"] and d["mentions_concurrency"]
    assert d["reported_rpm_values"] == [7]
    assert "private-key" not in json.dumps(d)


def test_probe_never_retries_transport_error_and_releases_lease(tmp_path, monkeypatch):
    p = SharedPacer(tmp_path / "state.sqlite")
    c = ProviderCredential("slot1", "private-key")
    identity = p.identity(c.value)
    p.register(identity, 5, 1)
    lease, _ = p.try_acquire(identity, now=100, lease_seconds=9999999999)
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(1)
            raise TimeoutError("private-key")
    monkeypatch.setattr(calibration.urllib.request, "build_opener", lambda *a: Opener())
    output = tmp_path / "probes.jsonl"
    event = calibration.request_probe(c, p, lease, "test", 1, output)
    assert calls == [1] and event["error_type"] == "TimeoutError"
    assert "private-key" not in output.read_text()
    with sqlite3.connect(p.path) as db:
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone() == (0,)


def test_measurement_excludes_first_minute_and_counts_actual_overlaps():
    rows = [dict(start=t, end=t+15, accepted=True, rate_limited=False, origin="probe")
            for t in (5, 61, 67, 73)]
    s = calibration.summarize(rows, 0, 180)
    assert s["successful"] == 3 and s["observed_peak_completed_request_overlap"] == 3
