#!/usr/bin/env python3
"""Build and qualify tau2-bench tasks for the rebuilt L2 V5 task pool.

tau2-bench grades a conversation by replaying its tool calls in the upstream
environment and comparing the final DB state, environment assertions, actions
and communicated facts with the task's evaluation criteria. The L2 runtime has
no live tau2 tool server or user simulator, so each pool task is offline:

* workspace_seed: upstream policy, tool schemas generated from the upstream
  toolkits, gzip JSON snapshots of the initial DBs (after native task
  initialization), and the request information the user would give.
* submission: ``submission/trajectory.json`` with ordered tool calls and
  messages to the customer.
* evaluation_assets: the upstream task JSON (private), ``tau2_replay.py`` and
  ``source_verifier.py``, which execute the submitted calls in the upstream
  environment and grade them with ``tau2.evaluator.evaluate_simulation``.

Commands:
  build-image  assemble a filtered build context and build the tau2 task image
  build        select tasks, write pool directories, export workspace files
  qualify      run known_correct / known_broken through source_verifier in the
               image (network none) and record admission

Third-party checkouts are read-only. Docker jobs bind-mount freshly created
staging directories only (rootless Docker on /data_storage keeps stale inodes
for re-created paths); results are copied into the pool afterwards.
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
CHECKOUT = REPO_ROOT / "third_party" / "src" / "tau2-bench"
REVISION = "a2c024725189473d2d7cea3a5cfdbcc67478e41f"
BENCHMARK_ID = "tau2_bench"
POOL_ROOT = Path("/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_pool/tau2")
STAGING_ROOT = Path("/data_storage/yl_test/lgx/data-1/tmp/l2v5")
IMAGE_TAG = "trievo-l2-v5-tau2:0.1"
DOCKERFILE = REPO_ROOT / "sandbox" / "l2-v5" / "Dockerfile.tau2"
REPLAY_SOURCE = REPO_ROOT / "tools" / "l2_v5_tau2_replay.py"
VERIFIER_SOURCE = REPO_ROOT / "tools" / "l2_v5_tau2_source_verifier.py"
V4_ROOT = REPO_ROOT / "benchmarks" / "current" / "L2_Demo_V4"
# The September heterogeneous V5 candidate, read only for source continuity.
V5_ROOT = REPO_ROOT / "benchmarks" / "archive" / "L2_Demo_V5_heterogeneous_candidate"
DOMAINS = ("airline", "retail", "telecom", "banking_knowledge")
TRAJECTORY_PATH = "submission/trajectory.json"
RUNTIME_PROFILE = "tau2_offline_trajectory_v1"
EXTRA_BANKING = 25
EXTRA_TELECOM_PER_ISSUE = 20
SELECTION_SEED = 20261009
WORKERS = 24
# PHAI: the default DOCKER_HOST proxy is the endpoint that can bind-mount
# /data_storage paths (the direct rootless socket cannot). It drops attached
# stdout, so every result is read from bind-mounted files, never from stdout.

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

# Wrong-mutation broken fixtures for tasks where an empty trajectory already
# reproduces the gold end state (refusal tasks). One write call per domain that
# succeeds on the initial DB and is never part of the gold solution.
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


# ---------------------------------------------------------------------------
# Prompt and fixtures
# ---------------------------------------------------------------------------


def customer_block(domain: str, task: dict[str, Any]) -> str:
    if domain == "telecom":
        return ("Support ticket (verbatim from tau2-bench; in tau2-bench solo mode this ticket is the agent's only "
                "description of the customer's problem):\n\n" + task["ticket"].strip())
    instructions = (task.get("user_scenario") or {}).get("instructions")
    header = ("Customer request and the information the customer gives when asked (verbatim tau2-bench "
              "user-scenario text, addressed to the customer in the second person):\n\n")
    if isinstance(instructions, str):
        return header + instructions.strip()
    parts = []
    for key, label in (("reason_for_call", "Reason for call"), ("known_info", "Known info"),
                       ("unknown_info", "Unknown info (the customer does not know this)"),
                       ("task_instructions", "Customer instructions")):
        value = instructions.get(key)
        if value:
            parts.append(f"{label}:\n{str(value).strip()}")
    return header + "\n\n".join(parts)


def build_prompt(domain: str, task: dict[str, Any], meta: dict[str, Any]) -> str:
    _, label = DOMAIN_LABELS[domain]
    db_lines = []
    for name in meta["db_files"]:
        if name.endswith("agent_db.json.gz"):
            db_lines.append(f"- `{name}`: the agent-side database at the start of the conversation.")
        elif name.endswith("device_db.json.gz"):
            db_lines.append(f"- `{name}`: the customer's phone (device) state at the start; it is what the device "
                            "tools in `tools.json` would report.")
        else:
            db_lines.append(f"- `{name}`: the customer-side state at the start.")
    solo = meta["solo_mode"]
    customer_tools = meta["customer_tool_names"]
    lines = [
        f"You are {'an' if label[0] in 'aeiou' else 'a'} {label} agent in a tau2-bench scenario. This scenario is offline: there is no live tool "
        "server and no live customer. Plan the whole conversation from the files in this task directory and "
        "submit it as a trajectory.",
        "",
        "Files:",
        "- `policy.md`: the domain policy you must follow (upstream tau2-bench policy).",
        "- `tools.json`: JSON schemas of the tools. `agent_tools` are the tools you call."
        + (" In this domain you also operate the customer's device tools directly (tau2-bench solo mode)." if solo else "")
        + (" `customer_tools` are actions only the customer can perform on their side; include such a call with "
           "`\"actor\": \"customer\"` when the customer would perform it during the conversation." if customer_tools else ""),
        *db_lines,
        "  The snapshots are gzip-compressed JSON and read-only; they show the state your tool calls start from.",
        "",
        customer_block(domain, task),
        "",
        f"Submission: write `{TRAJECTORY_PATH}` as JSON `{{\"events\": [...]}}`, an ordered list where each event is "
        "either `{\"kind\": \"tool_call\", \"name\": \"<tool>\", \"arguments\": {...}}`"
        + (" (optionally with `\"actor\": \"customer\"`)" if customer_tools else "")
        + " or `{\"kind\": \"message\", \"content\": \"<text you say to the customer>\"}`. "
        "Grading executes the tool calls in order against the start state with the upstream tau2-bench "
        "environment and scores the resulting state and your messages with the upstream tau2-bench evaluator, as "
        "for a live conversation. Read-only calls are optional; write calls change the state exactly as they would "
        "live, including failed or unnecessary ones. At most 200 events; 10 tool errors end the conversation.",
    ]
    return "\n".join(lines)


def gold_trajectory(domain: str, task: dict[str, Any]) -> dict[str, Any]:
    events = []
    for action in (task.get("evaluation_criteria") or {}).get("actions") or []:
        actor = "customer" if action.get("requestor") == "user" and domain != "telecom" else "agent"
        events.append({"kind": "tool_call", "name": action["name"], "arguments": action.get("arguments") or {},
                       "actor": actor})
    info = (task.get("evaluation_criteria") or {}).get("communicate_info") or []
    if info:
        events.append({"kind": "message", "content": "To confirm: " + "; ".join(str(x) for x in info) + "."})
    return {"events": events}


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
# Docker helpers
# ---------------------------------------------------------------------------


def docker_env() -> dict[str, str]:
    return dict(os.environ)


def docker_run(image: str, mounts: dict[Path, str], command: list[str], log: Path, timeout: int) -> int:
    args = ["docker", "run", "--rm", "--network", "none", "--user", "0:0"]
    for host, container in mounts.items():
        args += ["-v", f"{host}:{container}"]
    args += [image, *command]
    with log.open("w", encoding="utf-8") as handle:
        run = subprocess.run(args, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, env=docker_env())
    return run.returncode


def image_digest(tag: str) -> str:
    out = subprocess.run(["docker", "image", "inspect", tag, "-f", "{{.Id}}"], capture_output=True, text=True, check=True,
                         env=docker_env())
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
    shutil.copy2(DOCKERFILE, context / "Dockerfile")
    log = context.parent / f"{context.name}.build.log"
    cmd = ["docker", "build", "-t", args.tag, "--build-arg", f"BASE_IMAGE={args.base}",
           "--build-arg", f"PIP_INDEX_URL={args.pip_index}", str(context)]
    print("building", args.tag, "from", args.base, "log", log, flush=True)
    with log.open("w", encoding="utf-8") as handle:
        rc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, env=docker_env()).returncode
    if rc != 0:
        raise SystemExit(f"docker build failed rc={rc}; see {log}")
    digest = image_digest(args.tag)
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    write_json(POOL_ROOT / "IMAGE.json", {"task_image": args.tag, "task_image_digest": digest,
                                          "base_image": args.base, "base_image_digest": image_digest(args.base),
                                          "tau2_revision": REVISION, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
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
    if args.limit:
        selected = selected[: args.limit]
    print(f"selected {len(selected)} tasks; legacy v4={len(legacy['v4'])} v5={len(legacy['v5'])}", flush=True)
    stage = fresh_staging("tau2_export_")
    jobs = []
    for index, row in enumerate(selected):
        job_dir = stage / f"{index:05d}"
        job_dir.mkdir()
        write_json(job_dir / "task.json", row["task"])
        jobs.append({"task": f"/work/{index:05d}/task.json", "domain": row["domain"],
                     "out": f"/work/{index:05d}/ws", "meta": f"/work/{index:05d}/meta.json"})
    write_json(stage / "jobs.json", jobs)
    shutil.copy2(REPLAY_SOURCE, stage / "tau2_replay.py")
    log = stage.parent / f"{stage.name}.log"
    rc = docker_run(image["task_image"], {stage: "/work"},
                    ["/opt/tau2/venv/bin/python", "/work/tau2_replay.py", "export-batch", "--jobs", "/work/jobs.json"],
                    log, timeout=7200)
    print("export rc", rc, "log", log, flush=True)
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    replay_text = REPLAY_SOURCE.read_text(encoding="utf-8")
    verifier_text = VERIFIER_SOURCE.read_text(encoding="utf-8")
    build_rows = []
    for index, row in enumerate(selected):
        job_dir = stage / f"{index:05d}"
        meta_path = job_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {"ok": False, "error": "no export output"}
        target = POOL_ROOT / task_dir_name(row["source_id"])
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        build_rows.append(write_task(target, row, meta, job_dir / "ws", image, replay_text, verifier_text))
    write_json(POOL_ROOT / "BUILD.json", {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tasks": build_rows,
                                          "legacy_sources": {k: sorted(v) for k, v in legacy.items()}})
    shutil.rmtree(stage)
    print("wrote", len(build_rows), "pool tasks under", POOL_ROOT)


def write_task(target: Path, row: dict[str, Any], meta: dict[str, Any], ws: Path, image: dict[str, Any],
               replay_text: str, verifier_text: str) -> dict[str, Any]:
    domain, task = row["domain"], row["task"]
    criteria = task.get("evaluation_criteria") or {}
    basis = list(criteria.get("reward_basis") or [])
    capability_id, rationale = capability_for(domain, task)
    notes = [
        "Offline tau2 format: no live tool server or user simulator. The agent sees the upstream policy, tool "
        "schemas generated from the upstream toolkits, gzip JSON snapshots of the initial DBs after native task "
        "initialization, and the request text below; it submits submission/trajectory.json.",
        "Revealed to the agent: telecom uses the upstream ticket (tau2 solo-mode input) plus the initial device "
        "state, which the device tools would report; airline/retail/banking use the upstream user_scenario "
        "instructions verbatim (reason for call, known/unknown info, customer instructions; persona omitted). "
        "Never revealed: evaluation_criteria (actions, env/NL assertions, communicate_info), description, annotations.",
        "Verifier executes the submitted calls in order in a fresh upstream environment (Environment.get_response, "
        "as the upstream orchestrator does), then grades with tau2.evaluator.evaluate_simulation(EvaluationType.ALL); "
        "max_steps=200 events and max_errors=10 follow tau2.config defaults.",
    ]
    graded = [b for b in basis if b != "NL_ASSERTION"]
    if "NL_ASSERTION" in basis:
        notes.append("NL_ASSERTION is in the upstream reward_basis but needs an LLM judge; it is excluded and the "
                     f"pass criterion is native reward 1.0 over {graded}.")
    if domain == "telecom":
        notes.append("telecom runs in upstream solo mode (main_policy_solo.md + tech_support_manual.md; the agent "
                     "calls device tools itself). Gold user-requestor actions are submitted as agent calls.")
    if domain == "banking_knowledge":
        notes.append("banking_knowledge uses the upstream retrieval variant full_kb (whole knowledge base inlined in "
                     "policy.md) and the native read_log_allowlist; customer-side tools are limited to task.user_tools "
                     "and submitted with actor=customer. Source file is the upstream loader's tasks/task_*.json.")
    if row["in_v5"] or row["in_v4"]:
        notes.append("Replaces the earlier L2 Demo " + "/".join(v for v, flag in (("V4", row["in_v4"]), ("V5", row["in_v5"])) if flag)
                     + " carrier for this source (V4 hand-written telecom simulator; V5 reason_for_call-only prompt "
                       "with exact tool-sequence matching).")
    pool_task = {
        "schema_version": "l2_v5_pool_task/1.0",
        "source_id": row["source_id"],
        "source": {"benchmark_id": BENCHMARK_ID, "benchmark_revision": REVISION,
                   "source_task_id": row["source_task_id"], "source_path": row["source_path"],
                   "source_hash": sha256_bytes(canonical(task))},
        "prompt": None,
        "benchmark_native_labels": {
            "domain": domain, "task_id": task["id"], "description": task.get("description"),
            "reward_basis": basis, "splits": row["splits"],
            **({"required_documents": task.get("required_documents")} if task.get("required_documents") else {}),
        },
        "capability_hint": {"capability_id": capability_id, "domain_id": DOMAIN_LABELS[domain][0],
                            "task_type_ids": ["tool_workflow", "policy_application"], "rationale": rationale},
        "runtime": {"runtime_profile_id": RUNTIME_PROFILE, "requires_gpu": False, "turn_timeout_seconds": 1800,
                    "network": "disabled", "task_image": image["task_image"],
                    "task_image_digest": image["task_image_digest"]},
        "submission": {"mode": "workspace_artifact", "payload_kind": "json_trajectory",
                       "relative_path": TRAJECTORY_PATH},
        "evaluation_profile": "general",
        "metric_profile": None,
        "qualification": {"image_digest": None, "known_correct": None, "known_broken": None, "admitted": False,
                          "notes": notes, "reward_basis_graded": graded,
                          "reward_basis_excluded": [b for b in basis if b not in graded]},
        "l2_flags": {"in_v4": row["in_v4"], "in_v5": row["in_v5"]},
    }
    status = {"source_id": row["source_id"], "task_dir": target.name, "capability_id": capability_id,
              "in_v4": row["in_v4"], "in_v5": row["in_v5"]}
    (target / "workspace_seed").mkdir()
    (target / "evaluation_assets").mkdir()
    (target / "fixtures" / "known_correct" / "submission").mkdir(parents=True)
    (target / "fixtures" / "known_broken" / "submission").mkdir(parents=True)
    if not meta.get("ok"):
        pool_task["qualification"]["notes"].append(f"export failed: {meta.get('error')}")
        write_json(target / "pool_task.json", pool_task)
        return {**status, "export_ok": False, "error": meta.get("error")}
    for item in ws.iterdir():
        shutil.move(str(item), str(target / "workspace_seed" / item.name))
    pool_task["prompt"] = build_prompt(domain, task, meta)
    write_json(target / "evaluation_assets" / "tau2_task.json", task)
    write_json(target / "evaluation_assets" / "tau2_meta.json",
               {"source_id": row["source_id"], "domain": domain, "task_id": task["id"], "revision": REVISION,
                "solo_mode": meta["solo_mode"], "trajectory_path": TRAJECTORY_PATH})
    (target / "evaluation_assets" / "tau2_replay.py").write_text(replay_text, encoding="utf-8")
    (target / "evaluation_assets" / "source_verifier.py").write_text(verifier_text, encoding="utf-8")
    gold = gold_trajectory(domain, task)
    write_json(target / "fixtures" / "known_correct" / TRAJECTORY_PATH, gold)
    broken: dict[str, Any] = {"events": []}
    broken_kind = "empty_trajectory"
    write_json(target / "fixtures" / "known_broken" / TRAJECTORY_PATH, broken)
    pool_task["qualification"]["fixture_kinds"] = {"known_correct": "upstream_gold_actions"
                                                   + ("+communicate_info_message" if criteria.get("communicate_info") else ""),
                                                   "known_broken": broken_kind}
    write_json(target / "pool_task.json", pool_task)
    return {**status, "export_ok": True}


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
            out[fixture] = mod.verify(turn / "fixtures" / fixture, turn)
    except Exception:
        out["error"] = traceback.format_exc()[-3000:]
    (turn / "result.json").write_text(json.dumps(out, indent=2))

# Each verify() runs the tau2 replay in its own subprocess, so threads give
# process-level parallelism without sharing tau2 state.
with ThreadPoolExecutor(max_workers=workers) as pool:
    list(pool.map(run, json.loads((root / "jobs.json").read_text())))
'''


def _airline_retail_db(domain: str) -> dict[str, Any]:
    return json.loads((domain_dir(domain) / "db.json").read_text(encoding="utf-8"))


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
    return {"status": result.get("status"), "score": result.get("score"), "raw_metric": result.get("raw_metric"),
            "reason": (result.get("details") or {}).get("reason"),
            "reward_breakdown": (result.get("details") or {}).get("reward_breakdown")}


def _valid_failure(result: dict[str, Any] | None) -> bool:
    """A broken fixture counts only if the evaluator ran and returned reward 0."""
    if not result or result.get("status") != "fail":
        return False
    details = result.get("details") or {}
    return details.get("reason") is None and result.get("raw_metric") == 0.0


def cmd_qualify(args: argparse.Namespace) -> None:
    image = image_record()
    dirs = sorted(p for p in POOL_ROOT.iterdir() if (p / "pool_task.json").is_file())
    exportable = [p for p in dirs if (p / "evaluation_assets" / "tau2_task.json").is_file()]
    print(f"qualifying {len(exportable)} of {len(dirs)} tasks in {image['task_image']}", flush=True)
    results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(exportable), args.batch):
        batch = exportable[start:start + args.batch]
        results.update(qualify_batch(batch, image, f"{start:05d}"))
        print(f"  batch {start}: done", flush=True)
    # Refusal tasks: an empty trajectory reproduces the gold end state. Swap in a
    # wrong-mutation broken fixture where the domain has one and re-run.
    retry = []
    for target in exportable:
        res = results[target.name]
        if res.get("known_correct", {}).get("status") == "pass" and res.get("known_broken", {}).get("status") == "pass":
            meta = json.loads((target / "evaluation_assets" / "tau2_meta.json").read_text())
            domain = meta["domain"]
            if domain in BROKEN_MUTATIONS:
                task = json.loads((target / "evaluation_assets" / "tau2_task.json").read_text())
                tool, make_args = BROKEN_MUTATIONS[domain]
                broken = {"events": [{"kind": "tool_call", "name": tool,
                                      "arguments": make_args(task, _airline_retail_db(domain)), "actor": "agent"}]}
                write_json(target / "fixtures" / "known_broken" / TRAJECTORY_PATH, broken)
                pool_task = json.loads((target / "pool_task.json").read_text())
                pool_task["qualification"]["fixture_kinds"]["known_broken"] = f"wrong_mutation:{tool}"
                pool_task["qualification"]["notes"].append(
                    "An empty trajectory reproduces the gold end state (refusal task), so known_broken is a single "
                    f"policy-unjustified write ({tool}).")
                write_json(target / "pool_task.json", pool_task)
                retry.append(target)
    if retry:
        print(f"re-qualifying {len(retry)} refusal tasks with wrong-mutation fixtures", flush=True)
        results.update(qualify_batch(retry, image, "retry"))
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
        gold_errors = [{"name": x.get("name"), "response": x.get("response")}
                       for x in (((correct or {}).get("details") or {}).get("replay") or {}).get("tool_log", [])
                       if x.get("error")]
        q["known_correct_gold_tool_errors"] = gold_errors
        task_path = target / "evaluation_assets" / "tau2_task.json"
        gold_writes = [w for w in write_actions(json.loads(task_path.read_text())) if w != "transfer_to_human_agents"] \
            if task_path.is_file() else []
        failed_writes = [e["name"] for e in gold_errors if e["name"] in gold_writes]
        degenerate = bool(gold_writes) and len(failed_writes) >= len(gold_writes)
        if admitted and degenerate:
            admitted = False
            reason = ("degenerate upstream task: every gold write fails in the upstream environment "
                      f"({failed_writes}), so the gold end state equals the initial state and any no-op passes")
            q["notes"].append(reason)
        q["admitted"] = admitted
        if admitted:
            reason = "red/green consistent" + (f"; gold replay has tool errors {[e['name'] for e in gold_errors]}"
                                               if gold_errors else "")
        elif degenerate and correct and correct.get("status") == "pass":
            pass
        elif "error" in res:
            reason = "qualification error: " + res["error"].strip().splitlines()[-1][:300]
        elif not (correct and correct.get("status") == "pass"):
            detail = (correct or {}).get("details") or {}
            reason = "known_correct (upstream gold actions) did not reach reward 1.0: " + json.dumps(
                {"reason": detail.get("reason"), "breakdown": detail.get("reward_breakdown"),
                 "error": str(detail.get("error", ""))[:200]})
        else:
            reason = "known_broken did not fail with evaluator reward 0: " + json.dumps(_status(broken))
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_image = sub.add_parser("build-image")
    p_image.add_argument("--base", default="trievo-l2-v5:0.4")
    p_image.add_argument("--tag", default=IMAGE_TAG)
    p_image.add_argument("--pip-index", default="https://pypi.tuna.tsinghua.edu.cn/simple")
    p_build = sub.add_parser("build")
    p_build.add_argument("--limit", type=int, default=0)
    p_qualify = sub.add_parser("qualify")
    p_qualify.add_argument("--batch", type=int, default=400)
    args = parser.parse_args()
    {"build-image": cmd_build_image, "build": cmd_build, "qualify": cmd_qualify}[args.command](args)


if __name__ == "__main__":
    main()
