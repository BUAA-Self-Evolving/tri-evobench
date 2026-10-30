"""Local, credential-authenticated Friday gateway with shared request pacing.

The gateway makes exactly one upstream attempt per admitted request. SQLite
shares start spacing, in-flight leases and 429 cooldowns across gateway
processes on the same host. It never stores prompts, replies or credentials.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .provider_profiles import ProviderCredential, get_provider_profile
from .performance import record

GATEWAY_SCHEMA = "friday_shared_gateway/1.0"
DEFAULT_POLICY = {"rpm": 10, "max_inflight": 2, "cooldown_seconds": 60}


def validate_policy(policy: dict, slot_names=None) -> None:
    """Validate an explicit restart policy without changing persisted budgets."""
    if not isinstance(policy, dict) or set(policy) - (set(DEFAULT_POLICY) | {"slot_overrides"}):
        raise ValueError("invalid Friday gateway policy")
    def limits(value):
        return (isinstance(value, dict) and set(value) == {"rpm", "max_inflight"}
                and type(value["rpm"]) in {int, float} and 0 < value["rpm"] <= 120
                and type(value["max_inflight"]) is int and 1 <= value["max_inflight"] <= 16)
    if (not set(DEFAULT_POLICY) <= set(policy)
            or not limits({key: policy[key] for key in ("rpm", "max_inflight")})
            or type(policy["cooldown_seconds"]) not in {int, float}
            or not 1 <= policy["cooldown_seconds"] <= 600):
        raise ValueError("invalid Friday gateway policy")
    overrides = policy.get("slot_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("invalid Friday slot policy")
    for slot, value in overrides.items():
        if (not isinstance(slot, str) or not slot.startswith("FRIDAY_APP_ID_")
                or not slot.removeprefix("FRIDAY_APP_ID_").isdigit()
                or not limits(value) or slot_names is not None and slot not in slot_names):
            raise ValueError("invalid Friday slot policy")


def slot_policy(policy: dict, slot: str) -> dict:
    return {**{key: policy[key] for key in DEFAULT_POLICY}, **policy.get("slot_overrides", {}).get(slot, {})}


def validate_host_gateway_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or not parsed.port or parsed.username or parsed.password
            or parsed.path != "/v1/openai/native" or parsed.query or parsed.fragment):
        raise ValueError("Friday host gateway must use an explicit loopback HTTP port and /v1/openai/native")
    return value.rstrip("/")


class SharedPacer:
    """Smooth start pacing plus bounded, expiring in-flight leases."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS budgets (
                    identity TEXT PRIMARY KEY, rpm REAL NOT NULL, max_inflight INTEGER NOT NULL,
                    next_start REAL NOT NULL DEFAULT 0, cooldown REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, expires REAL NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    def connection(self):
        return closing(sqlite3.connect(self.path, timeout=10, isolation_level=None))

    @staticmethod
    def identity(value: str) -> str:
        # Private local state contains opaque digests, never an auth value.
        return hashlib.sha256(value.encode()).hexdigest()

    def register(self, identity: str, rpm: float, max_inflight: int) -> None:
        if not 0 < rpm <= 120 or type(max_inflight) is not int or not 1 <= max_inflight <= 16:
            raise ValueError("invalid Friday request budget")
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            previous = database.execute("SELECT rpm,max_inflight FROM budgets WHERE identity=?", (identity,)).fetchone()
            if previous and previous != (rpm, max_inflight):
                database.rollback()
                raise ValueError("shared Friday budget differs; stop users before changing its state/policy")
            database.execute("INSERT OR IGNORE INTO budgets(identity,rpm,max_inflight) VALUES(?,?,?)",
                             (identity, rpm, max_inflight))
            database.commit()

    def try_acquire(self, identity: str, *, now: float, lease_seconds: float) -> tuple[str | None, float]:
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT rpm,max_inflight,next_start,cooldown FROM budgets WHERE identity=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("unknown Friday request budget")
            rpm, maximum, next_start, cooldown = row
            database.execute("DELETE FROM leases WHERE expires<=?", (now,))
            count = database.execute("SELECT COUNT(*) FROM leases WHERE identity=?", (identity,)).fetchone()[0]
            wait = max(0, next_start - now, cooldown - now)
            lease = None
            if count < maximum and wait <= 0:
                lease = uuid.uuid4().hex
                database.execute("INSERT INTO leases VALUES(?,?,?)", (lease, identity, now + lease_seconds))
                database.execute("UPDATE budgets SET next_start=? WHERE identity=?", (now + 60 / rpm, identity))
            database.commit()
        return lease, max(wait, 0.1 if count >= maximum else 0)

    def acquire(self, identity: str, *, deadline: float, lease_seconds: float, cancelled) -> str:
        while not cancelled():
            if time.monotonic() >= deadline:
                break
            lease, wait = self.try_acquire(identity, now=time.time(), lease_seconds=lease_seconds)
            if lease:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(wait, 0.05), 0.25, remaining))
        raise TimeoutError("Friday admission expired before upstream dispatch")

    def release(self, lease: str) -> None:
        with self.connection() as database:
            database.execute("DELETE FROM leases WHERE id=?", (lease,))

    def cooldown(self, identity: str, seconds: float) -> None:
        with self.connection() as database:
            database.execute("UPDATE budgets SET cooldown=MAX(cooldown,?) WHERE identity=?", (time.time() + seconds, identity))


class FridayGateway:
    def __init__(
        self, credentials: tuple[ProviderCredential, ...], state: Path,
        *, port: int = 18765, policy: dict | None = None,
        upstream_url: str | None = None, timeout: float = 900, queue_timeout: float = 240,
        model: str | None = None,
    ):
        self.policy = dict(DEFAULT_POLICY if policy is None else policy)
        validate_policy(self.policy, {credential.slot for credential in credentials})
        self.model = get_provider_profile("friday", model=model).model
        self.upstream_url = (upstream_url or get_provider_profile("friday").base_url).rstrip("/")
        self.timeout, self.queue_timeout = timeout, queue_timeout
        self.pacer = SharedPacer(state)
        self.timing_path = state.with_suffix(".timings.jsonl")
        self.credentials = {credential.value: credential for credential in credentials}
        if not self.credentials or len(self.credentials) != len(credentials):
            raise ValueError("Friday gateway requires a nonempty unique credential pool")
        for credential in credentials:
            effective = slot_policy(self.policy, credential.slot)
            self.pacer.register(self.pacer.identity(credential.value), effective["rpm"], effective["max_inflight"])
        self.metrics = {credential.slot: {"upstream_attempts": 0, "rate_limits": 0} for credential in credentials}
        self.metrics_lock = threading.Lock()
        self.stopping = threading.Event()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def handle(self):
                try:
                    self.connection.settimeout(10)
                    super().handle()
                except (ConnectionError, TimeoutError, OSError):
                    pass

            def credential(self):
                authorization = self.headers.get("Authorization", "")
                value = authorization[7:] if authorization.startswith("Bearer ") else self.headers.get("x-api-key", "")
                return gateway.credentials.get(value)

            def send_json(self, status, payload, retry_after=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if retry_after is not None:
                    self.send_header("Retry-After", str(int(retry_after)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                credential = self.credential()
                if credential is None:
                    self.send_json(401, {"error": "Friday gateway authentication required"})
                    return
                if self.path == "/health":
                    self.send_json(200, {"schema": GATEWAY_SCHEMA, "model": gateway.model,
                                         "policy": gateway.policy, "slot_name": credential.slot})
                elif self.path == "/metrics":
                    with gateway.metrics_lock:
                        metrics = dict(gateway.metrics[credential.slot])
                    self.send_json(200, {"slot_name": credential.slot, **metrics})
                elif self.path == "/v1/openai/native/models":
                    self.forward(credential, "/models", None)
                else:
                    self.send_json(404, {"error": "unsupported Friday gateway path"})

            def do_POST(self):
                credential = self.credential()
                if credential is None:
                    self.send_json(401, {"error": "Friday gateway authentication required"})
                    return
                prefix = "/v1/openai/native"
                suffix = self.path[len(prefix):] if self.path.startswith(prefix + "/") else ""
                if suffix not in {"/chat/completions", "/messages", "/responses"}:
                    self.send_json(404, {"error": "unsupported Friday gateway path"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32 * 1024 * 1024:
                        raise ValueError("invalid size")
                    body = self.rfile.read(length)
                    payload = json.loads(body)
                    if not isinstance(payload, dict) or payload.get("model") != gateway.model:
                        raise ValueError("unexpected model")
                except (ValueError, UnicodeError):
                    self.send_json(400, {"error": "invalid Friday model request"})
                    return
                self.forward(credential, suffix, body)

            def forward(self, credential, suffix, body):
                started = time.monotonic()
                admitted = dispatched_at = headers_at = first_byte = None
                status = None
                business_status = None
                identity = gateway.pacer.identity(credential.value)
                lease = None
                dispatched = False
                sent_headers = False
                try:
                    # A disconnected queued client must not cause a paid call.
                    def cancelled():
                        if gateway.stopping.is_set():
                            return True
                        import select
                        if select.select([self.connection], [], [], 0)[0]:
                            return not self.connection.recv(1, socket.MSG_PEEK)
                        return False
                    lease = gateway.pacer.acquire(identity,
                        deadline=time.monotonic() + gateway.queue_timeout,
                        lease_seconds=gateway.timeout + 10, cancelled=cancelled)
                    admitted = time.monotonic()
                    if cancelled():
                        return
                    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + credential.value}
                    for name in ("anthropic-version", "anthropic-beta", "OpenAI-Beta"):
                        if name in self.headers:
                            headers[name] = self.headers[name]
                    request = urllib.request.Request(gateway.upstream_url + suffix, data=body, headers=headers)
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with gateway.metrics_lock:
                        gateway.metrics[credential.slot]["upstream_attempts"] += 1
                    dispatched = True
                    dispatched_at = time.monotonic()
                    deadline = time.monotonic() + gateway.timeout
                    try:
                        response = opener.open(request, timeout=gateway.timeout)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        headers_at = time.monotonic()
                        status = response.code
                        if status >= 400:
                            delay = gateway.policy["cooldown_seconds"]
                            if status == 429:
                                try:
                                    delay = max(delay, min(600, float(response.headers.get("Retry-After", "0"))))
                                except ValueError:
                                    pass
                                gateway.pacer.cooldown(identity, delay)
                                with gateway.metrics_lock:
                                    gateway.metrics[credential.slot]["rate_limits"] += 1
                            self.send_json(status, {"error": {"type": "rate_limit_error" if status == 429 else "api_error",
                                "message": f"Friday upstream HTTP {status}; no gateway retry"}}, delay if status == 429 else None)
                            return
                        content_type = response.headers.get("Content-Type", "application/json")
                        if "text/event-stream" not in content_type.lower():
                            chunks = []
                            count = 0
                            while time.monotonic() < deadline:
                                raw = getattr(getattr(response, "fp", None), "raw", None)
                                upstream_socket = getattr(raw, "_sock", None)
                                if upstream_socket:
                                    upstream_socket.settimeout(max(0.01, deadline - time.monotonic()))
                                chunk = response.read1(64 * 1024)
                                if not chunk:
                                    break
                                if first_byte is None:
                                    first_byte = time.monotonic()
                                count += len(chunk)
                                if count > 32 * 1024 * 1024:
                                    raise ValueError("Friday response size limit")
                                chunks.append(chunk)
                            else:
                                raise TimeoutError("Friday response deadline")
                            result_body = b"".join(chunks)
                            try:
                                result = json.loads(result_body)
                                business_status = int(result.get("status", 200)) if isinstance(result, dict) else 200
                            except (ValueError, UnicodeError, TypeError):
                                business_status = 200
                            if 400 <= business_status <= 599 and not result.get("choices"):
                                delay = None
                                if business_status == 429:
                                    delay = gateway.policy["cooldown_seconds"]
                                    gateway.pacer.cooldown(identity, delay)
                                    with gateway.metrics_lock:
                                        gateway.metrics[credential.slot]["rate_limits"] += 1
                                self.send_json(business_status, {"error": {
                                    "type": "rate_limit_error" if business_status == 429 else "api_error",
                                    "message": f"Friday upstream business status {business_status}; no gateway retry"}}, delay)
                                return
                            self.send_response(status)
                            self.send_header("Content-Type", content_type)
                            self.send_header("Content-Length", str(len(result_body)))
                            self.end_headers()
                            sent_headers = True
                            self.wfile.write(result_body)
                            return
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        sent_headers = True
                        count = 0
                        while time.monotonic() < deadline:
                            raw = getattr(getattr(response, "fp", None), "raw", None)
                            upstream_socket = getattr(raw, "_sock", None)
                            if upstream_socket:
                                upstream_socket.settimeout(max(0.01, deadline - time.monotonic()))
                            chunk = response.read1(64 * 1024)
                            if not chunk:
                                return
                            if first_byte is None:
                                first_byte = time.monotonic()
                            count += len(chunk)
                            if count > 32 * 1024 * 1024:
                                raise ValueError("Friday response size limit")
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        raise TimeoutError("Friday response deadline")
                except Exception:
                    # Never log upstream exception strings, URLs, bodies or keys.
                    if not sent_headers:
                        self.send_json(502 if dispatched else 503, {"error": {"type": "api_error",
                            "message": "Friday gateway transport failed after dispatch; no gateway retry" if dispatched
                            else "Friday gateway admission expired; no upstream request dispatched"}})
                finally:
                    if lease:
                        gateway.pacer.release(lease)
                    record(gateway.timing_path, {
                        "stage": "provider_request", "slot": credential.slot, "endpoint": suffix,
                        "dispatched": dispatched, "upstream_status": status,
                        "business_status": business_status,
                        "queue_seconds": (admitted or time.monotonic()) - started,
                        "headers_seconds": headers_at - dispatched_at if headers_at is not None else None,
                        "first_body_byte_seconds": first_byte - dispatched_at if first_byte is not None else None,
                        "elapsed_seconds": time.monotonic() - started,
                    })

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *args: None  # Never dump request context.

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1/openai/native"

    def close(self):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()


def check_gateway(base_url: str, credentials: tuple[ProviderCredential, ...], policy: dict, model: str | None = None) -> None:
    """Verify every worker credential and policy before paid workers are admitted."""
    validate_host_gateway_url(base_url)
    health = base_url.removesuffix("/v1/openai/native") + "/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for credential in credentials:
        request = urllib.request.Request(health, headers={"Authorization": "Bearer " + credential.value})
        try:
            with opener.open(request, timeout=3) as response:
                data = json.load(response)
            if (data.get("schema") != GATEWAY_SCHEMA or data.get("policy") != policy
                    or data.get("model") != get_provider_profile("friday", model=model).model
                    or data.get("slot_name") != credential.slot):
                raise ValueError("gateway policy mismatch")
        except Exception:
            raise ValueError("Friday shared gateway is unavailable or differs from this configuration") from None
