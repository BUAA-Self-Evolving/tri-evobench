from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from tools.prepare_friday_fast_config import prepare
from trievo_bench.friday_gateway import FridayGateway, SharedPacer, check_gateway
from trievo_bench.model import TokenBudget, client_from_config
from trievo_bench.provider_profiles import ProviderCredential, configure_provider_environment

CREDENTIALS = (ProviderCredential("FRIDAY_APP_ID_1", "private-one"),
               ProviderCredential("FRIDAY_APP_ID_2", "private-two"))
PAYLOAD = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "mock-only"}], "max_tokens": 8}
FAST_TEST_POLICY = {"rpm": 120, "max_inflight": 2, "cooldown_seconds": 60}


@contextmanager
def server(handler):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    instance.daemon_threads = True
    instance.handle_error = lambda *args: None
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()


@contextmanager
def gateway(tmp_path, upstream, *, state_name="budget.sqlite", timeout=3, policy=None):
    instance = FridayGateway(CREDENTIALS, tmp_path / state_name, port=0,
        policy=policy or FAST_TEST_POLICY, timeout=timeout,
        upstream_url=f"http://127.0.0.1:{upstream.server_port}")
    threading.Thread(target=instance.server.serve_forever, daemon=True).start()
    try:
        yield instance
    finally:
        instance.close()


def post(base, *, key="private-one", payload=None):
    request = urllib.request.Request(base + "/chat/completions",
        data=json.dumps(PAYLOAD if payload is None else payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, dict(response.headers), response.read()


def test_shared_pacer_spacing_inflight_expiry_cooldown_and_private_state(tmp_path, monkeypatch):
    first = SharedPacer(tmp_path / "state.sqlite")
    second = SharedPacer(tmp_path / "state.sqlite")
    identity = first.identity("private-one")
    first.register(identity, 10, 2)
    second.register(identity, 10, 2)
    a, wait = first.try_acquire(identity, now=100, lease_seconds=50)
    assert a and wait == 0
    assert second.try_acquire(identity, now=100, lease_seconds=50) == (None, 6)
    b, _ = second.try_acquire(identity, now=106, lease_seconds=50)
    assert b
    assert first.try_acquire(identity, now=112, lease_seconds=50)[0] is None
    first.release(a)
    c, _ = first.try_acquire(identity, now=112, lease_seconds=50)
    assert c
    monkeypatch.setattr(time, "time", lambda: 120)
    second.cooldown(identity, 60)
    assert first.try_acquire(identity, now=170, lease_seconds=10) == (None, 10)
    assert second.try_acquire(identity, now=180, lease_seconds=10)[0]
    with pytest.raises(ValueError, match="differs"):
        first.register(identity, 20, 2)
    assert b"private-one" not in first.path.read_bytes()


def test_shared_pacer_processes_cannot_all_admit_at_same_timestamp(tmp_path):
    path = tmp_path / "state.sqlite"
    pacer = SharedPacer(path)
    pacer.register("opaque-test-identity", 10, 2)
    code = ("from pathlib import Path; from trievo_bench.friday_gateway import SharedPacer; "
            "import sys; p=SharedPacer(Path(sys.argv[1])); "
            "print(int(p.try_acquire('opaque-test-identity',now=100,lease_seconds=50)[0] is not None))")
    children = [subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True) for _ in range(4)]
    assert sum(int(child.communicate(timeout=10)[0]) for child in children) == 1
    assert all(child.returncode == 0 for child in children)


def test_explicit_slot_policy_restart_preserves_budget_and_leases(tmp_path):
    from tools.run_friday_fast_matrix import ensure_gateway
    import os
    policy = {"rpm": 5, "max_inflight": 2, "cooldown_seconds": 60,
              "slot_overrides": {"FRIDAY_APP_ID_2": {"rpm": 5, "max_inflight": 1}}}
    path = tmp_path / "state.sqlite"
    pacer = SharedPacer(path)
    for credential in CREDENTIALS:
        pacer.register(pacer.identity(credential.value), 5, 1 if credential.slot.endswith("_2") else 2)
    lease, _ = pacer.try_acquire(pacer.identity("private-two"), now=time.time(), lease_seconds=300)
    assert lease
    with pacer.connection() as database:
        budgets_before = database.execute("SELECT * FROM budgets ORDER BY identity").fetchall()
        leases_before = database.execute("SELECT * FROM leases").fetchall()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    configuration = {"host_base_url": f"http://127.0.0.1:{port}/v1/openai/native", "policy": policy}
    environment = {key: value for key, value in os.environ.items() if not key.startswith("FRIDAY_APP_ID")}
    environment.update({credential.slot: credential.value for credential in CREDENTIALS})
    child = ensure_gateway(configuration, CREDENTIALS, environment, path)
    try:
        check_gateway(configuration["host_base_url"], CREDENTIALS, policy)
        with pytest.raises(ValueError):
            check_gateway(configuration["host_base_url"], CREDENTIALS, {**policy, "rpm": 6})
        with pacer.connection() as database:
            assert database.execute("SELECT * FROM budgets ORDER BY identity").fetchall() == budgets_before
            assert database.execute("SELECT * FROM leases").fetchall() == leases_before
        assert pacer.try_acquire(pacer.identity("private-two"), now=time.time()+60, lease_seconds=5)[0] is None
    finally:
        child.terminate(); child.wait(timeout=5)


@pytest.mark.parametrize("overrides", [{"UNKNOWN": {"rpm": 5, "max_inflight": 1}},
    {"FRIDAY_APP_ID_9": {"rpm": 5, "max_inflight": 1}},
    {"FRIDAY_APP_ID_2": {"rpm": 5, "max_inflight": 0}},
    {"FRIDAY_APP_ID_2": {"rpm": 5, "max_inflight": True}}])
def test_invalid_slot_policy_rejected_before_persisting_state(tmp_path, overrides):
    with pytest.raises(ValueError, match="slot policy"):
        FridayGateway(CREDENTIALS, tmp_path / "state.sqlite", port=0,
            policy={"rpm": 5, "max_inflight": 2, "cooldown_seconds": 60, "slot_overrides": overrides})
    assert not (tmp_path / "state.sqlite").exists()


def test_two_gateways_share_spacing_and_preserve_requests(tmp_path):
    arrivals = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            arrivals.append((time.monotonic(), self.headers["Authorization"], body, self.path))
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')
    with server(Upstream) as upstream, gateway(tmp_path, upstream) as one, gateway(tmp_path, upstream) as two:
        check_gateway(one.base_url, CREDENTIALS, FAST_TEST_POLICY)
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(post, instance.base_url) for instance in (one, two)]
            assert all(future.result()[0] == 200 for future in futures)
        assert len(arrivals) == 2
        assert arrivals[1][0] - arrivals[0][0] >= 0.47
        assert all(row[1:] == ("Bearer private-one", PAYLOAD, "/chat/completions") for row in arrivals)
        assert post(one.base_url, key="wrong")[0] == 401
        assert post(one.base_url, payload={"model": "wrong"})[0] == 400
        assert len(arrivals) == 2


@pytest.mark.parametrize("business", [False, True])
def test_rate_limit_never_replays_and_cools_shared_account(tmp_path, business):
    calls = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(1)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200 if business else 429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":429,"message":"private-one RPM quota"}')
    with server(Upstream) as upstream, gateway(tmp_path, upstream) as instance:
        status, headers, body = post(instance.base_url)
        assert status == 429
        assert int(headers["Retry-After"]) >= 60
        assert b"private-one" not in body
        assert calls == [1]
        lease, wait = instance.pacer.try_acquire(instance.pacer.identity("private-one"), now=time.time(), lease_seconds=5)
        assert lease is None and wait > 59
        assert instance.metrics[CREDENTIALS[0].slot] == {"upstream_attempts": 1, "rate_limits": 1}


def test_streaming_bytes_are_forwarded_without_protocol_rewrite(tmp_path):
    events = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b'data: [DONE]\n\n']
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for event in events:
                self.wfile.write(event); self.wfile.flush(); time.sleep(0.02)
    with server(Upstream) as upstream, gateway(tmp_path, upstream) as instance:
        status, headers, body = post(instance.base_url, payload={**PAYLOAD, "stream": True})
        assert status == 200
        assert headers["Content-Type"] == "text/event-stream"
        assert body == b"".join(events)


def test_disconnected_queued_request_is_not_dispatched(tmp_path):
    calls = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(1); self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    with server(Upstream) as upstream, gateway(tmp_path, upstream) as instance:
        assert post(instance.base_url)[0] == 200
        body = json.dumps(PAYLOAD).encode()
        with socket.create_connection(instance.server.server_address) as connection:
            connection.sendall((f"POST /v1/openai/native/chat/completions HTTP/1.0\r\n"
                f"Authorization: Bearer private-one\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        time.sleep(0.65)
        assert calls == [1]


def test_gateway_routes_host_evaluator_and_rejects_external_bypass(monkeypatch):
    url = "http://127.0.0.1:18765/v1/openai/native"
    environment = {"FRIDAY_APP_ID": "private-one", "TRIEVO_FRIDAY_GATEWAY_URL": url}
    configure_provider_environment("friday", environment)
    assert environment["TRIEVO_BASE_URL"] == url
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    client = client_from_config({"base_url": "https://aigc.sankuai.com/v1/openai/native"}, TokenBudget())
    assert client.base_url == url
    with pytest.raises(ValueError, match="outside"):
        client_from_config({"base_url": "https://elsewhere.test/v1"}, TokenBudget())


def test_fast_preset_routes_all_configs_and_preserves_dsh_contract(tmp_path):
    source = tmp_path / "source.json"
    original = {"adapter": {"command": ["docker", "run", "-i", "{image}"]},
        "options": {"image": "test-image", "base_url": "https://aigc.sankuai.com/v1/openai/native",
                    "execution_profile": "opencode-full-stream-20260928", "max_output_tokens": 4096,
                    "context_window": 128000}, "state": {"paths": ["session.json"]}}
    source.write_text(json.dumps(original))
    output = prepare(tmp_path / "fast", dsh_source=source)
    assert json.loads(source.read_text()) == original
    profile = json.loads((output / "provider-profile.json").read_text())
    route = profile["friday_gateway"]["container_base_url"]
    files = list(output.glob("*.override.json"))
    assert len(files) == 12
    for path in files:
        data = json.loads(path.read_text())
        assert data["environment"]["set"]["TRIEVO_BASE_URL"] == route
        assert "https://aigc.sankuai.com" not in path.read_text()
        assert "TRIEVO_BASE_URL" in data["adapter"]["command"]
    dsh = json.loads((output / "tmp_deepseek-harness_docker.override.json").read_text())
    assert dsh["options"] | {"base_url": original["options"]["base_url"]} == original["options"]
    assert dsh["state"] == original["state"]
    assert route in (output / "hermes-l1-seed/config.yaml").read_text()
    preset = json.loads((output / "execution-preset.json").read_text())
    assert preset["global_workers"] == 8
    assert preset["request_policy"]["rpm"] == 10
    assert set(preset["worker_capacities"].values()) == {2}
    with pytest.raises(ValueError, match="exists"):
        prepare(output)


def test_slow_stream_has_absolute_deadline_and_no_replay(tmp_path):
    calls = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(1); self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for _ in range(30):
                self.wfile.write(b"data: x\n\n"); self.wfile.flush(); time.sleep(0.04)
    with server(Upstream) as upstream, gateway(tmp_path, upstream, timeout=0.2) as instance:
        started = time.monotonic()
        status, _, body = post(instance.base_url)
        assert status == 200 and body
        assert time.monotonic() - started < 0.7
        assert calls == [1]


def test_models_discovery_uses_same_authenticated_budget(tmp_path):
    calls = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
    with server(Upstream) as upstream, gateway(tmp_path, upstream) as instance:
        request = urllib.request.Request(instance.base_url + "/models", headers={"Authorization": "Bearer private-one"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response) == {"data": []}
        assert calls == ["/models"]
        assert instance.metrics[CREDENTIALS[0].slot]["upstream_attempts"] == 1


@pytest.mark.parametrize("policy", [None, "continue", "stop"])
def test_fast_launcher_dry_run_starts_no_gateway_or_run_directory(tmp_path, policy):
    output = prepare(tmp_path / "fast")
    credentials = tmp_path / "private.txt"
    credentials.write_text("\n".join(f"fake-secret-{i}" for i in range(6)))
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "not-created"
    result = subprocess.run([sys.executable, str(repo / "tools/run_friday_fast_matrix.py"),
        "--app-id-file", str(credentials), "--config-dir", str(output), "--root", str(root),
        "--level", "l1", "--l1-package", str(repo / "benchmarks/current/l1_arena"),
        "--baseline", "opencode", "--dry-run",
        *(["--job-error-policy", policy] if policy else [])], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["concurrency"] == 8
    assert plan["job_error_policy"] == (policy or "continue")
    assert plan["provider_gateway"]["policy"]["rpm"] == 10
    assert "fake-secret" not in result.stdout + result.stderr
    assert not root.exists()


def test_driver_gateway_check_fails_before_launching_paid_workers(tmp_path, monkeypatch):
    from tools import run_new_package_matrix as driver
    import trievo_bench.friday_gateway as module
    output = prepare(tmp_path / "fast")
    credentials = tuple(ProviderCredential(f"FRIDAY_APP_ID_{i}", f"fake-{i}") for i in range(1, 7))
    row = {"id": "job", "level": "l1", "flow_id": "s", "agent": "opencode", "turns": ["t"], "output": str(tmp_path / "job")}
    monkeypatch.setenv("TRIEVO_FRIDAY_GATEWAY_URL", "http://127.0.0.1:18765/v1/openai/native")
    monkeypatch.setattr(driver, "provider_credentials", lambda *args: credentials)
    monkeypatch.setattr(driver, "jobs", lambda *args, **kwargs: [row])
    monkeypatch.setattr(sys, "argv", ["matrix", "--provider-mode", "friday", "--config-dir", str(output), "--root", str(tmp_path / "run")])
    def unavailable(*args):
        raise ValueError("gateway deliberately unavailable")
    def forbidden(*args, **kwargs):
        pytest.fail("unavailable gateway reached paid-worker dispatch")
    monkeypatch.setattr(module, "check_gateway", unavailable)
    monkeypatch.setattr(driver.subprocess, "Popen", forbidden)
    monkeypatch.setattr(driver, "save", forbidden)
    with pytest.raises(SystemExit, match="deliberately unavailable"):
        driver.main()


def test_gateway_daemon_start_and_reuse_requires_no_paid_requests(tmp_path):
    import os
    from tools.run_friday_fast_matrix import ensure_gateway
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    configuration = {"host_base_url": f"http://127.0.0.1:{port}/v1/openai/native",
                     "policy": {"rpm": 10, "max_inflight": 2, "cooldown_seconds": 60}}
    environment = {key: value for key, value in os.environ.items() if not key.startswith("FRIDAY_APP_ID")}
    environment.update({credential.slot: credential.value for credential in CREDENTIALS})
    child = ensure_gateway(configuration, CREDENTIALS, environment, tmp_path / "state.sqlite")
    try:
        assert child.poll() is None
        assert ensure_gateway(configuration, CREDENTIALS, environment, tmp_path / "state.sqlite") is None
        request = urllib.request.Request(configuration["host_base_url"].removesuffix("/v1/openai/native") + "/metrics",
            headers={"Authorization": "Bearer private-one"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["upstream_attempts"] == 0
    finally:
        child.terminate(); child.wait(timeout=5)
    assert "private-one" not in (tmp_path / "friday-gateway.log").read_text()


def test_readiness_checks_actual_docker_command_image(tmp_path, monkeypatch):
    from tools import run_friday_fast_matrix as launch
    prepare(tmp_path / "fast")
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="")
    monkeypatch.setattr(launch.shutil, "which", lambda *args, **kwargs: "docker")
    monkeypatch.setattr(launch.subprocess, "run", run)
    launch.check_readiness(tmp_path / "fast", ["skillopt"], {})
    assert commands[0] == ["docker", "image", "inspect", "trievo-skillopt:matrix-current"]
