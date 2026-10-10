#!/usr/bin/env python3
"""Replay an L2 tau2 trajectory against the upstream tau2 environment.

Runs under the tau2 interpreter of the task image (``/opt/tau2/venv``), not
the runtime interpreter. Two commands:

* ``export``: write the agent-visible workspace files for one task: the domain
  policy, the tool schemas generated from the upstream toolkits, and read-only
  snapshots of the initial databases after the task's native initialization.
* ``score``: execute the submitted tool calls one by one in a fresh upstream
  environment (as the upstream orchestrator does), build a SimulationRun from
  the resulting messages, and grade it with ``tau2.evaluator.evaluate_simulation``.

The NL_ASSERTION reward component needs an LLM judge and is excluded; every
other component in the task's reward_basis (DB, ENV_ASSERTION, ACTION,
COMMUNICATE) is computed by the upstream evaluator unchanged.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("LOGURU_LEVEL", "ERROR")

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="ERROR")

from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage  # noqa: E402
from tau2.data_model.simulation import SimulationRun, TerminationReason  # noqa: E402
from tau2.data_model.tasks import RewardType, Task  # noqa: E402
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation  # noqa: E402
from tau2.registry import registry  # noqa: E402

# Upstream defaults (tau2.config.DEFAULT_MAX_STEPS / DEFAULT_MAX_ERRORS).
MAX_STEPS = 200
MAX_ERRORS = 10
SOLO_DOMAINS = {"telecom"}
BANKING_VARIANT = "full_kb"
EXCLUDED_BASES = {RewardType.NL_ASSERTION}


def load_task(path: Path) -> Task:
    return Task.model_validate(json.loads(path.read_text(encoding="utf-8")))


def env_kwargs_for(domain: str, task: Task) -> dict[str, Any]:
    if domain == "banking_knowledge":
        from tau2.runner.build import _derive_read_log_allowlist

        return {
            "retrieval_variant": BANKING_VARIANT,
            "task": task,
            "read_log_allowlist": _derive_read_log_allowlist(task),
        }
    return {}


def make_env(domain: str, task: Task):
    solo = domain in SOLO_DOMAINS
    env = registry.get_env_constructor(domain)(solo_mode=solo, **env_kwargs_for(domain, task))
    return env, solo


def initial_history(task: Task) -> list:
    state = task.initial_state
    if state is None or state.message_history is None:
        return []
    return deepcopy(state.message_history)


def initialize(env, task: Task) -> list:
    state = task.initial_state
    history = initial_history(task)
    env.set_state(
        initialization_data=state.initialization_data if state else None,
        initialization_actions=state.initialization_actions if state else None,
        message_history=history,
    )
    return history


def customer_tool_names(env, task: Task) -> set[str]:
    if env.user_tools is None or env.solo_mode:
        return set()
    return {tool.name for tool in env.get_user_tools(include=task.user_tools)}


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _db_payload(toolkit) -> Any:
    db = getattr(toolkit, "db", None)
    if db is None:
        return None
    return db.model_dump(mode="json")


def export(task_path: Path, domain: str, out_dir: Path) -> dict[str, Any]:
    task = load_task(task_path)
    env, solo = make_env(domain, task)
    initialize(env, task)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "policy.md").write_text(env.get_policy(), encoding="utf-8")
    agent_tools = [tool.openai_schema for tool in env.get_tools()]
    device_tools: list = []
    customer_tools: list = []
    if solo and env.user_tools is not None:
        device_tools = [tool.openai_schema for tool in env.get_user_tools()]
    elif env.user_tools is not None:
        customer_tools = [tool.openai_schema for tool in env.get_user_tools(include=task.user_tools)]
    tools_doc = {
        "agent_tools": agent_tools + device_tools,
        "customer_tools": customer_tools,
    }
    (out_dir / "tools.json").write_text(json.dumps(tools_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    db_dir = out_dir / "db"
    db_dir.mkdir(exist_ok=True)
    files = []
    # Snapshots are gzip-compressed compact JSON: the airline/retail DBs are
    # 1-3 MB each and every pool task carries its own copy.
    def write_db(name: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with open(db_dir / name, "wb") as handle:
            handle.write(gzip.compress(data, compresslevel=9, mtime=0))
        files.append(f"db/{name}")

    agent_db = _db_payload(env.tools)
    if agent_db is not None:
        write_db("agent_db.json.gz", agent_db)
    if env.user_tools is not None:
        user_db = _db_payload(env.user_tools)
        if user_db is not None:
            write_db("device_db.json.gz" if solo else "customer_db.json.gz", user_db)
    return {
        "solo_mode": solo,
        "agent_tool_names": [t["function"]["name"] for t in tools_doc["agent_tools"]],
        "customer_tool_names": [t["function"]["name"] for t in tools_doc["customer_tools"]],
        "db_files": files,
    }


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


class SubmissionError(ValueError):
    pass


def parse_events(raw: Any) -> list[dict[str, Any]]:
    events = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(events, list):
        raise SubmissionError("trajectory must be an object with an 'events' list")
    parsed = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise SubmissionError(f"event {index} is not an object")
        kind = event.get("kind")
        if kind == "message":
            content = event.get("content")
            if not isinstance(content, str):
                raise SubmissionError(f"event {index}: message content must be a string")
            parsed.append({"kind": "message", "content": content})
        elif kind == "tool_call":
            name = event.get("name")
            arguments = event.get("arguments", {})
            actor = event.get("actor", "agent")
            if not isinstance(name, str) or not name:
                raise SubmissionError(f"event {index}: tool_call name must be a non-empty string")
            if not isinstance(arguments, dict):
                raise SubmissionError(f"event {index}: tool_call arguments must be an object")
            if actor not in {"agent", "customer"}:
                raise SubmissionError(f"event {index}: actor must be 'agent' or 'customer'")
            parsed.append({"kind": "tool_call", "name": name, "arguments": arguments, "actor": actor})
        else:
            raise SubmissionError(f"event {index}: kind must be 'tool_call' or 'message'")
    return parsed


def build_messages(env, task: Task, events: list[dict[str, Any]]) -> tuple[list, TerminationReason, dict[str, Any]]:
    messages = initialize(env, task)
    allowed_customer = customer_tool_names(env, task)
    termination = TerminationReason.AGENT_STOP
    errors = 0
    steps = 0
    tool_log = []
    for index, event in enumerate(events):
        if steps >= MAX_STEPS:
            termination = TerminationReason.MAX_STEPS
            break
        steps += 1
        if event["kind"] == "message":
            messages.append(AssistantMessage(role="assistant", content=event["content"]))
            continue
        requestor = "user" if event["actor"] == "customer" else "assistant"
        call = ToolCall(id=f"call_{index}", name=event["name"], arguments=event["arguments"], requestor=requestor)
        if requestor == "user":
            wrapper = UserMessage(role="user", content=None, tool_calls=[call])
        else:
            wrapper = AssistantMessage(role="assistant", content=None, tool_calls=[call])
        if requestor == "user" and event["name"] not in allowed_customer:
            from tau2.data_model.message import ToolMessage

            result = ToolMessage(id=call.id, role="tool", requestor="user", error=True,
                                 content=f"Error: customer cannot call tool '{event['name']}'")
        else:
            result = env.get_response(call)
        messages.extend([wrapper, result])
        tool_log.append({"index": index, "name": event["name"], "actor": event["actor"],
                         "error": bool(result.error), "response": str(result.content)[:300]})
        if result.error:
            errors += 1
            if errors >= MAX_ERRORS:
                termination = TerminationReason.TOO_MANY_ERRORS
                break
    return messages, termination, {"tool_errors": errors, "steps": steps, "tool_log": tool_log[:60]}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda o: o.model_dump(mode="json") if hasattr(o, "model_dump") else str(o)))


def score(task_path: Path, domain: str, trajectory_path: Path) -> dict[str, Any]:
    task = load_task(task_path)
    try:
        if not trajectory_path.is_file():
            raise SubmissionError(f"missing trajectory file: {trajectory_path.name}")
        try:
            raw = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SubmissionError(f"trajectory is not valid JSON: {exc}") from exc
        events = parse_events(raw)
    except SubmissionError as exc:
        return {"status": "fail", "score": 0.0, "raw_metric": None,
                "details": {"carrier": "tau2_native_replay/1.0", "reason": "invalid_submission", "error": str(exc)}}

    env, solo = make_env(domain, task)
    messages, termination, replay_info = build_messages(env, task, events)
    now = datetime.now().isoformat()
    simulation = SimulationRun(id="l2-replay", task_id=task.id, start_time=now, end_time=now, duration=0.0,
                               termination_reason=termination, messages=messages)
    graded_task = task.model_copy(deep=True)
    excluded = []
    if graded_task.evaluation_criteria is not None:
        basis = list(graded_task.evaluation_criteria.reward_basis)
        excluded = [b.value for b in basis if b in EXCLUDED_BASES]
        graded_task.evaluation_criteria.reward_basis = [b for b in basis if b not in EXCLUDED_BASES]
    reward_info = evaluate_simulation(
        simulation=simulation,
        task=graded_task,
        evaluation_type=EvaluationType.ALL,
        solo_mode=solo,
        domain=domain,
        env_kwargs=env_kwargs_for(domain, task),
        strict_replay=True,
    )
    reward = float(reward_info.reward)
    passed = abs(reward - 1.0) < 1e-9
    info = _jsonable(reward_info.model_dump(mode="json"))
    return {
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "raw_metric": reward,
        "details": {
            "carrier": "tau2_native_replay/1.0",
            "termination_reason": termination.value,
            "reward_basis_graded": [b.value for b in (graded_task.evaluation_criteria.reward_basis if graded_task.evaluation_criteria else [])],
            "reward_basis_excluded": excluded,
            "reward_breakdown": info.get("reward_breakdown"),
            "db_check": info.get("db_check"),
            "env_assertions": info.get("env_assertions"),
            "action_checks": info.get("action_checks"),
            "communicate_checks": info.get("communicate_checks"),
            "replay": replay_info,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_export = sub.add_parser("export")
    p_export.add_argument("--task", type=Path, required=True)
    p_export.add_argument("--domain", required=True)
    p_export.add_argument("--out", type=Path, required=True)
    p_export.add_argument("--meta", type=Path)
    p_score = sub.add_parser("score")
    p_score.add_argument("--task", type=Path, required=True)
    p_score.add_argument("--domain", required=True)
    p_score.add_argument("--trajectory", type=Path, required=True)
    p_score.add_argument("--result", type=Path, required=True)
    p_batch = sub.add_parser("export-batch", help="export many tasks: JSON list of {task, domain, out, meta}")
    p_batch.add_argument("--jobs", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        meta = export(args.task, args.domain, args.out)
        if args.meta:
            args.meta.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    elif args.command == "export-batch":
        for job in json.loads(args.jobs.read_text(encoding="utf-8")):
            try:
                meta = export(Path(job["task"]), job["domain"], Path(job["out"]))
                meta["ok"] = True
            except Exception as exc:  # recorded per task; the builder excludes failures
                meta = {"ok": False, "error": repr(exc)[:2000]}
            Path(job["meta"]).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    else:
        try:
            result = score(args.task, args.domain, args.trajectory)
        except Exception as exc:
            result = {"status": "fail", "score": 0.0, "raw_metric": None,
                      "details": {"carrier": "tau2_native_replay/1.0", "reason": "replay_error", "error": repr(exc)[:4000]}}
        args.result.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
