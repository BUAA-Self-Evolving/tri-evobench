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

Third-party checkouts are read-only. Docker jobs bind-mount freshly created
staging directories through the default DOCKER_HOST endpoint and read results
from files (that endpoint drops attached stdout).
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
from pathlib import Path
from typing import Any

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
SESSION_PACKAGE = REPO_ROOT / "trievo_bench" / "tau2_live" / "session"
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


def docker_run(image: str, mounts: dict[Path, str], command: list[str], log: Path, timeout: int,
               *, network: bool = False, env_names: tuple[str, ...] = ()) -> int:
    args = ["docker", "run", "--rm", "--user", "0:0"]
    if not network:
        args += ["--network", "none"]
    for name in env_names:
        args += ["-e", name]  # value comes from this process's environment
    for host, container in mounts.items():
        args += ["-v", f"{host}:{container}"]
    args += [image, *command]
    with log.open("w", encoding="utf-8") as handle:
        run = subprocess.run(args, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
    return run.returncode


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


def cmd_build_image(args: argparse.Namespace) -> None:
    context = fresh_staging("tau2_image_ctx_")
    src = context / "tau2-src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(CHECKOUT / name, src / name)
    shutil.copytree(CHECKOUT / "src" / "tau2", src / "src" / "tau2",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The telecom task generator composes tasks with their gold solutions; the
    # environment does not import it.
    shutil.rmtree(src / "src" / "tau2" / "domains" / "telecom" / "tasks")
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
    shutil.copytree(SESSION_PACKAGE, context / "tau2_session", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(DOCKERFILE, context / "Dockerfile")
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
    session_files = sorted(p for p in SESSION_PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    session_digest = hashlib.sha256(b"".join(
        p.relative_to(SESSION_PACKAGE).as_posix().encode() + b"\0" + p.read_bytes() for p in session_files)).hexdigest()
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    write_json(POOL_ROOT / "IMAGE.json", {"task_image": args.tag, "task_image_digest": digest,
                                          "base_image": args.base, "base_image_digest": image_digest(args.base),
                                          "tau2_revision": REVISION, "session_package_sha256": session_digest,
                                          "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                          "dockerfile": str(DOCKERFILE.relative_to(REPO_ROOT))})
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
                                          "legacy_sources": {k: sorted(v) for k, v in legacy.items()}})
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
            "in_v4": row["in_v4"], "in_v5": row["in_v5"]}


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
    rc = docker_run(image["task_image"], {stage: "/work"},
                    ["/opt/tau2/venv/bin/python", "-m", "tau2_session.fixtures", "--jobs", "/work/jobs.json",
                     "--revision", REVISION, "--workers", str(args.workers)],
                    log, timeout=14400, network=True, env_names=(CREDENTIAL_ENV,))
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
import importlib.util, json, sys, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
root = Path("/work")
workers = int(sys.argv[1]) if len(sys.argv) > 1 else 8

def run(job):
    turn = root / job
    out = {}
    try:
        spec = importlib.util.spec_from_file_location("sv_" + job, turn / "evaluation_assets" / "source_verifier.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        for fixture in ("known_correct", "known_broken"):
            try:
                text = (turn / "fixtures" / fixture / "response.txt").read_text()
                out[fixture] = mod.verify(text, turn)
            except Exception as exc:
                out[fixture] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    except Exception:
        out["error"] = traceback.format_exc()[-3000:]
    (turn / "result.json").write_text(json.dumps(out, indent=2))

# Each verify() re-scores in its own tau2 subprocess, so threads give
# process-level parallelism without sharing tau2 state.
with ThreadPoolExecutor(max_workers=workers) as pool:
    list(pool.map(run, json.loads((root / "jobs.json").read_text())))
'''


def qualify_batch(dirs: list[Path], image: dict[str, Any], tag: str) -> dict[str, dict[str, Any]]:
    stage = fresh_staging(f"tau2_qualify_{tag}_")
    jobs = []
    for index, target in enumerate(dirs):
        name = f"{index:05d}"
        turn = stage / name
        shutil.copytree(target / "evaluation_assets", turn / "evaluation_assets")
        shutil.copytree(target / "fixtures", turn / "fixtures")
        jobs.append(name)
    write_json(stage / "jobs.json", jobs)
    (stage / "runner.py").write_text(QUALIFY_RUNNER, encoding="utf-8")
    log = stage.parent / f"{stage.name}.log"
    rc = docker_run(image["task_image"], {stage: "/work"}, ["python", "/work/runner.py", str(WORKERS)], log, timeout=7200)
    results = {}
    for index, target in enumerate(dirs):
        path = stage / f"{index:05d}" / "result.json"
        results[target.name] = json.loads(path.read_text()) if path.is_file() else {"error": f"no result (docker rc={rc}, log {log})"}
    shutil.rmtree(stage)
    return results


def _status(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    details = result.get("details") or {}
    return {"status": result.get("status"), "score": result.get("score"), "raw_metric": result.get("raw_metric"),
            "error": result.get("error"), "reward_breakdown": details.get("reward_breakdown"),
            "termination_reason": details.get("termination_reason")}


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
        q["admitted"] = admitted
        q["admission_reason"] = reason
        write_json(target / "pool_task.json", pool_task)
        index_rows.append({"source_id": pool_task["source_id"], "task_dir": target.name, "admitted": admitted,
                           "capability_id": pool_task["capability_hint"]["capability_id"], "reason": reason,
                           "in_v4": pool_task["l2_flags"]["in_v4"], "in_v5": pool_task["l2_flags"]["in_v5"]})
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


def main() -> None:
    global POOL_ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool-root", type=Path, default=POOL_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    p_image = sub.add_parser("build-image")
    p_image.add_argument("--base", default="trievo-l2-v5:0.4")
    p_image.add_argument("--tag", default=IMAGE_TAG)
    p_image.add_argument("--pip-index", default="https://pypi.tuna.tsinghua.edu.cn/simple")
    p_build = sub.add_parser("build")
    p_build.add_argument("--limit", type=int, default=0)
    p_build.add_argument("--only", nargs="*")
    p_fix = sub.add_parser("fixtures")
    p_fix.add_argument("--only", nargs="*")
    p_fix.add_argument("--workers", type=int, default=8)
    p_qualify = sub.add_parser("qualify")
    p_qualify.add_argument("--batch", type=int, default=400)
    args = parser.parse_args()
    POOL_ROOT = args.pool_root
    {"build-image": cmd_build_image, "build": cmd_build, "fixtures": cmd_fixtures,
     "qualify": cmd_qualify}[args.command](args)


if __name__ == "__main__":
    main()
