"""Red/green fixtures for live τ² Turns, built in the τ² image.

``known_correct`` is the upstream gold solution played through the native
environment as a conversation: the orchestrator's greeting, a customer opening
message, every gold action as a tool call by its requestor (customer actions as
user tool calls) with the environment's own responses, one agent message that
states the task's ``communicate_info`` (and, for NL assertions of the form
"Agent should tell the user that X", the statement X), and the customer's ``###STOP###``. It is
scored by the official evaluator with the live NL-assertion judge, exactly as a
live session is, and stored as a session record.

``known_broken`` is the same opening followed by an agent refusal; on tasks
whose gold outcome leaves the database unchanged (refusals) it adds one
policy-unjustified write instead. A broken fixture counts only if the evaluator
ran and returned reward 0.

Usage (inside the image, network on, credential in the environment):
  python -m tau2_session.fixtures --jobs /work/jobs.json --revision <sha>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import traceback
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
from tau2.data_model.simulation import SimulationRun, TerminationReason
from tau2.data_model.tasks import Task
from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE
from tau2.registry import registry

from .areal.runner import EpisodeResult, Tau2EnvConfig, Tau2Runner
from .record import build_record
from .scoring import env_kwargs_for, evaluate_live

OPENING = "Hi, I need some help with my account."
REFUSAL = "I'm sorry, but I can't help with this request."


def _environment(domain: str, task: Task):
    env = registry.get_env_constructor(domain)(solo_mode=False, **env_kwargs_for(domain, task))
    state = task.initial_state
    history = deepcopy(state.message_history) if state and state.message_history else []
    env.set_state(
        initialization_data=state.initialization_data if state else None,
        initialization_actions=state.initialization_actions if state else None,
        message_history=history,
    )
    return env, history


def _opening(history: list) -> list:
    if history:
        return history
    return [deepcopy(DEFAULT_FIRST_AGENT_MESSAGE), UserMessage(role="user", content=OPENING)]


def _call(env, messages: list, index: int, name: str, arguments: dict, requestor: str) -> None:
    call = ToolCall(id=f"call_{index}", name=name, arguments=arguments, requestor=requestor)
    if requestor == "user":
        messages.append(UserMessage(role="user", content=None, tool_calls=[call]))
    else:
        messages.append(AssistantMessage(role="assistant", content=None, tool_calls=[call], raw_data={"l2_fixture": True}))
    messages.append(env.get_response(call))


_TELL = re.compile(r"^(?:the )?agent should (?:tell|inform|let know|confirm to) the user (?:that )?(.+)$", re.I)


def _told(assertion: str) -> str | None:
    """What an NL assertion says the agent must tell the user, as a statement."""
    match = _TELL.match(assertion.strip())
    if not match:
        return None
    text = match.group(1).strip().rstrip(".")
    return text[0].upper() + text[1:] + "."


def gold_messages(domain: str, task: Task) -> list:
    env, history = _environment(domain, task)
    messages = _opening(history)
    criteria = task.evaluation_criteria
    for index, action in enumerate((criteria.actions if criteria else None) or []):
        requestor = "user" if action.requestor == "user" else "assistant"
        _call(env, messages, index, action.name, action.arguments or {}, requestor)
    info = (criteria.communicate_info if criteria else None) or []
    summary = "Your request is complete." + (" To confirm: " + "; ".join(str(x) for x in info) + "." if info else "")
    statements = [_told(a) for a in ((criteria.nl_assertions if criteria else None) or [])]
    summary += "".join(" " + x for x in statements if x)
    messages.append(AssistantMessage(role="assistant", content=summary, raw_data={"l2_fixture": True}))
    messages.append(UserMessage(role="user", content="###STOP###"))
    return messages


def broken_messages(domain: str, task: Task, mutation: dict[str, Any] | None) -> list:
    env, history = _environment(domain, task)
    messages = _opening(history)
    if mutation is not None:
        _call(env, messages, 0, mutation["name"], mutation["arguments"], "assistant")
    messages.append(AssistantMessage(role="assistant", content=REFUSAL, raw_data={"l2_fixture": True}))
    messages.append(UserMessage(role="user", content="###STOP###"))
    return messages


def score(econfig: Tau2EnvConfig, task: Task, messages: list, runner: Tau2Runner) -> EpisodeResult:
    now = datetime.now().isoformat()
    simulation = SimulationRun(id=f"l2-fixture-{task.id}", task_id=task.id, start_time=now, end_time=now,
                               duration=0.0, termination_reason=TerminationReason.USER_STOP, messages=messages)
    runner._bind_nl_assertion_evaluator()
    reward_info, nl_reward_info = evaluate_live(simulation=simulation, task=task, domain=econfig.domain)
    return EpisodeResult(simulation=simulation, reward_info=reward_info, nl_reward_info=nl_reward_info,
                         termination_reason="user_stop", elapsed_seconds=0.0)


def run_job(job: dict[str, Any], revision: str) -> dict[str, Any]:
    out = Path(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    try:
        task_bytes = Path(job["task"]).read_bytes()
        task = Task.model_validate(json.loads(task_bytes))
        econfig = Tau2EnvConfig.from_mapping(job["domain"], job.get("config") or {})
        runner = Tau2Runner(econfig, agent_factory=None, user_api_key=os.environ.get(econfig.user_llm_api_key_env),
                            timeout=float(job.get("timeout", 1800)))
        common = dict(econfig=econfig, task_id=task.id, task_sha256=hashlib.sha256(task_bytes).hexdigest(),
                      tau2_revision=revision, timeout=float(job.get("timeout", 1800)), error=None,
                      partial_messages=[], abandoned=False)
        summary: dict[str, Any] = {}
        correct = score(econfig, task, gold_messages(job["domain"], task), runner)
        record = build_record(result=correct, extra={"fixture": "known_correct:upstream_gold_actions"}, **common)
        (out / "known_correct.json").write_text(json.dumps(record, ensure_ascii=False))
        summary["known_correct"] = {"reward": record["reward"], "breakdown": record["reward_info"].get("reward_breakdown")}
        broken = score(econfig, task, broken_messages(job["domain"], task, None), runner)
        kind = "agent_refusal"
        if broken.reward_info.reward == 1.0 and job.get("mutation"):
            broken = score(econfig, task, broken_messages(job["domain"], task, job["mutation"]), runner)
            kind = f"wrong_mutation:{job['mutation']['name']}"
        record = build_record(result=broken, extra={"fixture": f"known_broken:{kind}"}, **common)
        (out / "known_broken.json").write_text(json.dumps(record, ensure_ascii=False))
        summary["known_broken"] = {"kind": kind, "reward": record["reward"],
                                   "breakdown": record["reward_info"].get("reward_breakdown")}
        names = {
            c.id: c.name for m in correct.simulation.messages for c in (getattr(m, "tool_calls", None) or [])
        }
        gold_errors = [
            {"name": names.get(m.id), "content": str(m.content)[:300]}
            for m in correct.simulation.messages
            if getattr(m, "role", None) == "tool" and getattr(m, "error", False)
        ]
        summary["gold_tool_errors"] = gold_errors
        summary["ok"] = True
    except Exception:
        summary = {"ok": False, "error": traceback.format_exc()[-3000:]}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    jobs = json.loads(Path(args.jobs).read_text())
    # Processes, not threads: scoring patches the NL evaluator class per call.
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(partial(run_job, revision=args.revision), jobs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
