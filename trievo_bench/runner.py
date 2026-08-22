from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .baselines import (
    BaselineInteractionError,
    BaselineReply,
    EvolutionContext,
    FlowContext,
    InteractionContext,
    TurnContext,
)
from .baselines.manifest import load_mapping
from .baselines.registry import create_baseline_runtime, resolve_baseline_selection
from .model import TokenBudget, client_from_config
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
    usage: dict[str, Any]


@dataclass
class TurnRecord:
    turn_id: str
    score: float
    passed: bool
    interactions: list[InteractionRecord] = field(default_factory=list)
    evolution: dict[str, Any] = field(default_factory=dict)


class Runner:
    def __init__(
        self,
        spec: BenchmarkSpec,
        *,
        output_dir: Path,
        backend: str | None = None,
        image: str | None = None,
        agent_mode: str | None = None,
        judge_mode: str | None = None,
        learner_mode: str = "frozen",
        max_total_tokens: int | None = None,
        baseline: str | None = None,
        baseline_manifest: str | None = None,
        baseline_config: dict[str, Any] | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.spec = spec
        self.output_dir = output_dir
        self.backend = backend
        self.image = image
        self.judge_mode = judge_mode or str(spec.judge.get("type", "deterministic"))
        self.learner_mode = learner_mode
        self.budget = TokenBudget(max_total_tokens or int(spec.benchmark.get("max_total_tokens", 3_000_000)))
        self.repo_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
        self.baseline_id, resolved_manifest, runtime_overrides = resolve_baseline_selection(
            spec,
            baseline_override=baseline,
            agent_override=agent_mode,
            config_override=baseline_config,
            manifest_override=baseline_manifest,
        )
        self.agent_mode = self.baseline_id
        self.runtime = create_baseline_runtime(
            self.baseline_id,
            spec=spec,
            budget=self.budget,
            learner_mode=learner_mode,
            repo_root=self.repo_root,
            manifest_path=resolved_manifest,
            overrides=runtime_overrides,
        )

        self.client = None
        if self.judge_mode in {"llm", "hybrid"}:
            merged = dict(spec.agent)
            merged.update(spec.judge)
            self.client = client_from_config(merged, self.budget)
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
        runtime_started = False
        try:
            self.runtime.start_flow(
                FlowContext(
                    flow_id=flow.id,
                    workspace=sandbox.workspace_path(),
                    output_dir=flow_dir,
                    metadata=self._public_metadata(flow.metadata),
                )
            )
            runtime_started = True
            for turn in flow.turns:
                turn_records.append(self._run_turn(turn, sandbox))
            final_snapshot = self.runtime.snapshot_state()
        finally:
            try:
                if runtime_started:
                    self.runtime.end_flow()
            finally:
                sandbox.close()
        runtime_metadata = self.runtime.metadata()
        result = {
            "schema_version": "0.1",
            "flow_id": flow.id,
            "baseline": self.baseline_id,
            "baseline_runtime": runtime_metadata,
            "learner_mode": runtime_metadata.get("learner", learner_mode_label(self.learner_mode)),
            "agent_mode": self.agent_mode,
            "judge_mode": self.judge_mode,
            "turns": [asdict(record) for record in turn_records],
            "metrics": self._metrics(turn_records),
            "token_budget": asdict(self.budget),
            "state": final_snapshot.state,
            "state_snapshot": final_snapshot.to_dict(),
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
        self.runtime.start_turn(
            TurnContext(
                turn_id=turn.id,
                request=turn.request,
                max_interactions=turn.max_interactions,
                metadata=self._public_metadata(turn.metadata),
            )
        )
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
                reply = self.runtime.interact(
                    InteractionContext(
                        turn_id=turn.id,
                        interaction_index=index,
                        request=request,
                        history=list(history),
                        sandbox=agent_sandbox,
                        remaining_token_budget=max(0, self.budget.limit - self.budget.used),
                    )
                )
            except BaselineInteractionError as exc:
                model_error = str(exc)
                reply = exc.reply
            except RuntimeError as exc:
                model_error = str(exc)
                reply = BaselineReply()
            action_error = None
            try:
                if not reply.actions_are_executed:
                    self._apply_actions(reply.actions, sandbox)
                executed_calls = (
                    reply.tool_calls
                    if reply.tool_calls_are_executed
                    else self._apply_tool_calls(reply.tool_calls, sandbox)
                )
            except (KeyError, TypeError, ValueError) as exc:
                action_error = str(exc)
                executed_calls = []
            if not action_error:
                all_events.extend(item for item in reply.events if isinstance(item, dict))
                all_tool_calls.extend(executed_calls)
            if model_error:
                check = CheckResult(0.0, False, f"Model request failed: {model_error}", {"type": "model_error", "error": model_error})
            elif action_error:
                check = CheckResult(0.0, False, f"Invalid agent action: {action_error}", {"type": "action_error", "error": action_error})
            else:
                state_snapshot = self.runtime.snapshot_state()
                trajectory = {"events": all_events, "tool_calls": all_tool_calls, "state": state_snapshot.state}
                try:
                    check = self._evaluate(turn.verifier, reply.text, sandbox, trajectory)
                except RuntimeError as exc:
                    check = CheckResult(0.0, False, f"Verifier model request failed: {exc}", {"type": "verifier_model_error", "error": str(exc)})
            state_snapshot = self.runtime.snapshot_state()
            state_keys = [str(item["path"]) for item in state_snapshot.files] or sorted(state_snapshot.state)
            interaction = InteractionRecord(
                index,
                request,
                reply.text,
                reply.actions,
                executed_calls,
                reply.events,
                reply.state_update,
                action_error,
                check.score,
                check.passed,
                check.feedback,
                check.details,
                sandbox.snapshot(),
                state_keys,
                reply.usage.to_dict(),
            )
            interactions.append(interaction)
            history.extend([{"role": "user", "content": request}, {"role": "assistant", "content": reply.text}, {"role": "judge", "content": check.feedback}])
            if check.score >= turn.success_threshold:
                break
            request = user.next_request(history=history, feedback=check.feedback)
        trajectory = [self._public_interaction(item) for item in interactions]
        feedback = [
            {
                "interaction_index": item.index,
                "score": item.score,
                "passed": item.passed,
                "feedback": item.feedback,
            }
            for item in interactions
        ]
        evolution = self.runtime.evolve(
            EvolutionContext(turn_id=turn.id, trajectory=trajectory, feedback=feedback)
        )
        # The baseline defines a Turn score as the first successful Interaction's
        # score, or the final Interaction's score when the Turn is unsuccessful.
        # Do not select the historical maximum: that would hide later failures.
        final = interactions[-1]
        return TurnRecord(turn.id, final.score, final.passed, interactions, evolution.to_dict())

    @staticmethod
    def _public_interaction(item: InteractionRecord) -> dict[str, Any]:
        return {
            "index": item.index,
            "request": item.request,
            "response": item.response,
            "actions": item.actions,
            "tool_calls": item.tool_calls,
            "events": item.events,
            "state_update": item.state_update,
            "action_error": item.action_error,
            "score": item.score,
            "passed": item.passed,
            "feedback": item.feedback,
            "sandbox": item.sandbox,
            "state_keys": item.state_keys,
            "usage": item.usage,
        }

    @staticmethod
    def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        public = metadata.get("public", {})
        return dict(public) if isinstance(public, dict) else {}

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
    parser.add_argument("--baseline", help="baseline id registered by a manifest or legacy adapter")
    parser.add_argument("--baseline-manifest", help="explicit baseline manifest path")
    parser.add_argument("--baseline-config", help="JSON/YAML manifest overrides")
    parser.add_argument("--agent", choices=["scripted", "openai_compatible"])
    parser.add_argument("--judge", choices=["deterministic", "llm", "hybrid"])
    parser.add_argument("--learner", choices=["frozen", "raw-replay", "evolving", "oracle-update"], default="frozen")
    parser.add_argument("--max-total-tokens", type=int)
    args = parser.parse_args()
    spec = load_spec(args.spec)
    baseline_config = load_mapping(args.baseline_config) if args.baseline_config else None
    flows = [flow for flow in spec.flows if args.flow_id is None or flow.id == args.flow_id]
    if not flows:
        raise SystemExit(f"flow not found: {args.flow_id}")
    for flow in flows:
        runner = Runner(
            spec,
            output_dir=Path(args.output),
            backend=args.backend,
            image=args.image,
            agent_mode=args.agent,
            judge_mode=args.judge,
            learner_mode=args.learner,
            max_total_tokens=args.max_total_tokens,
            baseline=args.baseline,
            baseline_manifest=args.baseline_manifest,
            baseline_config=baseline_config,
        )
        result = runner.run(flow)
        print(json.dumps({"flow_id": flow.id, "baseline": result["baseline"], "metrics": result["metrics"], "token_budget": result["token_budget"]}, ensure_ascii=False))


def learner_mode_label(mode: str) -> str:
    labels = {
        "reset": "FrozenLearner",
        "frozen": "FrozenLearner",
        "evolving": "EvolvingLearner",
        "raw-replay": "RawReplayLearner",
        "oracle-update": "OracleLearner",
    }
    return labels.get(mode, mode)


if __name__ == "__main__":
    main()
