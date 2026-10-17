#!/usr/bin/env python3
"""Build and qualify live tau2-bench tasks for the L2 V5 task pool.

Each pool task is a live τ² conversation. During the Turn the L2 runtime starts
the τ² session service (``trievo_bench/tau2_live/session``) from this task
image: AReaL's τ² harness (official tau2-bench orchestrator, DeepSeek user
simulator, official evaluator with the NL-assertion judge bound to the user
model) with the agent under test in place of the policy LLM. The agent reads
the policy and tool schemas, talks to the simulated customer and calls the
live domain tools through ``/workspace/.tau2/tau2.py``. The service scores the
finished conversation with ``evaluate_simulation(EvaluationType.ALL)`` and
seals it as a ``trievo_tau2_live_session/1.0`` record, which is the Turn's
inline ``interaction_record`` submission.

Per pool task:

* workspace_seed: empty; the agent's only inputs are the prompt and the live
  session.
* evaluation_assets: the upstream task JSON (private; the runtime copies it
  into the service container, never into the Workspace), ``tau2_meta.json``,
  ``tau2_scoring.py`` and ``source_verifier.py``. The verifier checks that the
  record belongs to this task and revision and re-runs the official evaluator
  offline on the recorded simulation with the recorded NL-judge result.
* fixtures: ``known_correct/response.txt`` and ``known_broken/response.txt``
  are session records (see ``tau2_session.fixtures``).

Commands:
  build-image  build the τ² image: tau2-bench b7ea907 (as pinned by AReaL) plus
               the session package
  build        select tasks and write pool directories
  fixtures     build red/green session records in the image (network on: the
               NL-assertion judge calls the user-simulator model)
  qualify      run both fixtures through source_verifier.py in the image with
               network disabled and record admission

Third-party checkouts are read-only. Python 3.12 jobs use fresh daemon-owned
containers and nonce-checked Docker copy transport, with actual cgroup readback.
Legacy jobs retain their original staging-directory bind transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from trievo_bench.container_resources import (
    ContainerResourceError,
    docker_resource_capability_preflight,
    require_container_resources,
    resource_enforcement_mode,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# tau2-bench at the revision AReaL pins (examples/tau2/contracts.py
# OFFICIAL_TAU2_REVISION), a full source checkout with data, read only.
REVISION = "b7ea9074c1cba482b30687fecdb5c8425fd6f619"
CHECKOUT = Path(os.environ.get(
    "TAU2_CHECKOUT", f"/data_storage/yl_test/lgx/data-1/code/_cache/tau2-bench-{REVISION}"))
BENCHMARK_ID = "tau2_bench"
POOL_ROOT = Path(os.environ.get(
    "TAU2_POOL_ROOT", "/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_pool/tau2"))
STAGING_ROOT = Path("/data_storage/yl_test/lgx/data-1/tmp/l2v5")
IMAGE_TAG = "trievo-l2-v5-tau2:0.2"
DOCKERFILE = REPO_ROOT / "sandbox" / "l2-v5" / "Dockerfile.tau2"
PY312_DOCKERFILE = DOCKERFILE.with_name("Dockerfile.tau2-py312")
SESSION_PACKAGE = REPO_ROOT / "trievo_bench" / "tau2_live" / "session"
RESOURCE_METADATA_KEYS = ("resource_enforcement", "resource_readback", "requested_resources", "observed_resources", "resource_observation_provenance")
SCORING_SOURCE = SESSION_PACKAGE / "scoring.py"
VERIFIER_SOURCE = REPO_ROOT / "tools" / "l2_v5_tau2_live_verifier.py"
V4_ROOT = REPO_ROOT / "benchmarks" / "current" / "L2_Demo_V4"
# The September heterogeneous V5 candidate, read only for source continuity.
V5_ROOT = REPO_ROOT / "benchmarks" / "archive" / "L2_Demo_V5_heterogeneous_candidate"
DOMAINS = ("airline", "retail", "telecom", "banking_knowledge")
RUNTIME_PROFILE = "tau2_live_session_v1"
TURN_TIMEOUT_SECONDS = 1800
# A conversation takes one terminal command per agent move plus reading the
# policy and tool schemas; gold solutions have up to ~25 moves.
MAX_TOOL_CALLS = 60
# Tau2EnvConfig defaults are AReaL config_grpo.yaml econfig values; L2 keeps
# them, so the per-task live config is empty.
LIVE_CONFIG: dict[str, Any] = {}
CREDENTIAL_ENV = "DEEPSEEK_API_KEY"
EXTRA_BANKING = 25
EXTRA_TELECOM_PER_ISSUE = 20
SELECTION_SEED = 20261009
WORKERS = 16

DOMAIN_LABELS = {
    "airline": ("airline_support", "airline customer service"),
    "retail": ("retail_support", "online retail customer service"),
    "telecom": ("telecom_support", "telecom technical support"),
    "banking_knowledge": ("banking_support", "banking customer service"),
}
READ_PREFIXES = ("get_", "find_", "list_", "search_", "calculate", "think", "check_", "can_", "run_speed_test")
AIRLINE_BOOKING = {"book_reservation", "cancel_reservation"}
AIRLINE_CHANGES = {"update_reservation_flights", "update_reservation_baggages", "update_reservation_passengers",
                   "send_certificate"}
RETAIL_POST_DELIVERY = {"return_delivered_order_items", "exchange_delivered_order_items"}
RETAIL_PENDING = {"cancel_pending_order", "modify_pending_order_items", "modify_pending_order_address",
                  "modify_pending_order_payment", "modify_user_address"}


# Wrong-mutation broken fixtures for tasks where a refusal already reproduces
# the gold end state. One write call per domain that succeeds on the initial
# DB and is never part of the gold solution.
BROKEN_MUTATIONS = {
    "airline": ("send_certificate", lambda task, db: {"user_id": _airline_user(task, db), "amount": 100}),
    "retail": ("modify_user_address", lambda task, db: {
        "user_id": _retail_user(task, db), "address1": "1 Wrong Way", "address2": "", "city": "Nowhere",
        "state": "ZZ", "country": "USA", "zip": "00000"}),
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fresh_staging(prefix: str) -> Path:
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=STAGING_ROOT))


def task_dir_name(source_id: str) -> str:
    return source_id.replace(":", "__").replace("/", "__")


# ---------------------------------------------------------------------------
# Upstream tasks
# ---------------------------------------------------------------------------


def domain_dir(domain: str) -> Path:
    return CHECKOUT / "data" / "tau2" / "domains" / domain


def load_upstream(domain: str) -> list[dict[str, Any]]:
    """Tasks as the upstream loader reads them (banking: tasks/task_*.json)."""
    rows = []
    if domain == "banking_knowledge":
        for path in sorted((domain_dir(domain) / "tasks").glob("task_*.json")):
            task = json.loads(path.read_text(encoding="utf-8"))
            rows.append({"task": task, "source_path": f"data/tau2/domains/{domain}/tasks/{path.name}"})
        return rows
    for task in json.loads((domain_dir(domain) / "tasks.json").read_text(encoding="utf-8")):
        rows.append({"task": task, "source_path": f"data/tau2/domains/{domain}/tasks.json#{task['id']}"})
    return rows


def splits(domain: str) -> dict[str, set[str]]:
    path = domain_dir(domain) / "split_tasks.json"
    if not path.is_file():
        return {}
    return {name: set(ids) for name, ids in json.loads(path.read_text(encoding="utf-8")).items()}


def legacy_sources() -> dict[str, set[str]]:
    """tau2 source ids ("domain:task_id") used by L2 Demo V4 and V5."""
    found = {"v4": set(), "v5": set()}
    for label, root in (("v4", V4_ROOT), ("v5", V5_ROOT)):
        for path in root.glob("streams/*/turn*/task.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            basis = data.get("source_basis") or {}
            bench = str(basis.get("benchmark_id", ""))
            sid = str(basis.get("source_task_id", ""))
            if "tau" not in bench.lower() and "tau" not in str(data.get("stream_id", "")).lower():
                continue
            if not sid:
                continue
            if ":" not in sid or sid.split(":", 1)[0] not in DOMAINS:
                # V4 telecom turns record the bare upstream telecom id.
                domain = "telecom" if sid.startswith("[") else None
                if domain is None:
                    continue
                sid = f"{domain}:{sid}"
            found[label].add(sid)
    return found


def write_actions(task: dict[str, Any]) -> list[str]:
    actions = (task.get("evaluation_criteria") or {}).get("actions") or []
    return [a["name"] for a in actions if not a["name"].startswith(READ_PREFIXES)]


def capability_for(domain: str, task: dict[str, Any]) -> tuple[str, str]:
    writes = write_actions(task)
    if domain == "telecom":
        issue = task["id"].split("]", 1)[0].lstrip("[")
        name = {"mobile_data_issue": "mobile_data_troubleshooting", "mms_issue": "mms_troubleshooting",
                "service_issue": "service_troubleshooting"}.get(issue, issue)
        return f"tau2.telecom.{name}", f"upstream telecom issue type [{issue}] in the task id"
    if domain == "airline":
        names = set(writes)
        if names & AIRLINE_BOOKING:
            return "tau2.airline.booking_cancellation", "gold actions book or cancel a reservation"
        if names & AIRLINE_CHANGES:
            return "tau2.airline.reservation_changes", "gold actions modify an existing reservation"
        return "tau2.airline.policy_refusal", "gold actions contain no reservation write (refuse/inform/transfer)"
    if domain == "retail":
        relevant = [w for w in writes if w in RETAIL_POST_DELIVERY | RETAIL_PENDING]
        if not relevant:
            return "tau2.retail.policy_refusal", "gold actions contain no order write (refuse/inform/transfer)"
        if relevant[0] in RETAIL_POST_DELIVERY:
            return "tau2.retail.returns_exchanges", "first gold write returns/exchanges a delivered order"
        return "tau2.retail.order_modification", "first gold write cancels/modifies a pending order or user address"
    return "tau2.banking.knowledge_procedures", "banking_knowledge tasks: KB-guided account procedures"


def select_tasks(legacy: dict[str, set[str]]) -> list[dict[str, Any]]:
    wanted = legacy["v4"] | legacy["v5"]
    selected = []
    rng = random.Random(SELECTION_SEED)
    for domain in DOMAINS:
        rows = load_upstream(domain)
        domain_splits = splits(domain)
        if domain in {"airline", "retail"}:
            chosen = rows
        elif domain == "banking_knowledge":
            legacy_rows = [r for r in rows if f"{domain}:{r['task']['id']}" in wanted]
            rest = [r for r in rows if f"{domain}:{r['task']['id']}" not in wanted]
            chosen = legacy_rows + sorted(rng.sample(rest, min(EXTRA_BANKING, len(rest))), key=lambda r: r["task"]["id"])
        else:
            keep_ids = set(domain_splits.get("test", set())) | set(domain_splits.get("small", set()))
            chosen_ids = {r["task"]["id"] for r in rows if f"{domain}:{r['task']['id']}" in wanted or r["task"]["id"] in keep_ids}
            by_issue: dict[str, list] = {}
            for r in rows:
                if r["task"]["id"] not in chosen_ids:
                    by_issue.setdefault(r["task"]["id"].split("]", 1)[0], []).append(r)
            for issue in sorted(by_issue):
                pool = sorted(by_issue[issue], key=lambda r: r["task"]["id"])
                chosen_ids |= {r["task"]["id"] for r in rng.sample(pool, min(EXTRA_TELECOM_PER_ISSUE, len(pool)))}
            chosen = [r for r in rows if r["task"]["id"] in chosen_ids]
        for row in chosen:
            task = row["task"]
            key = f"{domain}:{task['id']}"
            selected.append({
                "domain": domain, "task": task, "source_path": row["source_path"],
                "source_task_id": key, "source_id": f"{BENCHMARK_ID}:{key}",
                "in_v4": key in legacy["v4"], "in_v5": key in legacy["v5"],
                "splits": sorted(name for name, ids in domain_splits.items() if task["id"] in ids),
            })
    return selected


def _airline_user(task: dict[str, Any], db: dict[str, Any]) -> str:
    text = json.dumps(task.get("user_scenario"))
    for user_id in db.get("users", {}):
        if user_id in text:
            return user_id
    return sorted(db.get("users", {}))[0]


def _retail_user(task: dict[str, Any], db: dict[str, Any]) -> str:
    text = json.dumps(task.get("user_scenario")).lower()
    for user_id, user in db.get("users", {}).items():
        name = user.get("name", {})
        if f"{name.get('first_name', '')} {name.get('last_name', '')}".lower() in text:
            return user_id
    return sorted(db.get("users", {}))[0]



# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

CLI = "/workspace/.tau2/tau2.py"


def build_prompt(domain: str) -> str:
    _, label = DOMAIN_LABELS[domain]
    article = "an" if label[0] in "aeiou" else "a"
    return "\n".join([
        f"This Turn is a live tau2-bench conversation. You are {article} {label} agent, and a simulated customer "
        "is waiting to talk to you. The domain tools are live: each tool call reads or changes the real "
        "environment state for this conversation.",
        "",
        f"Use the command-line client `{CLI}` (run it with `python3`):",
        f"1. `python3 {CLI} policy` prints your instructions and the domain policy. Read it before acting; you "
        "must follow it.",
        f"2. `python3 {CLI} tools` prints the JSON schemas of the tools you can call.",
        f"3. `python3 {CLI} observe` shows whose turn it is and the customer's message waiting for you.",
        f"4. Then, on each of your turns, do exactly one of: `python3 {CLI} say \"<message>\"` to send a message "
        f"to the customer (prints the customer's reply), or `python3 {CLI} call <tool> '<json arguments>'` to "
        "call a tool (prints the result).",
        "",
        "The customer ends the conversation. Keep going until the client reports `\"ended\": true`; if your "
        "work ends while the conversation is still open, the conversation is scored 0. If a command prints "
        f"`\"turn\": \"pending\"`, the reply is still being generated: run `python3 {CLI} observe`.",
        "",
        "Grading uses the official tau2-bench evaluator on the finished conversation: the final environment "
        "state, the required actions, and the information you gave the customer, as in a standard tau2-bench "
        "run. Only the conversation is graded; files in the Workspace are not.",
    ])


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def resource_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Carry actual resource evidence unchanged into portable task records."""
    checked = require_container_resources(snapshot)
    return {"resource_enforcement": checked["resource_enforcement"],
            "resource_readback": checked,
            "requested_resources": checked["validation"]["expected"],
            "observed_resources": checked["validation"]["observed"],
            "resource_observation_provenance": "actual fresh container guard; nonce-correlated Docker cp transport"}


def transport_resource_metadata(log: Path) -> dict[str, Any]:
    observed = json.loads(log.with_suffix(log.suffix + ".transport.json").read_text())
    return resource_metadata(observed["resource_readback"])


def docker_run(image: str, mounts: dict[Path, str], command: list[str], log: Path, timeout: int,
               *, network: bool = False, env_names: tuple[str, ...] = (), staged: bool = False) -> int:
    if staged:
        return docker_run_staged(image, mounts, command, log, timeout, network=network, env_names=env_names)
    container_name = "trievo-tau2-qual-" + os.urandom(6).hex()
    args = ["docker", "run", "--rm", "--name", container_name, "--user", "0:0",
            "--memory", "4g", "--cpus", "2", "--pids-limit", "256"]
    if not network:
        args += ["--network", "none"]
    for name in env_names:
        args += ["-e", name]  # value comes from this process's environment
    for host, container in mounts.items():
        args += ["-v", f"{host}:{container}"]
    args += [image, *command]
    with log.open("w", encoding="utf-8") as handle:
        try:
            run = subprocess.run(args, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            return 124
    return run.returncode



DAEMON_RUNNER = r'''
import json, os, socket, subprocess, sys
from pathlib import Path
from resource_guard import read_container_resources, require_container_resources, ContainerResourceError
spec = json.loads(Path("/tau2-transport/spec.json").read_text())
resource_readback = read_container_resources()
try:
    resource_readback = require_container_resources(resource_readback, enforcement_mode=spec["resource_enforcement_mode"])
except ContainerResourceError:
    code = 125
else:
    try:
        result = subprocess.run(spec["command"], timeout=spec["timeout"])
        code = result.returncode
    except subprocess.TimeoutExpired:
        code = 124
for mount in spec["mounts"]:
    Path(mount, ".tau2-transport-nonce").write_text(spec["nonce"])
Path("/tau2-transport/result.json").write_text(json.dumps({
    "nonce": spec["nonce"], "returncode": code, "python": sys.version,
    "network_interfaces": socket.if_nameindex(), "resource_readback": resource_readback}))
sys.exit(code)
'''


def docker_run_staged(image: str, mounts: dict[Path, str], command: list[str], log: Path,
                      timeout: int, *, network: bool = False, env_names: tuple[str, ...] = ()) -> int:
    """Fresh daemon-owned filesystem, nonce-checked cp output; no binds or exec.

    Only environment variable names enter argv. Credentials remain in Docker's
    environment transport and must never be included in inspection output.
    """
    require_resource_capability(log.with_suffix(log.suffix + ".resources.json"))
    name = "trievo-tau2-staged-" + os.urandom(6).hex()
    nonce = os.urandom(24).hex()
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tau2-transport-", dir=log.parent) as temporary:
        local = Path(temporary)
        payload = local / "payload"
        payload.mkdir()
        write_json(payload / "spec.json", {"nonce": nonce, "command": command, "timeout": timeout,
                                            "mounts": list(mounts.values()),
                                            "resource_enforcement_mode": resource_enforcement_mode()})
        (payload / "runner.py").write_text(DAEMON_RUNNER)
        shutil.copy2(REPO_ROOT / "trievo_bench/container_resources.py", payload / "resource_guard.py")
        args = ["docker", "create", "--name", name, "--user", "0:0", "--memory", "4g",
                "--cpus", "2", "--pids-limit", "256", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--entrypoint", "python"]
        if not network:
            args += ["--network", "none"]
        for variable in env_names:
            args += ["-e", variable]
        args += [image, "/tau2-transport/runner.py"]
        with log.open("w", encoding="utf-8") as handle:
            def run(argv, *, limit=300):
                return subprocess.run(argv, stdout=handle, stderr=subprocess.STDOUT,
                                      timeout=limit, check=False)
            try:
                if run(args).returncode:
                    return 125
                if run(["docker", "cp", str(payload), f"{name}:/tau2-transport"]).returncode:
                    return 125
                for host, destination in mounts.items():
                    if not host.is_dir() or not destination.startswith("/"):
                        raise ValueError("staged Docker mounts require local directories and absolute destinations")
                    if run(["docker", "cp", str(host), f"{name}:{destination}"]).returncode:
                        return 125
                run(["docker", "start", "-a", name], limit=timeout + 15)
                run(["docker", "logs", name])
                record_file = local / "observed.json"
                if run(["docker", "cp", f"{name}:/tau2-transport/result.json", str(record_file)]).returncode:
                    return 125
                try:
                    observed = json.loads(record_file.read_text())
                except (OSError, ValueError):
                    return 125
                if observed.get("nonce") != nonce or not isinstance(observed.get("returncode"), int):
                    return 125
                inspection = subprocess.run(["docker", "inspect", name, "--format",
                    "{{.Image}} {{.HostConfig.NetworkMode}} {{.State.ExitCode}}"],
                    capture_output=True, text=True, timeout=30, check=False)
                fields = inspection.stdout.strip().split()
                expected_network = "default" if network else "none"
                if (inspection.returncode or len(fields) != 3 or fields[0] != image_digest(image)
                        or fields[1] not in ({"default", "bridge"} if network else {expected_network})
                        or fields[2] != str(observed["returncode"])):
                    return 125
                write_json(log.with_suffix(log.suffix + ".transport.json"), {
                    **observed, "resource_enforcement": observed.get("resource_readback", {}).get("resource_enforcement", "not_enforced"),
                    "image_digest": fields[0], "network_mode": fields[1],
                    "transport": "docker_create_cp_start_cp", "fresh_container": True})
                try:
                    require_container_resources(observed.get("resource_readback", {}))
                except ContainerResourceError:
                    return 125
                # Copy into empty local directories and verify a per-run marker;
                # neither a stale host result nor a no-op Docker cp can pass.
                for index, (host, destination) in enumerate(mounts.items()):
                    downloaded = local / f"mount-{index}"
                    if run(["docker", "cp", f"{name}:{destination}", str(downloaded)]).returncode:
                        return 125
                    marker = downloaded / ".tau2-transport-nonce"
                    if not marker.is_file() or marker.read_text() != nonce:
                        return 125
                    marker.unlink()
                    shutil.copytree(downloaded, host, dirs_exist_ok=True)
                return observed["returncode"]
            except subprocess.TimeoutExpired:
                return 124
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)

def require_resource_capability(evidence: Path) -> dict[str, Any]:
    """Reject incapable daemons before build/API work; retain the readback."""
    result = docker_resource_capability_preflight()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    write_json(evidence, result)
    if not result.get("allowed", result["passed"]):
        raise ContainerResourceError("tau2 resource preflight blocked: " + "; ".join(result["errors"]))
    return result


def image_digest(tag: str) -> str:
    out = subprocess.run(["docker", "image", "inspect", tag, "-f", "{{.Id}}"], capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# build-image
# ---------------------------------------------------------------------------

DATA_KEEP = {
    "airline": ["db.json", "policy.md"],
    "retail": ["db.json", "policy.md"],
    "telecom": ["db.toml", "user_db.toml", "main_policy.md", "main_policy_solo.md", "tech_support_manual.md",
                "tech_support_workflow.md", "tech_support_workflow_solo.md"],
    "banking_knowledge": ["db.json", "documents", "prompts"],
    "mock": ["db.json", "user_db.json", "policy.md", "policy_solo.md"],
}


def stage_session_package(context: Path, *, python312: bool) -> None:
    session_ignored = ["__pycache__", "*.pyc"]
    if python312:
        # Fixed-variant replacement tasks embed private gold actions. Official
        # mode never loads this file, so omit it before any image layer exists.
        session_ignored.append("benchmark_fixes.json")
    shutil.copytree(SESSION_PACKAGE, context / "tau2_session",
                    ignore=shutil.ignore_patterns(*session_ignored))


def stage_upstream_source(context: Path) -> None:
    """Public runtime code only, before any Docker COPY/layer can see it.

    The pinned source tree contains only .py/.md runtime assets outside the
    telecom task generator. Definition JSON lives in upstream data and is not
    copied; domain data is separately allowlisted by DATA_KEEP.
    """
    src = context / "tau2-src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(CHECKOUT / name, src / name)

    def public_runtime_only(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if name in {"__pycache__", "tasks", "tests", "fixtures"}
                or (Path(directory, name).is_file() and Path(name).suffix not in {".py", ".md"})]

    shutil.copytree(CHECKOUT / "src/tau2", src / "src/tau2", ignore=public_runtime_only)


def cmd_build_image(args: argparse.Namespace) -> None:
    dockerfile = PY312_DOCKERFILE if args.python312 else DOCKERFILE
    if args.python312 and any(part.name == "l2v5_pool" for part in (POOL_ROOT.resolve(), *POOL_ROOT.resolve().parents)):
        raise SystemExit("Python 3.12 builds require a new --pool-root; the released pool is immutable")
    if args.python312:
        require_resource_capability(STAGING_ROOT / "tau2-resource-capability.json")
    args.base = args.base or ("python:3.12-slim-bookworm" if args.python312 else "trievo-l2-v5:0.4")
    args.tag = args.tag or ("trievo-l2-v5-tau2:0.3-py312" if args.python312 else IMAGE_TAG)
    context = fresh_staging("tau2_image_ctx_")
    stage_upstream_source(context)
    for domain, names in DATA_KEEP.items():
        target = context / "tau2-data" / "tau2" / "domains" / domain
        target.mkdir(parents=True)
        for name in names:
            source = domain_dir(domain) / name
            if source.is_dir():
                shutil.copytree(source, target / name)
            else:
                shutil.copy2(source, target / name)
    # The user simulator reads its global guidelines from the data directory.
    shutil.copytree(CHECKOUT / "data" / "tau2" / "user_simulator", context / "tau2-data" / "tau2" / "user_simulator")
    stage_session_package(context, python312=args.python312)
    shutil.copy2(dockerfile, context / "Dockerfile")
    log = context.parent / f"{context.name}.build.log"
    cmd = ["docker", "build", "-t", args.tag, "--build-arg", f"BASE_IMAGE={args.base}",
           "--build-arg", f"PIP_INDEX_URL={args.pip_index}", "--build-arg", f"TAU2_REVISION={REVISION}",
           str(context)]
    print("building", args.tag, "from", args.base, "log", log, flush=True)
    with log.open("w", encoding="utf-8") as handle:
        rc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise SystemExit(f"docker build failed rc={rc}; see {log}")
    digest = image_digest(args.tag)
    resource_fields = {}
    if args.python312:
        probe_log = context.parent / f"{context.name}.resource-probe.log"
        probe_code = docker_run_staged(digest, {}, ["python", "-c", "pass"], probe_log, 60)
        if probe_code:
            raise SystemExit(f"resource probe failed rc={probe_code}; see {probe_log}")
        resource_fields = transport_resource_metadata(probe_log)
    session_context = context / "tau2_session"
    session_files = sorted(p for p in session_context.rglob("*") if p.is_file())
    session_digest = hashlib.sha256(b"".join(
        p.relative_to(session_context).as_posix().encode() + b"\0" + p.read_bytes() for p in session_files)).hexdigest()
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    write_json(POOL_ROOT / "IMAGE.json", {**resource_fields, "task_image": args.tag, "task_image_digest": digest,
                                          "base_image": args.base, "base_image_digest": image_digest(args.base),
                                          "tau2_revision": REVISION, "session_package_sha256": session_digest,
                                          "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                          "dockerfile": str(dockerfile.relative_to(REPO_ROOT)),
                                          "dockerfile_sha256": sha256_bytes(dockerfile.read_bytes()),
                                          "python_target": "3.12" if args.python312 else "3.11",
                                          "adaptations": ["Python 3.12 isolated venv; upstream requires-python enforced",
                                                           "Omit inactive fixed-variant gold task payload benchmark_fixes.json"]
                                          if args.python312 else []})
    shutil.rmtree(context)
    print("built", args.tag, digest)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def image_record() -> dict[str, Any]:
    path = POOL_ROOT / "IMAGE.json"
    if not path.is_file():
        raise SystemExit("run build-image first")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_build(args: argparse.Namespace) -> None:
    image = image_record()
    legacy = legacy_sources()
    selected = select_tasks(legacy)
    if args.only:
        wanted = set(args.only)
        selected = [row for row in selected if row["source_id"] in wanted]
    if args.limit:
        selected = selected[: args.limit]
    print(f"selected {len(selected)} tasks; legacy v4={len(legacy['v4'])} v5={len(legacy['v5'])}", flush=True)
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    scoring_text = SCORING_SOURCE.read_text(encoding="utf-8")
    verifier_text = VERIFIER_SOURCE.read_text(encoding="utf-8")
    build_rows = []
    for row in selected:
        target = POOL_ROOT / task_dir_name(row["source_id"])
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        build_rows.append(write_task(target, row, image, scoring_text, verifier_text))
    write_json(POOL_ROOT / "BUILD.json", {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tasks": build_rows,
                                          "tau2_revision": REVISION,
                                          "legacy_sources": {k: sorted(v) for k, v in legacy.items()},
                                          **({k: image[k] for k in RESOURCE_METADATA_KEYS} if image.get("python_target") == "3.12" else {})})
    print("wrote", len(build_rows), "pool tasks under", POOL_ROOT)


def write_task(target: Path, row: dict[str, Any], image: dict[str, Any], scoring_text: str,
               verifier_text: str) -> dict[str, Any]:
    domain, task = row["domain"], row["task"]
    criteria = task.get("evaluation_criteria") or {}
    basis = list(criteria.get("reward_basis") or [])
    capability_id, rationale = capability_for(domain, task)
    notes = [
        "Live tau2 format: the agent talks to the tau2 user simulator and calls the live domain tools through "
        "/workspace/.tau2/tau2.py during the Turn; the session service runs AReaL's tau2 harness (official "
        "orchestrator, non-solo, max_steps 200, DeepSeek deepseek-flash user simulator at temperature 0) with "
        "the agent under test as the policy.",
        "Revealed to the agent: the official LLMAgent system prompt with the domain policy, the tool schemas, and "
        "the conversation itself. The user scenario stays with the user simulator; evaluation_criteria, "
        "description and annotations are never revealed.",
        "Scoring: tau2.evaluator.evaluate_simulation(EvaluationType.ALL) on the finished simulation inside the "
        f"session (full reward basis {basis}, NL assertions judged by the user-simulator model); the verifier "
        "re-runs it offline on the sealed record with the recorded NL-judge result. pass = reward 1.0.",
    ]
    if domain == "banking_knowledge":
        notes.append("banking_knowledge uses the upstream retrieval variant full_kb and the native "
                     "read_log_allowlist; user tools are limited to task.user_tools (tau2.runner.build).")
    if row["in_v5"] or row["in_v4"]:
        notes.append("Replaces the earlier L2 Demo " + "/".join(v for v, flag in (("V4", row["in_v4"]), ("V5", row["in_v5"])) if flag)
                     + " carrier for this source (V4 hand-written telecom simulator; V5 offline trajectory replay).")
    pool_task = {
        "schema_version": "l2_v5_pool_task/1.0",
        "source_id": row["source_id"],
        "source": {"benchmark_id": BENCHMARK_ID, "benchmark_revision": REVISION,
                   "source_task_id": row["source_task_id"], "source_path": row["source_path"],
                   "source_hash": sha256_bytes(canonical(task))},
        "prompt": build_prompt(domain),
        "benchmark_native_labels": {
            "domain": domain, "task_id": task["id"], "description": task.get("description"),
            "reward_basis": basis, "splits": row["splits"],
            **({"required_documents": task.get("required_documents")} if task.get("required_documents") else {}),
        },
        "capability_hint": {"capability_id": capability_id, "domain_id": DOMAIN_LABELS[domain][0],
                            "task_type_ids": ["tool_workflow", "policy_application"], "rationale": rationale},
        "runtime": {"runtime_profile_id": RUNTIME_PROFILE, "requires_gpu": False,
                    "turn_timeout_seconds": TURN_TIMEOUT_SECONDS, "max_tool_calls": MAX_TOOL_CALLS,
                    "network": "disabled", "task_image": image["task_image"],
                    "task_image_digest": image["task_image_digest"],
                    "live_session": {"kind": "tau2_live_session", "service_module": "tau2_session.service",
                                     "credential_env": [CREDENTIAL_ENV], "network": "user_simulator_llm_only"}},
        "submission": {"mode": "tool_trajectory", "payload_kind": "tau2_live_session_record", "relative_path": None},
        "evaluation_profile": "general",
        "metric_profile": None,
        "qualification": {"image_digest": None, "known_correct": None, "known_broken": None, "admitted": False,
                          "notes": notes, "reward_basis_graded": basis, "reward_basis_excluded": []},
        "l2_flags": {"in_v4": row["in_v4"], "in_v5": row["in_v5"]},
    }
    if image.get("python_target") == "3.12":
        resource_fields = {k: image[k] for k in RESOURCE_METADATA_KEYS}
        pool_task.update(resource_fields)
        pool_task["runtime"].update(resource_fields)
        if resource_fields["resource_enforcement"] == "declared_not_enforced":
            pool_task["qualification"]["notes"].append(
                "User-approved declared_only waiver: CPU/memory/PID limits are requested but not enforced; actual readback retained")
        pool_task["runtime"]["environment_version"] = "l2_environment/1.0"
        pool_task["qualification"]["notes"].append(
            "r3 adaptation: Python 3.12 isolated venv, enforced upstream requires-python; "
            "official b7ea907, AReaL carrier and deepseek-flash model unchanged")
    (target / "workspace_seed").mkdir()
    (target / "evaluation_assets").mkdir()
    write_json(target / "evaluation_assets" / "tau2_task.json", task)
    write_json(target / "evaluation_assets" / "tau2_meta.json",
               {"source_id": row["source_id"], "domain": domain, "task_id": task["id"], "revision": REVISION,
                "live_config": LIVE_CONFIG})
    (target / "evaluation_assets" / "tau2_scoring.py").write_text(scoring_text, encoding="utf-8")
    (target / "evaluation_assets" / "source_verifier.py").write_text(verifier_text, encoding="utf-8")
    write_json(target / "pool_task.json", pool_task)
    return {"source_id": row["source_id"], "task_dir": target.name, "capability_id": capability_id,
            "in_v4": row["in_v4"], "in_v5": row["in_v5"],
            **({k: image[k] for k in RESOURCE_METADATA_KEYS} if image.get("python_target") == "3.12" else {})}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _mutation(domain: str, task: dict[str, Any]) -> dict[str, Any] | None:
    if domain not in BROKEN_MUTATIONS:
        return None
    tool, make_args = BROKEN_MUTATIONS[domain]
    db = json.loads((domain_dir(domain) / "db.json").read_text(encoding="utf-8"))
    return {"name": tool, "arguments": make_args(task, db)}


def _pool_dirs(only: list[str] | None) -> list[Path]:
    dirs = sorted(p for p in POOL_ROOT.iterdir() if (p / "pool_task.json").is_file())
    if only:
        wanted = set(only)
        dirs = [p for p in dirs if json.loads((p / "pool_task.json").read_text())["source_id"] in wanted]
    return dirs


def cmd_fixtures(args: argparse.Namespace) -> None:
    if not os.environ.get(CREDENTIAL_ENV):
        raise SystemExit(f"{CREDENTIAL_ENV} must be set in the environment (the NL-assertion judge needs it)")
    image = image_record()
    dirs = _pool_dirs(args.only)
    stage = fresh_staging("tau2_fixtures_")
    jobs = []
    for index, target in enumerate(dirs):
        meta = json.loads((target / "evaluation_assets" / "tau2_meta.json").read_text())
        task = json.loads((target / "evaluation_assets" / "tau2_task.json").read_text())
        job_dir = stage / f"{index:05d}"
        job_dir.mkdir()
        shutil.copy2(target / "evaluation_assets" / "tau2_task.json", job_dir / "task.json")
        jobs.append({"task": f"/work/{index:05d}/task.json", "domain": meta["domain"], "out": f"/work/{index:05d}/out",
                     "config": meta.get("live_config") or {}, "timeout": TURN_TIMEOUT_SECONDS,
                     "mutation": _mutation(meta["domain"], task)})
    write_json(stage / "jobs.json", jobs)
    log = stage.parent / f"{stage.name}.log"
    print(f"building fixtures for {len(jobs)} tasks; log {log}", flush=True)
    fixture_image = image["task_image_digest"] if image.get("python_target") == "3.12" else image["task_image"]
    rc = docker_run(fixture_image, {stage: "/work"},
                    ["/opt/tau2/venv/bin/python", "-m", "tau2_session.fixtures", "--jobs", "/work/jobs.json",
                     "--revision", REVISION, "--workers", str(args.workers)],
                    log, timeout=14400, network=True, env_names=(CREDENTIAL_ENV,),
                    staged=image.get("python_target") == "3.12")
    print("fixtures rc", rc, flush=True)
    for index, target in enumerate(dirs):
        out = stage / f"{index:05d}" / "out"
        fixtures = target / "fixtures"
        if fixtures.exists():
            shutil.rmtree(fixtures)
        summary = json.loads((out / "summary.json").read_text()) if (out / "summary.json").is_file() \
            else {"ok": False, "error": f"no fixture output (docker rc={rc}, log {log})"}
        if summary.get("ok"):
            for name in ("known_correct", "known_broken"):
                (fixtures / name).mkdir(parents=True)
                shutil.copy2(out / f"{name}.json", fixtures / name / "response.txt")
        pool_task = json.loads((target / "pool_task.json").read_text())
        if image.get("python_target") == "3.12":
            resource_fields = transport_resource_metadata(log)
            summary.update(resource_fields)
            pool_task.update(resource_fields)
            pool_task["runtime"].update(resource_fields)
        pool_task["qualification"]["fixture_build"] = summary
        if summary.get("ok"):
            pool_task["qualification"]["fixture_kinds"] = {
                "known_correct": "upstream_gold_actions_conversation",
                "known_broken": summary["known_broken"]["kind"]}
        write_json(target / "pool_task.json", pool_task)
    shutil.rmtree(stage)


# ---------------------------------------------------------------------------
# qualify
# ---------------------------------------------------------------------------

QUALIFY_RUNNER = r'''
import importlib.util, json, sys, traceback, socket
from pathlib import Path
turn = Path("/work/turn")
fixture = sys.argv[1]
try:
    spec = importlib.util.spec_from_file_location("source_verifier", turn / "evaluation_assets/source_verifier.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.verify((turn / "fixtures" / fixture / "response.txt").read_text(), turn)
except Exception:
    result = {"status": "error", "error": traceback.format_exc()[-3000:]}
Path("/work/result.json").write_text(json.dumps({"result": result, "python": sys.version,
    "network_interfaces": socket.if_nameindex()}, indent=2))
'''


def qualify_batch(dirs: list[Path], image: dict[str, Any], tag: str) -> dict[str, dict[str, Any]]:
    """Each fixture gets a fresh container with declared limits and actual readback."""
    def run_fixture(job: tuple[Path, str]) -> tuple[str, str, dict[str, Any]]:
        target, fixture = job
        stage = fresh_staging(f"tau2_qualify_{tag}_{fixture}_")
        try:
            turn = stage / "turn"
            shutil.copytree(target / "evaluation_assets", turn / "evaluation_assets")
            shutil.copytree(target / "fixtures" / fixture, turn / "fixtures" / fixture)
            (stage / "runner.py").write_text(QUALIFY_RUNNER, encoding="utf-8")
            log = stage.parent / f"{stage.name}.log"
            rc = docker_run(image["task_image_digest"], {stage: "/work"},
                            ["python", "/work/runner.py", fixture], log, timeout=1200, staged=image.get("python_target") == "3.12")
            path = stage / "result.json"
            payload = json.loads(path.read_text()) if path.is_file() else {}
            result = payload.get("result") or {"status": "error", "error": f"no result (docker rc={rc}, log {log})"}
            if rc != 0:
                result = {"status": "error", "error": f"docker rc={rc}; log {log}"}
            resource_fields = (transport_resource_metadata(log) if image.get("python_target") == "3.12"
                               and log.with_suffix(log.suffix + ".transport.json").is_file() else {})
            result["qualification_container"] = {"image_digest": image["task_image_digest"],
                "network": "none", "network_interfaces": payload.get("network_interfaces"),
                "cpus": 2, "memory": "4g", "python": payload.get("python"),
                "fresh_fixture_container": True, "returncode": rc, "log": str(log), **resource_fields}
            return target.name, fixture, result
        finally:
            shutil.rmtree(stage)
    results: dict[str, dict[str, Any]] = {target.name: {} for target in dirs}
    jobs = [(target, fixture) for target in dirs for fixture in ("known_correct", "known_broken")]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for name, fixture, result in pool.map(run_fixture, jobs):
            results[name][fixture] = result
    return results


def _status(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    details = result.get("details") or {}
    return {"status": result.get("status"), "score": result.get("score"), "raw_metric": result.get("raw_metric"),
            "error": result.get("error"), "reward_breakdown": details.get("reward_breakdown"),
            "termination_reason": details.get("termination_reason"),
            "container": result.get("qualification_container")}


def _valid_failure(result: dict[str, Any] | None) -> bool:
    """A broken fixture counts only if the evaluator ran and returned reward 0."""
    return bool(result) and result.get("status") == "fail" and result.get("raw_metric") == 0.0


def _gold_tool_errors(target: Path) -> list[str]:
    """Names of the tool calls that returned an error in the known_correct conversation."""
    path = target / "fixtures" / "known_correct" / "response.txt"
    if not path.is_file():
        return []
    messages = (json.loads(path.read_text()).get("simulation") or {}).get("messages") or []
    names = {call["id"]: call["name"] for m in messages for call in (m.get("tool_calls") or [])}
    return [names.get(m.get("id"), "?") for m in messages if m.get("role") == "tool" and m.get("error")]


def cmd_qualify(args: argparse.Namespace) -> None:
    image = image_record()
    dirs = _pool_dirs(None)
    ready = [p for p in dirs if (p / "fixtures" / "known_correct" / "response.txt").is_file()]
    print(f"qualifying {len(ready)} of {len(dirs)} tasks in {image['task_image']}", flush=True)
    results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ready), args.batch):
        results.update(qualify_batch(ready[start:start + args.batch], image, f"{start:05d}"))
        print(f"  batch {start}: done", flush=True)
    index_rows = []
    for target in dirs:
        pool_task = json.loads((target / "pool_task.json").read_text())
        res = results.get(target.name, {})
        correct, broken = res.get("known_correct"), res.get("known_broken")
        q = pool_task["qualification"]
        q["image_digest"] = image["task_image_digest"]
        q["known_correct"] = _status(correct)
        q["known_broken"] = _status(broken)
        admitted = bool(correct and correct.get("status") == "pass" and correct.get("score") == 1.0
                        and _valid_failure(broken))
        build = q.get("fixture_build") or {}
        gold_errors = _gold_tool_errors(target)
        q["known_correct_gold_tool_errors"] = gold_errors
        task = json.loads((target / "evaluation_assets" / "tau2_task.json").read_text())
        gold_writes = [w for w in write_actions(task) if w != "transfer_to_human_agents"]
        failed_writes = [name for name in gold_errors if name in gold_writes]
        degenerate = bool(gold_writes) and len(failed_writes) >= len(gold_writes)
        if admitted and degenerate:
            admitted = False
            reason = ("degenerate upstream task: every gold write fails in the upstream environment "
                      f"({failed_writes}), so the gold end state equals the initial state and any no-op passes")
            q["notes"].append(reason)
        elif admitted:
            reason = "red/green consistent" + (f"; gold conversation has tool errors {gold_errors}"
                                               if gold_errors else "")
        elif not build.get("ok", False):
            reason = "fixture build error: " + str(build.get("error", "missing")).strip().splitlines()[-1][:300]
        elif "error" in res:
            reason = "qualification error: " + res["error"].strip().splitlines()[-1][:300]
        elif not (correct and correct.get("status") == "pass"):
            reason = "known_correct (upstream gold actions) did not reach reward 1.0: " + json.dumps(
                _status(correct), default=str)[:600]
        else:
            reason = "known_broken did not fail with evaluator reward 0: " + json.dumps(_status(broken), default=str)[:600]
        if image.get("python_target") == "3.12":
            # Include rejected/missing-fixture rows using the actual image probe,
            # then retain both per-fixture resource snapshots in qualification.
            resource_fields = {k: image[k] for k in RESOURCE_METADATA_KEYS}
            pool_task.update(resource_fields)
            pool_task["runtime"].update(resource_fields)
            q.update(resource_fields)
        q["admitted"] = admitted
        q["admission_reason"] = reason
        write_json(target / "pool_task.json", pool_task)
        index_rows.append({"source_id": pool_task["source_id"], "task_dir": target.name, "admitted": admitted,
                           "capability_id": pool_task["capability_hint"]["capability_id"], "reason": reason,
                           "in_v4": pool_task["l2_flags"]["in_v4"], "in_v5": pool_task["l2_flags"]["in_v5"],
                           **({k: pool_task[k] for k in RESOURCE_METADATA_KEYS} if image.get("python_target") == "3.12" else {})})
    with (POOL_ROOT / "INDEX.jsonl").open("w", encoding="utf-8") as handle:
        for row in index_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary: dict[str, dict[str, int]] = {}
    for row in index_rows:
        cell = summary.setdefault(row["capability_id"], {"built": 0, "admitted": 0})
        cell["built"] += 1
        cell["admitted"] += int(row["admitted"])
    write_json(POOL_ROOT / "QUALIFICATION.json", {"qualified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                   "image": image, "by_capability": summary,
                                                   "admitted": sum(r["admitted"] for r in index_rows),
                                                   "total": len(index_rows)})
    print(json.dumps(summary, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    """Write source/placement comparisons and W3 availability from actual records."""
    current = {json.loads((p / "pool_task.json").read_text())["source_id"]: p for p in _pool_dirs(None)}
    previous = {json.loads((p / "pool_task.json").read_text())["source_id"]: p
                for p in args.previous_pool.iterdir() if (p / "pool_task.json").is_file()}
    diffs, ledger = [], []
    records = {}
    for source_id, path in sorted(current.items()):
        record = json.loads((path / "pool_task.json").read_text())
        records[source_id] = record
        old = json.loads((previous[source_id] / "pool_task.json").read_text()) if source_id in previous else {}
        q, oq = record["qualification"], old.get("qualification", {})
        diffs.append({"source_id": source_id, "old_admitted": oq.get("admitted"),
            "new_admitted": q.get("admitted"), "old_image": old.get("runtime", {}).get("task_image_digest"),
            "new_image": record["runtime"]["task_image_digest"],
            "old_outcomes": {k: oq.get(k) for k in ("known_correct", "known_broken")},
            "new_outcomes": {k: q.get(k) for k in ("known_correct", "known_broken")},
            "reason": q.get("admission_reason")})
        ledger.append({"source_id": source_id, "source_hash": record["source"]["source_hash"],
            "adapted": True, "adaptations": ["Python 3.12 isolated venv replaces Python 3.11 system-site venv",
                "Inactive benchmark_fixes.json omitted before image build because it embeds private gold tasks",
                "Agent image retains public domain assets only; private task/evaluator stays outside Workspace",
                "Live DeepSeek NL judgment sealed for subsequent offline native rescoring"] +
                (["User-approved declared_only resource waiver; declared limits are not enforced; actual readback retained"]
                 if record.get("resource_enforcement") == "declared_not_enforced" else []),
            "preserved": {"revision": REVISION, "variant": "official", "user_model": "deepseek-flash",
                "judge_model": "deepseek-flash", "carrier": "AReaL 979f4221"},
            "qualification": q.get("admitted"),
            **{k: record[k] for k in RESOURCE_METADATA_KEYS if k in record}})
    placements, used = [], set()
    for task_path in sorted(args.r2_package.glob("streams/*/turn*/task.json")):
        task = json.loads(task_path.read_text())
        basis = task.get("source_basis") or {}
        source_id = f"{basis.get('benchmark_id')}:{basis.get('source_task_id')}"
        if source_id not in records:
            continue
        if task_path.parents[1].name.startswith("intra_"):
            used.add(source_id)
        diff = next(d for d in diffs if d["source_id"] == source_id)
        placements.append({"stream_id": task_path.parents[1].name, "turn_id": task_path.parent.name,
                           **diff, "environment_changed": diff["old_image"] != diff["new_image"]})
    counts = {}
    for source_id, record in records.items():
        if record["qualification"].get("admitted") and source_id not in used:
            capability = record["capability_hint"]["capability_id"]
            counts[capability] = counts.get(capability, 0) + 1
    for name, rows in (("ADAPTATIONS.jsonl", ledger), ("OUTCOME_DIFF.jsonl", diffs),
                       ("R2_PLACEMENT_DIFF.jsonl", placements)):
        (POOL_ROOT / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    write_json(POOL_ROOT / "UNUSED_CAPABILITY_COUNTS.json", {
        "exclusion_scope": "source IDs used by existing r2 intra_* Streams", "counts": counts})


def main() -> None:
    global POOL_ROOT, STAGING_ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool-root", type=Path, default=POOL_ROOT)
    parser.add_argument("--staging-root", type=Path,
                        default=Path(os.environ.get("TAU2_STAGING_ROOT", str(STAGING_ROOT))),
                        help="scratch directories and retained logs (or TAU2_STAGING_ROOT)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_image = sub.add_parser("build-image")
    p_image.add_argument("--base")
    p_image.add_argument("--python312", action="store_true", help="build the r3 isolated Python 3.12 carrier")
    p_image.add_argument("--tag")
    p_image.add_argument("--pip-index", default="https://pypi.tuna.tsinghua.edu.cn/simple")
    p_build = sub.add_parser("build")
    p_build.add_argument("--limit", type=int, default=0)
    p_build.add_argument("--only", nargs="*")
    p_fix = sub.add_parser("fixtures")
    p_fix.add_argument("--only", nargs="*")
    p_fix.add_argument("--workers", type=int, default=8)
    p_qualify = sub.add_parser("qualify")
    p_qualify.add_argument("--batch", type=int, default=400)
    p_report = sub.add_parser("report")
    p_report.add_argument("--previous-pool", type=Path, required=True)
    p_report.add_argument("--r2-package", type=Path, required=True)
    args = parser.parse_args()
    POOL_ROOT = args.pool_root
    STAGING_ROOT = args.staging_root
    {"build-image": cmd_build_image, "build": cmd_build, "fixtures": cmd_fixtures,
     "qualify": cmd_qualify, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
