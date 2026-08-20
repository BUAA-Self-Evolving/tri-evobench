from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agent import OpenAICompatibleAgent, ScriptedAgent
from .learner import learner_from_mode
from .model import ModelReply, TokenBudget, client_from_config
from .sandbox import Sandbox, create_sandbox
from .spec import BenchmarkSpec, FlowSpec, TurnSpec, load_spec
from .user_sim import UserSimulator
from .verifier import CheckResult, DeterministicVerifier, LLMJudge, PythonVerifier


@dataclass
class InteractionRecord:
    index: int
    request: str
    response: str
    actions: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    events: list[dict[str, Any]]
    state_update: dict[str, Any]
    action_error: str | None
    score: float
    passed: bool
    feedback: str
    verifier_details: dict[str, Any]
    sandbox: dict[str, Any]
    state_keys: list[str]


@dataclass
class TurnRecord:
    turn_id: str
    score: float
    passed: bool
    interactions: list[InteractionRecord] = field(default_factory=list)


class Runner:
    def __init__(self, spec: BenchmarkSpec, *, output_dir: Path, backend: str | None = None, image: str | None = None, agent_mode: str | None = None, judge_mode: str | None = None, learner_mode: str = "frozen", max_total_tokens: int | None = None) -> None:
        self.spec = spec
        self.output_dir = output_dir
        self.backend = backend
        self.image = image
        self.agent_mode = agent_mode or str(spec.agent.get("type", "scripted"))
        self.judge_mode = judge_mode or str(spec.judge.get("type", "deterministic"))
        self.budget = TokenBudget(max_total_tokens or int(spec.benchmark.get("max_total_tokens", 3_000_000)))
        self.learner = learner_from_mode(learner_mode)
        self.state: dict[str, Any] = {}

        self.client = None
        if self.agent_mode == "openai_compatible" or self.judge_mode in {"llm", "hybrid"}:
            merged = dict(spec.agent if self.agent_mode == "openai_compatible" else spec.judge)
            self.client = client_from_config(merged, self.budget)
        if self.agent_mode == "openai_compatible":
            self.agent = OpenAICompatibleAgent(
                self.client,
                spec.agent.get("system_prompt"),
                str(spec.agent.get("response_format", "json")),
                int(spec.agent.get("max_output_tokens", 1024)),
            )
        elif self.agent_mode == "scripted":
            self.agent = ScriptedAgent(spec.agent.get("policy", {}))
        else:
            raise ValueError(f"unsupported agent type: {self.agent_mode}")
        self.program_verifier = DeterministicVerifier()
        self.python_verifier = PythonVerifier()
        if self.judge_mode not in {"deterministic", "llm", "hybrid"}:
            raise ValueError(f"unsupported judge type: {self.judge_mode}")
        # Program verifiers remain reusable and stateless. LLM judges are
        # instantiated inside _evaluate so each Interaction gets a fresh judge.
        self.judge = self.program_verifier if self.judge_mode in {"deterministic", "hybrid"} else None

    def run(self, flow: FlowSpec) -> dict[str, Any]:
        started = time.time()
        flow_dir = self.output_dir / flow.id
        flow_dir.mkdir(parents=True, exist_ok=True)
        sandbox = create_sandbox(flow.sandbox, self.backend, self.image)
        turn_records: list[TurnRecord] = []
        try:
            for turn in flow.turns:
                turn_records.append(self._run_turn(turn, sandbox))
        finally:
            sandbox.close()
        result = {
            "schema_version": "0.1",
            "flow_id": flow.id,
            "learner_mode": self.learner.__class__.__name__,
            "agent_mode": self.agent_mode,
            "judge_mode": self.judge_mode,
            "turns": [asdict(record) for record in turn_records],
            "metrics": self._metrics(turn_records),
            "token_budget": asdict(self.budget),
            "state": self.state,
            "wall_clock_seconds": round(time.time() - started, 3),
        }
        (flow_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with (flow_dir / "trace.jsonl").open("w", encoding="utf-8") as stream:
            for record in turn_records:
                for interaction in record.interactions:
                    stream.write(json.dumps({"flow_id": flow.id, "turn_id": record.turn_id, **asdict(interaction)}, ensure_ascii=False) + "\n")
        return result

    def _run_turn(self, turn: TurnSpec, sandbox: Sandbox) -> TurnRecord:
        sandbox.setup(turn.setup)
        self.state = self.learner.before_turn(self.state)
        user = UserSimulator(turn.user, turn.request)
        request = user.initial_request
        history: list[dict[str, Any]] = []
        interactions: list[InteractionRecord] = []
        all_events: list[dict[str, Any]] = []
        all_tool_calls: list[dict[str, Any]] = []
        for index in range(1, turn.max_interactions + 1):
            agent_sandbox = sandbox.snapshot()
            if self.spec.agent.get("include_workspace_content"):
                for item in agent_sandbox.get("files", []):
                    try:
                        item["content"] = sandbox.read_file(str(item["path"]))
                    except Exception:
                        item["content"] = "(file unavailable)"
            model_error = None
            try:
                reply = self.agent.respond(turn_id=turn.id, interaction_index=index, request=request, history=history, state=self.state, sandbox=agent_sandbox)
            except RuntimeError as exc:
                model_error = str(exc)
                reply = ModelReply(text="", actions=[], state_update={})
            action_error = None
            try:
                self._apply_actions(reply.actions, sandbox)
                executed_calls = self._apply_tool_calls(reply.tool_calls, sandbox)
            except (KeyError, TypeError, ValueError) as exc:
                action_error = str(exc)
                executed_calls = []
            if not action_error:
                self.state = self._merge_state(self.state, reply.state_update)
                all_events.extend(item for item in reply.events if isinstance(item, dict))
                all_tool_calls.extend(executed_calls)
            if model_error:
                check = CheckResult(0.0, False, f"Model request failed: {model_error}", {"type": "model_error", "error": model_error})
            elif action_error:
                check = CheckResult(0.0, False, f"Invalid agent action: {action_error}", {"type": "action_error", "error": action_error})
            else:
                trajectory = {"events": all_events, "tool_calls": all_tool_calls, "state": self.state}
                try:
                    check = self._evaluate(turn.verifier, reply.text, sandbox, trajectory)
                except RuntimeError as exc:
                    check = CheckResult(0.0, False, f"Verifier model request failed: {exc}", {"type": "verifier_model_error", "error": str(exc)})
            interaction = InteractionRecord(index, request, reply.text, reply.actions, executed_calls, reply.events, reply.state_update, action_error, check.score, check.passed, check.feedback, check.details, sandbox.snapshot(), sorted(self.state))
            interactions.append(interaction)
            history.extend([{"role": "user", "content": request}, {"role": "assistant", "content": reply.text}, {"role": "judge", "content": check.feedback}])
            if check.score >= turn.success_threshold:
                break
            request = user.next_request(history=history, feedback=check.feedback)
        trajectory = [asdict(item) for item in interactions]
        self.state = self.learner.after_turn(self.state, trajectory, turn)
        # The baseline defines a Turn score as the first successful Interaction's
        # score, or the final Interaction's score when the Turn is unsuccessful.
        # Do not select the historical maximum: that would hide later failures.
        final = interactions[-1]
        return TurnRecord(turn.id, final.score, final.passed, interactions)

    def _evaluate(self, verifier: dict[str, Any], response: str, sandbox: Sandbox, trajectory: Any = None) -> Any:
        verifier_type = str(verifier.get("type", ""))
        if verifier_type in {"llm", "llm_rubric"}:
            if self.judge_mode not in {"llm", "hybrid"}:
                raise ValueError("this Turn requires an LLM verifier, but judge mode is not hybrid/llm")
            # The baseline gives every Interaction an independent Judge
            # context. The client and token budget remain shared infrastructure,
            # but the evaluator itself carries no state across Interactions.
            judge = LLMJudge(
                self.client,
                str(self.spec.judge.get("rubric", "")),
                int(self.spec.judge.get("max_output_tokens", 2048)),
            )
            return judge.evaluate(verifier, response, sandbox, trajectory)
        if verifier_type == "python_verifier":
            return self.python_verifier.evaluate(verifier, response, sandbox, trajectory)
        return self.program_verifier.evaluate(verifier, response, sandbox)

    def _apply_actions(self, actions: list[dict[str, Any]], sandbox: Sandbox) -> None:
        for action in actions:
            op = action.get("op")
            if op == "write_file":
                path = action.get("path")
                if not isinstance(path, str) or not path:
                    raise ValueError("write_file action requires a non-empty path")
                sandbox.write_file(path, str(action.get("content", "")))
            elif op == "remove":
                path = action.get("path")
                if not isinstance(path, str) or not path:
                    raise ValueError("remove action requires a non-empty path")
                sandbox.remove(path)
            elif op in {None, ""}:
                continue
            else:
                raise ValueError(f"unsupported agent action: {op}")

    def _apply_tool_calls(self, tool_calls: list[dict[str, Any]], sandbox: Sandbox) -> list[dict[str, Any]]:
        """Execute the small, deterministic L1 tool surface and retain an auditable trace.

        The benchmark intentionally does not connect to users' real calendars or messaging
        systems.  Calls therefore act on sandbox-local fixtures with stable return values.
        """
        executed: list[dict[str, Any]] = []
        for raw_call in tool_calls:
            if not isinstance(raw_call, dict):
                raise ValueError("tool_calls items must be objects")
            tool = raw_call.get("tool") or raw_call.get("name")
            if not isinstance(tool, str) or not tool:
                raise ValueError("tool call requires a non-empty tool name")
            arguments = raw_call.get("arguments", raw_call.get("params", raw_call.get("parameters", {})))
            if not isinstance(arguments, dict):
                raise ValueError("tool call arguments must be an object")
            result: dict[str, Any] = {}
            if tool == "calendar.create_event":
                required = ("calendar_id", "title", "start", "end")
                missing = [key for key in required if not str(arguments.get(key, ""))]
                if missing:
                    raise ValueError(f"calendar.create_event missing {', '.join(missing)}")
                events = self._read_json_list(sandbox, "/workspace/state/calendar_events.json")
                result = {"event_id": f"evt_{len(events) + 1:03d}", "status": "created"}
                events.append({"tool": tool, "arguments": arguments, "result": result})
                sandbox.write_file("/workspace/state/calendar_events.json", json.dumps(events, ensure_ascii=False, indent=2))
            elif tool == "message.resolve_group":
                group_name = str(arguments.get("group_name", ""))
                if group_name == "Aurora 交付群":
                    result = {"group_id": "grp_aurora_delivery", "group_name": group_name}
                else:
                    result = {"group_id": None, "group_name": group_name}
            elif tool == "message.send_message":
                messages = self._read_json_list(sandbox, "/workspace/state/messages.json")
                result = {"message_id": f"msg_{len(messages) + 1:03d}", "status": "sent"}
                messages.append({"tool": tool, "arguments": arguments, "result": result})
                sandbox.write_file("/workspace/state/messages.json", json.dumps(messages, ensure_ascii=False, indent=2))
            elif tool == "file.create_file":
                filename = arguments.get("filename") or arguments.get("path")
                if not isinstance(filename, str) or not filename:
                    raise ValueError("file.create_file requires filename or path")
                result = {"path": filename, "status": "created"}
            else:
                raise ValueError(f"unsupported sandbox tool: {tool}")
            executed.append({"tool": tool, "arguments": arguments, "result": result})
        audit = self._read_json_list(sandbox, "/workspace/state/tool_calls.json")
        audit.extend(executed)
        sandbox.write_file("/workspace/state/tool_calls.json", json.dumps(audit, ensure_ascii=False, indent=2))
        return executed

    @staticmethod
    def _read_json_list(sandbox: Sandbox, path: str) -> list[Any]:
        try:
            value = json.loads(sandbox.read_file(path))
            return value if isinstance(value, list) else []
        except Exception:
            return []

    @staticmethod
    def _merge_state(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(update, dict):
            return dict(base)
        merged = dict(base)
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = Runner._merge_state(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _metrics(turns: list[TurnRecord]) -> dict[str, Any]:
        scores = [turn.score for turn in turns]
        interactions = [item for turn in turns for item in turn.interactions]
        return {
            "turn_count": len(turns),
            "turn_success_rate": sum(item.passed for item in turns) / len(turns) if turns else 0.0,
            "mean_turn_score": sum(scores) / len(scores) if scores else 0.0,
            "interaction_count": len(interactions),
            "mean_interactions_per_turn": len(interactions) / len(turns) if turns else 0.0,
            "first_interaction_success_rate": sum(item.interactions[0].passed for item in turns) / len(turns) if turns else 0.0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the specification-driven Tri-EvoBench baseline.")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", default="runs")
    parser.add_argument("--flow-id")
    parser.add_argument("--backend", choices=["local", "docker"])
    parser.add_argument("--image")
    parser.add_argument("--agent", choices=["scripted", "openai_compatible"])
    parser.add_argument("--judge", choices=["deterministic", "llm", "hybrid"])
    parser.add_argument("--learner", choices=["frozen", "raw-replay", "evolving", "oracle-update"], default="frozen")
    parser.add_argument("--max-total-tokens", type=int)
    args = parser.parse_args()
    spec = load_spec(args.spec)
    flows = [flow for flow in spec.flows if args.flow_id is None or flow.id == args.flow_id]
    if not flows:
        raise SystemExit(f"flow not found: {args.flow_id}")
    for flow in flows:
        runner = Runner(spec, output_dir=Path(args.output), backend=args.backend, image=args.image, agent_mode=args.agent, judge_mode=args.judge, learner_mode=args.learner, max_total_tokens=args.max_total_tokens)
        result = runner.run(flow)
        print(json.dumps({"flow_id": flow.id, "metrics": result["metrics"], "token_budget": result["token_budget"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
