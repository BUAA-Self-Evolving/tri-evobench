import json
import urllib.error

from tools import probe_friday_capacity as probe
from trievo_bench.provider_profiles import ProviderCredential


def test_rate_headers_allow_only_numeric_allowlisted_values():
    assert probe.numeric_rate_headers({
        "Authorization": "private-key", "X-App-Id": "private-key",
        "X-Ratelimit-Limit-Requests": "10", "Retry-After": "60",
        "X-Ratelimit-Reset-Requests": "1m2s", "Ratelimit-Limit": "private-key",
    }) == {"x-ratelimit-limit-requests": "10", "retry-after": "60", "x-ratelimit-reset-requests": "1m2s"}


def test_business_429_preserves_numeric_evidence_without_body_or_secret(monkeypatch):
    class Response:
        code = 200
        headers = {"Authorization": "private-key", "Retry-After": "60"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return json.dumps({"status": 429, "message": "private-key RPM: 10"}).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.headers["Authorization"] == "Bearer private-key"
            assert json.loads(request.data)["max_tokens"] == 8
            return Response()
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *args: Opener())
    result = probe.probe_request(ProviderCredential("slot-1", "private-key"), 1, 10)
    assert result["rate_limited"]
    assert result["reported_rpm_values"] == [10]
    assert result["limit_mentions_rpm"]
    assert not result["accepted"]
    assert "private-key" not in json.dumps(result)


def test_network_error_does_not_echo_exception_or_retry(monkeypatch):
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError("private-key in upstream body")
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *args: Opener())
    result = probe.probe_request(ProviderCredential("slot-1", "private-key"), 1, 10)
    assert len(calls) == 1
    assert result["error_type"] == "URLError"
    assert "private-key" not in json.dumps(result)


def test_slot_stops_at_first_rejection_and_never_replays(monkeypatch):
    calls = []
    def request(credential, sequence, timeout):
        calls.append(sequence)
        return {"started_at": "2026-10-17T00:00:00+00:00", "accepted": False, "rate_limited": True}
    monkeypatch.setattr(probe, "probe_request", request)
    result = probe.probe_slot(ProviderCredential("slot-1", "private-key"), 100, 60, 10)
    assert calls == [1]
    assert result["quota_conclusion"].startswith("unknown")
    assert result["max_accepted_starts_in_60_seconds"] == 0
