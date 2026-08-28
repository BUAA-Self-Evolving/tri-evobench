from __future__ import annotations

import argparse
import json
import re
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
from .verifier import CheckResult, DeterministicVerifier, L1LifeJudge, LLMJudge, PythonVerifier


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
    state_before: dict[str, Any] = field(default_factory=dict)
    workspace_before: dict[str, Any] = field(default_factory=dict)


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
            state_before = self.runtime.snapshot_state().state
            workspace_before = self._workspace_evidence(sandbox) if turn.verifier.get("type") == "l1_life_judge" else agent_sandbox
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
                interaction_trace = [self._public_interaction(item) for item in interactions]
                interaction_trace.append({
                    "index": index,
                    "request": request,
                    "response": reply.text,
                    "actions": reply.actions,
                    "tool_calls": executed_calls,
                    "events": reply.events,
                    "state_update": reply.state_update,
                })
                trajectory = {
                    "events": all_events,
                    "tool_calls": all_tool_calls,
                    "tool_results": [item.get("result") for item in all_tool_calls if isinstance(item, dict) and "result" in item],
                    "state": state_snapshot.state,
                    "interactions": interaction_trace,
                    "state_before": state_before,
                    "state_after": state_snapshot.state,
                    "state_diff": self._state_diff(state_before, state_snapshot.state),
                    "workspace_before": workspace_before,
                    "workspace_after": self._workspace_evidence(sandbox) if turn.verifier.get("type") == "l1_life_judge" else sandbox.snapshot(),
                }
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
                state_before,
                workspace_before,
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
            "state_before": item.state_before,
            "workspace_before": item.workspace_before,
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
        if verifier_type == "l1_life_judge":
            if self.judge_mode not in {"llm", "hybrid"}:
                raise ValueError("this Turn requires an LLM verifier, but judge mode is not hybrid/llm")
            return L1LifeJudge(self.client).evaluate(verifier, response, sandbox, trajectory)
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
            elif tool == "file.write_text":
                path = self._l1_life_path(arguments.get("path"))
                overwrite = bool(arguments.get("overwrite", False))
                try:
                    sandbox.read_file(path)
                    exists = True
                except Exception:
                    exists = False
                if exists and not overwrite:
                    raise ValueError("file.write_text refuses to overwrite an existing file")
                content = arguments.get("content")
                if not isinstance(content, str):
                    raise ValueError("file.write_text requires string content")
                sandbox.write_file(path, content)
                result = {"path": str(arguments.get("path")), "status": "written", "bytes": len(content.encode("utf-8"))}
            elif tool == "file.append_text":
                path = self._l1_life_path(arguments.get("path"))
                content = arguments.get("content")
                if not isinstance(content, str):
                    raise ValueError("file.append_text requires string content")
                try:
                    prior = sandbox.read_file(path)
                except Exception as exc:
                    raise ValueError("file.append_text requires an existing file") from exc
                sandbox.write_file(path, prior + content)
                result = {"path": str(arguments.get("path")), "status": "appended", "bytes": len(content.encode("utf-8"))}
            elif tool == "file.edit_text":
                path = self._l1_life_path(arguments.get("path"))
                instruction = str(arguments.get("edit_instruction", ""))
                replacement = arguments.get("replacement")
                if not isinstance(replacement, str):
                    raise ValueError("file.edit_text requires string replacement")
                try:
                    prior = sandbox.read_file(path)
                except Exception as exc:
                    raise ValueError("file.edit_text requires an existing file") from exc
                if "first markdown heading" not in instruction.lower():
                    raise ValueError("unsupported localized edit instruction")
                updated, count = re.subn(r"(?m)^#{1,6}[^\r\n]*", replacement, prior, count=1)
                if count != 1:
                    raise ValueError("file.edit_text could not find the first Markdown heading")
                sandbox.write_file(path, updated)
                result = {"path": str(arguments.get("path")), "status": "edited", "edits": 1}
            elif tool == "file.rename":
                old_path = self._l1_life_path(arguments.get("old_path"))
                new_path = self._l1_life_path(arguments.get("new_path"))
                try:
                    content = sandbox.read_file(old_path)
                except Exception as exc:
                    raise ValueError("file.rename requires an existing old_path") from exc
                try:
                    sandbox.read_file(new_path)
                except Exception:
                    pass
                else:
                    raise ValueError("file.rename refuses to overwrite an existing new_path")
                sandbox.write_file(new_path, content)
                sandbox.remove(old_path)
                result = {"old_path": str(arguments.get("old_path")), "new_path": str(arguments.get("new_path")), "status": "renamed"}
            elif tool == "file.read_text":
                path = self._l1_life_path(arguments.get("path"))
                result = {"path": str(arguments.get("path")), "content": sandbox.read_file(path)}
            elif tool == "calendar.create_reminder":
                required = ("calendar_id", "title", "date")
                missing = [key for key in required if not str(arguments.get(key, ""))]
                if missing:
                    raise ValueError(f"calendar.create_reminder missing {', '.join(missing)}")
                reminders = self._tool_state_list(sandbox, "calendar_reminders")
                entry = {key: arguments[key] for key in required}
                entry["reminder_id"] = f"rem_{len(reminders) + 1:03d}"
                reminders.append(entry)
                self._write_tool_state_list(sandbox, "calendar_reminders", reminders)
                result = {"reminder_id": entry["reminder_id"], "status": "created"}
            elif tool == "message.prepare_draft":
                required = ("contact_id", "subject", "body_summary")
                missing = [key for key in required if not str(arguments.get(key, ""))]
                if missing:
                    raise ValueError(f"message.prepare_draft missing {', '.join(missing)}")
                drafts = self._tool_state_list(sandbox, "message_drafts")
                entry = {key: arguments[key] for key in required}
                entry["draft_id"] = f"draft_{len(drafts) + 1:03d}"
                drafts.append(entry)
                self._write_tool_state_list(sandbox, "message_drafts", drafts)
                result = {"draft_id": entry["draft_id"], "status": "prepared"}
            elif tool == "file.tag_artifact":
                required = ("tag", "source_task_id")
                missing = [key for key in required if not str(arguments.get(key, ""))]
                if missing:
                    raise ValueError(f"file.tag_artifact missing {', '.join(missing)}")
                tags = self._tool_state_list(sandbox, "artifact_tags")
                tags.append({key: arguments[key] for key in required})
                self._write_tool_state_list(sandbox, "artifact_tags", tags)
                result = {"status": "tagged"}
            else:
                raise ValueError(f"unsupported sandbox tool: {tool}")
            executed.append({"tool": tool, "arguments": arguments, "result": result})
        life_tools = {
            "file.write_text", "file.append_text", "file.edit_text", "file.rename", "file.read_text",
            "calendar.create_reminder", "message.prepare_draft", "file.tag_artifact",
        }
        # L1-Life records side effects in tool_state/current_state.json.  Do
        # not create an extra audit file, because its preservation checks treat
        # unrelated workspace writes as evidence of an intrusive implementation.
        if any(item.get("tool") not in life_tools for item in executed):
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
    def _l1_life_path(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("L1-Life file tool requires a non-empty relative path")
        normalized = value.replace("\\", "/").strip()
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("L1-Life file paths must remain inside the workspace")
        return "/workspace/" + normalized

    @staticmethod
    def _tool_state_list(sandbox: Sandbox, key: str) -> list[Any]:
        try:
            state = json.loads(sandbox.read_file("/workspace/tool_state/current_state.json"))
            value = state.get(key, []) if isinstance(state, dict) else []
            return list(value) if isinstance(value, list) else []
        except Exception:
            return []

    @staticmethod
    def _write_tool_state_list(sandbox: Sandbox, key: str, value: list[Any]) -> None:
        path = "/workspace/tool_state/current_state.json"
        try:
            state = json.loads(sandbox.read_file(path))
            state = state if isinstance(state, dict) else {}
        except Exception:
            state = {}
        state[key] = value
        sandbox.write_file(path, json.dumps(state, ensure_ascii=False, indent=2))

    @staticmethod
    def _state_diff(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
        if before == after:
            return []
        if isinstance(before, dict) and isinstance(after, dict):
            changes: list[dict[str, Any]] = []
            for key in sorted(set(before) | set(after)):
                name = f"{prefix}.{key}" if prefix else str(key)
                changes.extend(Runner._state_diff(before.get(key), after.get(key), name))
            return changes
        return [{"path": prefix or "$", "before": before, "after": after}]

    @staticmethod
    def _workspace_evidence(sandbox: Sandbox) -> dict[str, Any]:
        snapshot = sandbox.snapshot()
        files: list[dict[str, Any]] = []
        for item in snapshot.get("files", []):
            path = item.get("path") if isinstance(item, dict) else None
            if not isinstance(path, str):
                continue
            try:
                content = sandbox.read_file(path)
            except Exception:
                content = "(file unavailable)"
            files.append({**item, "content": content})
        return {**snapshot, "files": files}

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
