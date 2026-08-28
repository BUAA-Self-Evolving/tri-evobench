from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from .base import RUNTIME_PROTOCOL_VERSION


class HermesBridge:
    """Tri-EvoBench protocol bridge backed by Hermes' ACP SessionManager."""

    def __init__(self, session_manager_factory: Callable[[], Any] | None = None) -> None:
        self._session_manager_factory = session_manager_factory
        self._manager: Any = None
        self._state: Any = None
        self._workspace: str | None = None
        self._turn_id: str | None = None
        self._turn_evolved = False
        self._policy = "turn_boundary"
        self._review_timeout = 900.0
        self._approval_policy = "session"
        self._last_usage: dict[str, float] = {}
        self._events: list[dict[str, Any]] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._turn_limits: dict[str, int] = {}
        self._limit_state: dict[str, Any] = {}
        self._l1_tools: list[str] = []
        self._is_l1_turn = False

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, method, None)
        if handler is None or method.startswith("_"):
            raise RuntimeError(f"unsupported bridge method: {method}")
        result = handler(params)
        if not isinstance(result, dict):
            raise RuntimeError(f"bridge method {method} returned a non-object")
        return result

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocol_version", ""))
        if requested != RUNTIME_PROTOCOL_VERSION:
            raise RuntimeError(
                f"unsupported runtime protocol {requested!r}; expected {RUNTIME_PROTOCOL_VERSION}"
            )
        evolution = params.get("evolution", {})
        options = params.get("options", {})
        if isinstance(evolution, dict):
            self._policy = str(evolution.get("policy", "turn_boundary"))
            self._review_timeout = float(evolution.get("timeout_seconds", 900))
        if isinstance(options, dict):
            self._approval_policy = str(options.get("approval_policy", "session"))
        if self._policy not in {"turn_boundary", "native", "disabled"}:
            raise RuntimeError(f"unsupported Hermes evolution policy: {self._policy}")
        if self._approval_policy not in {"once", "session", "always", "deny"}:
            raise RuntimeError(f"unsupported Hermes approval policy: {self._approval_policy}")
        return {
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "adapter": "hermes-session-manager",
            "capabilities": ["direct_workspace", "events", "usage", "memory", "skills", "evolution_barrier"],
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(str(params.get("workspace", ""))).resolve()
        if not workspace.is_dir():
            raise RuntimeError(f"Hermes workspace does not exist: {workspace}")
        hermes_home = Path(os.environ.get("HERMES_HOME", "")).resolve()
        if not str(os.environ.get("HERMES_HOME", "")).strip():
            raise RuntimeError("HERMES_HOME must be set for an isolated Hermes flow")
        hermes_home.mkdir(parents=True, exist_ok=True)
        if self._session_manager_factory is None:
            from acp_adapter.session import SessionManager

            self._manager = SessionManager()
        else:
            self._manager = self._session_manager_factory()
        self._workspace = str(workspace)
        return {
            "framework": "hermes-agent",
            "workspace": self._workspace,
            "evolution_policy": self._policy,
        }

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._manager is None or self._workspace is None:
            raise RuntimeError("Hermes flow has not started")
        if self._state is not None:
            if not self._turn_evolved:
                raise RuntimeError(f"Hermes turn {self._turn_id} has not crossed the evolution barrier")
            self._close_current_session()
        state = self._manager.create_session(cwd=self._workspace)
        agent = state.agent
        agent.skip_background_review = self._policy != "native"
        if self._policy == "turn_boundary":
            agent._memory_nudge_interval = 0
            agent._skill_nudge_interval = 0
        self._turn_limits = _parse_l2_agent_limits(params.get("metadata"))
        metadata = params.get("metadata")
        self._l1_tools = [str(item) for item in metadata.get("available_tools", [])] if isinstance(metadata, dict) and isinstance(metadata.get("available_tools"), list) else []
        self._is_l1_turn = _is_l1_metadata(metadata)
        if self._is_l1_turn:
            # L1 already controls retries through max_interactions.  Give the
            # framework exactly one foreground decision per Interaction: an
            # inner tool/reasoning loop would otherwise make one simulated
            # L1 retry consume several model calls before the evaluator can
            # supply its scripted follow-up or feedback.
            agent.max_iterations = 1
            # L1's calendar, messaging and file calls are evaluator-owned
            # simulated tools, serialised in the final response as protocol
            # lines and executed by Runner._apply_tool_calls.  Passing
            # Hermes' large native tool schema here both wastes context and
            # invites the model to mutate the real workspace instead of using
            # the auditable simulated interface.
            agent.valid_tool_names = set()
            # The DeepSeek V4 profile otherwise enables an expensive hidden
            # reasoning stream by default.  L1 needs a concise protocol reply,
            # and the evaluator itself supplies bounded retry feedback.
            agent.reasoning_config = {"enabled": False}
            # Hermes' normal interactive UX retries length-truncated output
            # and then asks for a summary.  Those are extra model calls that
            # bypass L1's max_interactions contract, so return the partial
            # result to the evaluator instead.
            agent.max_length_continue_retries = 0
        self._limit_state = {"tool_starts": 0, "reasons": []}
        if self._turn_limits:
            # Hermes reserves one no-tools call to request a concise final
            # answer after its iteration budget is depleted.  Leave that
            # grace call inside the benchmark's advertised model-call cap.
            if not self._is_l1_turn:
                agent.max_iterations = max(1, self._turn_limits["max_model_calls"] - 1)
            agent.run_budget_seconds = float(self._turn_limits["episode_timeout_seconds"])
            agent._trievo_evolution_limits = {
                "max_model_calls": self._turn_limits["evolution_max_model_calls"],
                "timeout_seconds": self._turn_limits["evolution_timeout_seconds"],
            }
        self._state = state
        self._turn_id = str(params.get("turn_id", ""))
        self._turn_evolved = False
        self._last_usage = {}
        return {"session_id": state.session_id, "fresh_session": True}

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_turn(str(params.get("turn_id", "")))
        request = str(params.get("request", ""))
        if not request:
            raise RuntimeError("Hermes interaction request is empty")
        self._events = []
        self._tool_calls = []
        if self._is_l1_turn:
            request = (
                _l1_protocol_prompt(self._l1_tools)
                + _l1_readonly_workspace_context(Path(self._workspace or state.cwd))
                + "\n\n"
                + request
            )
        agent = state.agent
        self._install_callbacks(agent)
        with self._foreground_policy(agent, state.session_id, state.cwd, self._turn_limits):
            result = agent.run_conversation(
                user_message=request,
                conversation_history=state.history,
                task_id=state.session_id,
                persist_user_message=request,
            )
        if not isinstance(result, dict):
            raise RuntimeError("Hermes returned a non-object conversation result")
        if isinstance(result.get("messages"), list):
            state.history = result["messages"]
            self._manager.save_session(state.session_id)
        usage = self._usage_delta(result)
        limit_reasons = list(self._limit_state.get("reasons", []))
        if self._turn_limits and int(result.get("api_calls", 0) or 0) >= self._turn_limits["max_model_calls"]:
            limit_reasons.append("max_model_calls")
        limit_reasons = list(dict.fromkeys(limit_reasons))
        if result.get("failed") and not result.get("final_response"):
            raise RuntimeError(str(result.get("error") or "Hermes conversation failed"))
        text, state_update, simulated_calls, protocol_events = _extract_l1_protocol(
            str(result.get("final_response", "")), self._l1_tools
        )
        return {
            "text": text,
            # L1 side effects are exclusively evaluator-owned protocol lines.
            # Ignore native Hermes callbacks, including malformed speculative
            # calls, so they cannot create duplicate simulated events.
            "events": protocol_events if self._is_l1_turn else self._events,
            "tool_calls": simulated_calls if self._is_l1_turn else self._tool_calls,
            "state_update": state_update,
            "tool_calls_are_executed": not bool(simulated_calls) if self._is_l1_turn else True,
            "usage": usage,
            "raw": {
                "completed": bool(result.get("completed", False)),
                "failed": bool(result.get("failed", False)),
                "turn_exit_reason": result.get("turn_exit_reason"),
                "model": result.get("model"),
                "provider": result.get("provider"),
                "work_limits": self._turn_limits,
                "limit_exhausted": bool(limit_reasons),
                "limit_reasons": limit_reasons,
            },
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._require_turn(str(params.get("turn_id", "")))
        if self._turn_evolved:
            raise RuntimeError(f"Hermes turn {self._turn_id} was already evolved")
        agent = state.agent
        before_usage = self._background_review_usage(agent)
        events: list[dict[str, Any]] = []

        if self._policy == "turn_boundary":
            evolution_timeout = float(
                self._turn_limits.get("evolution_timeout_seconds", self._review_timeout)
            )
            focus = self._build_review_focus(params)
            agent._spawn_background_review(
                messages_snapshot=list(state.history),
                review_memory=True,
                review_skills=True,
                focus=focus,
            )
            review_run = getattr(agent, "_background_review_run", None)
            if review_run is None:
                raise RuntimeError("Hermes did not create a background review run")
            if not review_run.request_done.wait(timeout=evolution_timeout):
                try:
                    review_run.cancel()
                except Exception:
                    pass
                review_run.request_done.wait(timeout=5)
                after_usage = self._background_review_usage(agent)
                self._turn_evolved = True
                return {
                    "completed": False,
                    "recoverable_failure": True,
                    "events": [{
                        "type": "evolution.limit_exhausted",
                        "reason": "evolution_timeout",
                        "policy": self._policy,
                        "timeout_seconds": evolution_timeout,
                        "review_trace": getattr(review_run, "outcome", {}),
                    }],
                    "usage": _subtract_usage(after_usage, before_usage),
                }
            review_trace = getattr(review_run, "outcome", {})
            max_calls = self._turn_limits.get("evolution_max_model_calls")
            review_calls = int(review_trace.get("api_calls", 0) or 0) if isinstance(review_trace, dict) else 0
            if (isinstance(review_trace, dict) and review_trace.get("error")) or (max_calls and review_calls >= max_calls):
                after_usage = self._background_review_usage(agent)
                self._turn_evolved = True
                reason = "evolution_error" if review_trace.get("error") else "evolution_max_model_calls"
                return {
                    "completed": False,
                    "recoverable_failure": True,
                    "events": [{
                        "type": "evolution.limit_exhausted",
                        "reason": reason,
                        "policy": self._policy,
                        "work_limits": {"max_model_calls": max_calls, "timeout_seconds": evolution_timeout},
                        "review_trace": review_trace,
                    }],
                    "usage": _subtract_usage(after_usage, before_usage),
                }
            events.append({"type": "evolution.completed", "policy": self._policy, "review_trace": review_trace})
        elif self._policy == "native":
            review_run = getattr(agent, "_background_review_run", None)
            if review_run is not None and not review_run.request_done.wait(timeout=self._review_timeout):
                raise RuntimeError(
                    f"Hermes native background review exceeded {self._review_timeout:.0f}s"
                )
            events.append({"type": "evolution.quiesced", "policy": self._policy})
        else:
            events.append({"type": "evolution.skipped", "policy": self._policy})

        after_usage = self._background_review_usage(agent)
        self._turn_evolved = True
        return {
            "completed": True,
            "events": events,
            "usage": _subtract_usage(after_usage, before_usage),
        }

    def end_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self._close_current_session()
        self._manager = None
        self._workspace = None
        return {"closed": True}

    def _require_turn(self, turn_id: str) -> Any:
        if self._state is None or self._turn_id is None:
            raise RuntimeError("Hermes turn has not started")
        if turn_id != self._turn_id:
            raise RuntimeError(f"inactive Hermes turn: {turn_id}")
        return self._state

    def _close_current_session(self) -> None:
        state = self._state
        if state is None:
            return
        agent = state.agent
        try:
            agent.shutdown_memory_provider()
        except Exception:
            pass
        try:
            agent.close()
        except Exception:
            pass
        if self._manager is not None:
            try:
                self._manager.remove_session(state.session_id)
            except Exception:
                pass
        self._state = None
        self._turn_id = None
        self._turn_evolved = False
        self._turn_limits = {}
        self._limit_state = {}
        self._l1_tools = []
        self._is_l1_turn = False

    def _install_callbacks(self, agent: Any) -> None:
        def tool_progress(event_type: str, name: str | None = None, preview: str | None = None, args: Any = None, **kwargs: Any) -> None:
            if event_type != "tool.started":
                return
            self._limit_state["tool_starts"] = int(self._limit_state.get("tool_starts", 0)) + 1
            arguments = args if isinstance(args, dict) else {"raw": args} if args is not None else {}
            self._events.append(
                {
                    "type": "tool.started",
                    "tool": str(name or "unknown"),
                    "arguments": _json_safe(arguments),
                    "preview": str(preview or ""),
                }
            )
            max_tools = self._turn_limits.get("max_tool_calls")
            if max_tools is not None and self._limit_state["tool_starts"] >= max_tools:
                self._record_limit("max_tool_calls")
                # The current call is the last permitted one.  Hermes checks
                # this flag before starting another model iteration, so no
                # subsequent tool batch is accepted.
                agent._interrupt_requested = True

        def step(api_call_count: int, previous_tools: Any = None) -> None:
            if not isinstance(previous_tools, list):
                return
            for item in previous_tools:
                normalized = _normalize_tool_result(item)
                self._tool_calls.append(normalized)
                self._events.append({"type": "tool.completed", **normalized})

        def reasoning(text: str) -> None:
            if text:
                self._events.append({"type": "reasoning", "text": str(text)})

        def event(name: str, payload: Any) -> None:
            self._events.append({"type": str(name), "payload": _json_safe(payload)})

        agent.tool_progress_callback = tool_progress
        agent.step_callback = step
        agent.reasoning_callback = reasoning
        agent.event_callback = event

    @contextlib.contextmanager
    def _foreground_policy(self, agent: Any, session_id: str, cwd: str, limits: dict[str, int]):
        from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist
        from tools import terminal_tool

        allowed = set(getattr(agent, "valid_tool_names", set()) or set())
        if self._policy == "turn_boundary":
            allowed.difference_update({"memory", "skill_manage", "session_search"})
        previous_approval = terminal_tool._get_approval_callback()
        previous_terminal_timeout = os.environ.get("TERMINAL_TIMEOUT")
        previous_terminal_handler = terminal_tool.terminal_tool

        def approval_callback(*args: Any, **kwargs: Any) -> str:
            return self._approval_policy

        def limited_terminal(*args: Any, **kwargs: Any) -> str:
            """Keep terminal work inside the same hard per-command budget."""

            terminal_limit = limits.get("terminal_timeout_seconds")
            if terminal_limit is None:
                return previous_terminal_handler(*args, **kwargs)
            if bool(kwargs.get("background", False)):
                self._record_limit("background_terminal_forbidden")
                return json.dumps(
                    {
                        "output": "",
                        "exit_code": 124,
                        "error": "L2 evaluation forbids background terminal jobs; finish within the foreground command budget.",
                        "status": "error",
                    },
                    ensure_ascii=False,
                )
            requested_timeout = kwargs.get("timeout")
            if requested_timeout is None:
                kwargs["timeout"] = terminal_limit
            else:
                try:
                    kwargs["timeout"] = min(int(requested_timeout), terminal_limit)
                except (TypeError, ValueError):
                    kwargs["timeout"] = terminal_limit
            result = previous_terminal_handler(*args, **kwargs)
            if '"exit_code": 124' in str(result) or "timed out after" in str(result):
                self._record_limit("terminal_timeout")
            return result

        session_tokens = None
        clear_session_vars = None
        try:
            try:
                from gateway.session_context import clear_session_vars as clear_vars
                from gateway.session_context import set_session_vars

                session_tokens = set_session_vars(
                    session_key=session_id,
                    session_id=session_id,
                    cwd=cwd,
                    cron_session="",
                )
                clear_session_vars = clear_vars
            except Exception:
                pass
            terminal_tool.set_approval_callback(approval_callback)
            if limits.get("terminal_timeout_seconds") is not None:
                os.environ["TERMINAL_TIMEOUT"] = str(limits["terminal_timeout_seconds"])
                terminal_tool.terminal_tool = limited_terminal
            if self._policy == "turn_boundary":
                set_thread_tool_whitelist(
                    allowed,
                    deny_msg_fmt=(
                        "Tri-EvoBench strict turn boundary denied persistent-state tool: {tool_name}. "
                        "Learning is only allowed after the Turn."
                    ),
                )
            yield
        finally:
            if self._policy == "turn_boundary":
                clear_thread_tool_whitelist()
            terminal_tool.terminal_tool = previous_terminal_handler
            if previous_terminal_timeout is None:
                os.environ.pop("TERMINAL_TIMEOUT", None)
            else:
                os.environ["TERMINAL_TIMEOUT"] = previous_terminal_timeout
            terminal_tool.set_approval_callback(previous_approval)
            if session_tokens is not None and clear_session_vars is not None:
                try:
                    clear_session_vars(session_tokens)
                except Exception:
                    pass

    def _record_limit(self, reason: str) -> None:
        reasons = self._limit_state.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)
            self._events.append({"type": "work_limit.reached", "reason": reason})

    def _usage_delta(self, result: dict[str, Any]) -> dict[str, Any]:
        current: dict[str, float] = {}
        mapping = {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cached_read_tokens": "cache_read_tokens",
            "cached_write_tokens": "cache_write_tokens",
            "reasoning_tokens": "reasoning_tokens",
            "total_tokens": "total_tokens",
            "estimated_cost_usd": "estimated_cost_usd",
        }
        delta: dict[str, Any] = {}
        for target, source in mapping.items():
            raw = result.get(source, 0)
            try:
                value = float(raw or 0)
            except (TypeError, ValueError):
                value = 0.0
            current[target] = value
            difference = max(0.0, value - self._last_usage.get(target, 0.0))
            delta[target] = difference if target == "estimated_cost_usd" else int(difference)
        try:
            calls = float(result.get("api_calls", 0) or 0)
        except (TypeError, ValueError):
            calls = 0.0
        current["api_calls"] = calls
        delta["api_calls"] = int(max(0.0, calls))
        self._last_usage = current
        return delta

    def _background_review_usage(self, agent: Any) -> dict[str, Any]:
        db = getattr(agent, "_session_db", None)
        session_id = getattr(agent, "session_id", None)
        if db is None or not session_id or not hasattr(db, "_read_ctx"):
            return {}
        try:
            db.flush_token_counts()
            with db._read_ctx() as connection:
                row = connection.execute(
                    """SELECT COALESCE(SUM(input_tokens), 0),
                              COALESCE(SUM(output_tokens), 0),
                              COALESCE(SUM(cache_read_tokens), 0),
                              COALESCE(SUM(cache_write_tokens), 0),
                              COALESCE(SUM(reasoning_tokens), 0),
                              COALESCE(SUM(api_call_count), 0),
                              COALESCE(SUM(estimated_cost_usd), 0)
                         FROM session_model_usage
                        WHERE session_id = ? AND task = 'background_review'""",
                    (session_id,),
                ).fetchone()
            values = list(row) if row is not None else [0] * 7
            return {
                "input_tokens": int(values[0] or 0),
                "output_tokens": int(values[1] or 0),
                "cached_read_tokens": int(values[2] or 0),
                "cached_write_tokens": int(values[3] or 0),
                "reasoning_tokens": int(values[4] or 0),
                "api_calls": int(values[5] or 0),
                "estimated_cost_usd": float(values[6] or 0),
                "total_tokens": sum(int(value or 0) for value in values[:4]),
            }
        except Exception:
            return {}

    @staticmethod
    def _build_review_focus(params: dict[str, Any]) -> str:
        trajectory = params.get("trajectory", [])
        feedback = params.get("feedback", [])
        return (
            "This is the single Tri-EvoBench Turn-boundary evolution pass. "
            "Use only the public task trajectory and public evaluator feedback below. "
            "Extract durable, class-level improvements; do not memorize hidden answers or one-off oracle facts.\n\n"
            "PUBLIC TRAJECTORY:\n"
            + json.dumps(_json_safe(trajectory), ensure_ascii=False)
            + "\n\nPUBLIC EVALUATOR FEEDBACK:\n"
            + json.dumps(_json_safe(feedback), ensure_ascii=False)
        )


def _l1_protocol_prompt(available_tools: list[str]) -> str:
    tools = ", ".join(available_tools)
    return (
        "L1 SIMULATED-TOOL PROTOCOL: these evaluator tools are available and must not be described as missing: "
        + tools
        + ". In your final response, after the user-facing text, emit one line per required call exactly as "
        "TRIEVO_TOOL_CALL: {\"tool\": \"name\", \"arguments\": {...}}. "
        "For durable facts emit exactly one TRIEVO_STATE_UPDATE: {\"facts\": {...}} line. "
        "When you ask a required clarification, emit a concrete event such as "
        "TRIEVO_EVENT: {\"event\": \"asked_for_calendar_id\"}; event names may contain only lowercase letters, digits, and underscores. "
        "The bridge removes these protocol lines before showing the user and the evaluator executes the calls. "
        "Do not attempt native Hermes tool calls: only the TRIEVO protocol lines enact L1 side effects. "
        "Keep visible replies within any requested sentence limit (otherwise at most three concise sentences), and never repeat a simulated call that succeeded earlier in the conversation. "
        "For calendar timestamps include the requested +08:00 offset, for example 2026-08-24T10:00:00+08:00. "
        "Benchmark task literals are authoritative: use an explicitly stated absolute date exactly; do not replace it using the host date. "
        "Use fact keys default_output_language=zh-CN, aurora.technical_owner, and default_delivery_calendar_id when applicable."
    )


def _is_l1_metadata(metadata: Any) -> bool:
    """Identify L1 explicitly, including read-only turns without any tools."""

    return isinstance(metadata, dict) and metadata.get("source_format") == "l1_demo_task_json"


def _l1_readonly_workspace_context(workspace: Path) -> str:
    """Give protocol-only L1 agents safe access to benchmark source material.

    L1 side effects are evaluator-owned, so native terminal/file tools stay
    disabled.  File analysis, editing, and translation tasks nevertheless need
    their source text; provide a bounded immutable snapshot rather than
    reopening native workspace tools.
    """

    allowed_roots = {"docs", "data"}
    excluded_names = {"__pycache__", ".git", "state", "iterations"}
    total_limit = 48_000
    per_file_limit = 16_000
    remaining = total_limit
    parts: list[str] = []
    try:
        candidates = sorted(workspace.rglob("*"))
    except OSError:
        return ""
    for path in candidates:
        if remaining <= 0 or not path.is_file():
            continue
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] not in allowed_roots:
            continue
        if any(part in excluded_names for part in relative.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        excerpt = content[: min(per_file_limit, remaining)]
        parts.append(f"FILE /workspace/{relative.as_posix()} (read-only):\n{excerpt}")
        remaining -= len(excerpt)
    if not parts:
        return ""
    return (
        "\n\nREAD-ONLY SANDBOX CONTEXT: the following files are authoritative "
        "source material. Do not claim you cannot read them. Do not use native "
        "tools or modify them directly; required side effects still use only the "
        "TRIEVO protocol lines.\n\n"
        + "\n\n".join(parts)
    )


def _extract_l1_protocol(
    text: str, available_tools: list[str]
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    visible: list[str] = []
    state_update: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("TRIEVO_STATE_UPDATE:"):
            try:
                value = json.loads(line.split(":", 1)[1].strip())
                if isinstance(value, dict):
                    state_update = value
            except json.JSONDecodeError:
                pass
            continue
        if line.startswith("TRIEVO_EVENT:"):
            try:
                value = json.loads(line.split(":", 1)[1].strip())
                if isinstance(value, dict) and isinstance(value.get("event"), str):
                    events.append(value)
            except json.JSONDecodeError:
                pass
            continue
        if line.startswith("TRIEVO_TOOL_CALL:"):
            try:
                value = json.loads(line.split(":", 1)[1].strip())
                if isinstance(value, dict) and str(value.get("tool")) in available_tools and isinstance(value.get("arguments"), dict):
                    calls.append({"tool": str(value["tool"]), "arguments": value["arguments"]})
            except json.JSONDecodeError:
                pass
            continue
        visible.append(line)
    facts = state_update.get("facts") if isinstance(state_update.get("facts"), dict) else None
    if facts is not None:
        aliases = {
            "default_work_language": ("default_output_language", "zh-CN"),
            "aurora_tech_lead": ("aurora.technical_owner", None),
            "delivery_calendar_id": ("default_delivery_calendar_id", None),
        }
        normalized = dict(facts)
        for source, (target, fixed_value) in aliases.items():
            if source in normalized and target not in normalized:
                normalized[target] = fixed_value if fixed_value is not None else normalized[source]
        state_update["facts"] = normalized
    return "\n".join(visible).strip(), state_update, calls, events


def _normalize_tool_result(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        name = item.get("name") or item.get("function_name") or item.get("tool") or "unknown"
        arguments = item.get("arguments", item.get("args", {}))
        result = item.get("result", item.get("output"))
        return {
            "tool": str(name),
            "arguments": _json_safe(arguments if isinstance(arguments, dict) else {"raw": arguments}),
            "result": _json_safe(result),
        }
    return {"tool": str(item), "arguments": {}, "result": None}


def _parse_l2_agent_limits(metadata: Any) -> dict[str, int]:
    """Validate the shared bounded-autonomy contract (with L2 legacy support)."""

    if not isinstance(metadata, dict):
        return {}
    raw = metadata.get("agent_limits", metadata.get("l2_agent_limits"))
    if not isinstance(raw, dict):
        return {}
    fields = {
        "max_model_calls": (2, 100),
        "max_tool_calls": (1, 100),
        "terminal_timeout_seconds": (1, 600),
        "episode_timeout_seconds": (30, 3600),
        "evolution_max_model_calls": (1, 16),
        "evolution_timeout_seconds": (10, 900),
    }
    result: dict[str, int] = {}
    for name, (minimum, maximum) in fields.items():
        value = raw.get(name)
        if isinstance(value, bool):
            raise RuntimeError(f"agent_limits.{name} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"agent_limits.{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise RuntimeError(
                f"agent_limits.{name} must be between {minimum} and {maximum}"
            )
        result[name] = parsed
    return result


def _subtract_usage(after: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in {
        "input_tokens",
        "output_tokens",
        "cached_read_tokens",
        "cached_write_tokens",
        "reasoning_tokens",
        "api_calls",
        "total_tokens",
        "estimated_cost_usd",
    }:
        try:
            value = max(0.0, float(after.get(key, 0) or 0) - float(before.get(key, 0) or 0))
        except (TypeError, ValueError):
            value = 0.0
        result[key] = value if key == "estimated_cost_usd" else int(value)
    return result


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


def main() -> None:
    protocol_stdout = sys.stdout
    bridge = HermesBridge()
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        request_id: Any = None
        method = ""
        try:
            request = json.loads(stripped)
            if not isinstance(request, dict):
                raise RuntimeError("protocol request must be an object")
            request_id = request.get("id")
            method = str(request.get("method", ""))
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RuntimeError("protocol params must be an object")
            with contextlib.redirect_stdout(sys.stderr):
                result = bridge.dispatch(method, params)
            response = {"id": request_id, "result": _json_safe(result)}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = {"id": request_id, "error": {"message": str(exc)}}
        protocol_stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        protocol_stdout.flush()
        if method == "end_flow":
            break


if __name__ == "__main__":
    main()
