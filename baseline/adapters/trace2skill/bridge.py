from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import traceback
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Callable


RUNTIME_PROTOCOL_VERSION = "1.0"


class _CountingClient:
    """Add per-phase call accounting to Trace2Skill's text-only clients."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.model = getattr(client, "model", "")
        self.calls = 0

    def chat(self, messages: Any, settings: Any = None, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._client.chat(messages, settings, *args, **kwargs)

    async def chat_async(self, messages: Any, settings: Any = None, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self._client.chat_async(messages, settings, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class Trace2SkillBridge:
    def __init__(
        self,
        *,
        client_factory: Callable[[dict[str, Any], str], Any] | None = None,
        agent_factory: Callable[[Any, dict[str, Any], Path, Path], Any] | None = None,
        evolver_factory: Callable[[Any, Path, dict[str, Any], Path], Any] | None = None,
    ) -> None:
        self.options: dict[str, Any] = {}
        self.evolution_policy = "turn_boundary"
        self.repo_root = Path(os.environ.get("TRIEVO_REPO_ROOT", Path.cwd())).resolve()
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.skills_dir: Path | None = None
        self.skill_dir: Path | None = None
        self.flow_id = ""
        self.turn_id = ""
        self.turn_metadata: dict[str, Any] = {}
        self._agent: Any = None
        self._clients: dict[str, _CountingClient] = {}
        self._initialized = False
        self._client_factory = client_factory
        self._agent_factory = agent_factory
        self._evolver_factory = evolver_factory

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, method, None)
        if handler is None or method.startswith("_"):
            raise RuntimeError(f"unsupported bridge method: {method}")
        result = handler(params)
        if not isinstance(result, dict):
            raise RuntimeError(f"bridge method {method} returned a non-object")
        return result

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("protocol_version", "")) != RUNTIME_PROTOCOL_VERSION:
            raise RuntimeError(
                f"unsupported runtime protocol {params.get('protocol_version')!r}; "
                f"expected {RUNTIME_PROTOCOL_VERSION}"
            )
        options = params.get("options", {})
        if not isinstance(options, dict):
            raise RuntimeError("Trace2Skill options must be an object")
        self.options = dict(options)
        evolution = params.get("evolution", {})
        if isinstance(evolution, dict):
            self.evolution_policy = str(evolution.get("policy", "turn_boundary"))
        if self.evolution_policy not in {"turn_boundary", "disabled"}:
            raise RuntimeError(
                f"unsupported Trace2Skill evolution policy: {self.evolution_policy}"
            )
        self.repo_root = Path(
            str(self.options.get("repo_root") or os.environ.get("TRIEVO_REPO_ROOT") or Path.cwd())
        ).resolve()
        self._initialized = True
        return {
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "adapter": "trace2skill",
            "capabilities": [
                "direct_workspace",
                "tool_calls",
                "usage",
                "skill_reflection",
                "evolution_barrier",
            ],
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            raise RuntimeError("Trace2Skill bridge has not been initialized")
        workspace = Path(str(params.get("workspace", ""))).resolve()
        state_dir = Path(str(params.get("state_dir", ""))).resolve()
        if not workspace.is_dir():
            raise RuntimeError(f"Trace2Skill workspace does not exist: {workspace}")
        state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.state_dir = state_dir
        self.flow_id = str(params.get("flow_id", ""))
        self.skills_dir = state_dir / "skills"
        self._seed_skills()
        self.skill_dir = self._select_skill_dir()
        return {
            "framework": "Trace2Skill",
            "agent": str(self.options.get("agent", "cli_skill_preloaded")),
            "workspace": str(workspace),
            "state_dir": str(state_dir),
            "skills_dir": str(self.skills_dir),
            "skill_dir": str(self.skill_dir) if self.skill_dir else None,
            "evolution_policy": self.evolution_policy,
        }

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.skills_dir is None:
            raise RuntimeError("Trace2Skill flow has not started")
        if self.turn_id:
            raise RuntimeError(f"Trace2Skill turn {self.turn_id} has not completed evolution")
        self.turn_id = str(params.get("turn_id", ""))
        if not self.turn_id:
            raise RuntimeError("Trace2Skill turn_id is empty")
        metadata = params.get("metadata", {})
        self.turn_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self._agent = None
        self._clients = {}
        return {
            "fresh_session": True,
            "skill_sha256": _sha256(self._skill_snapshot()),
        }

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_turn(str(params.get("turn_id", "")))
        request = str(params.get("request", "")).strip()
        if not request:
            raise RuntimeError("Trace2Skill interaction request is empty")
        if self.workspace is None or self.skills_dir is None:
            raise RuntimeError("Trace2Skill workspace is unavailable")

        # L1 uses a deterministic simulated-tool protocol (calendar/message
        # calls plus persistent facts), not Trace2Skill's spreadsheet ReAct
        # tool surface.  Route those turns through the same model client with
        # the benchmark JSON envelope so calls are returned to Runner for
        # auditable execution instead of being lost as plain text.
        if _looks_like_l1_protocol_task(request, self.turn_metadata):
            return self._interact_l1_protocol(request, params)

        agent = self._get_agent()
        target_calls_before = self._clients.get("target").calls if self._clients.get("target") else 0
        target_usage_before = dict(getattr(self._clients.get("target"), "usage_totals", {}))
        context = self._build_context(request, params)
        result = agent.run(context)
        steps = getattr(getattr(agent, "_agent", None), "_last_steps", [])
        trace = _steps_to_trace(steps)
        success = bool(result.get("success", False)) if isinstance(result, dict) else False
        answer = str(result.get("answer", "")) if isinstance(result, dict) else str(result or "")
        error = str(result.get("error", "")) if isinstance(result, dict) else ""
        if not answer and error:
            answer = error
        answer, carrier_adaptation = _adapt_final_response_carrier(
            answer,
            trace["tool_calls"],
            self.turn_metadata,
        )
        target_calls_after = self._clients.get("target").calls if self._clients.get("target") else 0
        usage = {
            **_usage_delta(self._clients.get("target"), target_usage_before),
            "api_calls": max(0, target_calls_after - target_calls_before),
        }
        return {
            "text": answer,
            "events": trace["events"] + ([{"type": "agent.failed", "error": error}] if not success and error else []),
            "tool_calls": trace["tool_calls"],
            "usage": usage,
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
            "raw": {
                "success": success,
                "turns": result.get("turns", 0) if isinstance(result, dict) else 0,
                "error": error,
                "steps": trace["steps"],
                "carrier_adaptation": carrier_adaptation,
            },
        }

    def _interact_l1_protocol(self, request: str, params: dict[str, Any]) -> dict[str, Any]:
        from src.react_agent.models import Message, ModelSettings
        from trievo_bench.model import parse_reply

        client = self._get_client("target")
        before_calls = client.calls
        before_usage = dict(getattr(client, "usage_totals", {}))
        metadata = self.turn_metadata.get("public", {})
        available = metadata.get("available_tools", []) if isinstance(metadata, dict) else []
        system = (
            "你是 L1 benchmark 的项目助理 Agent。只返回一个 JSON 对象，字段必须为 "
            "response、actions、tool_calls、events、state_update。严格依据用户请求完成任务；"
            "只能调用用户列出的模拟工具，调用格式为 tool=name、arguments={...}；"
            "不要声称执行了未输出的调用或状态更新。"
        )
        history = params.get("history", [])
        user = (
            f"当前用户请求：{request}\n\n"
            f"可用模拟工具：{json.dumps(available, ensure_ascii=False)}\n"
            f"会话历史：{json.dumps(history, ensure_ascii=False)}\n"
            f"工作区快照：{json.dumps(params.get('sandbox', {}), ensure_ascii=False)}"
        )
        settings = ModelSettings(
            temperature=float(self.options.get("temperature", 0.0)),
            max_tokens=int(self.options.get("max_tokens", self.options.get("generation_config", {}).get("max_tokens", 2048)))
            if isinstance(self.options.get("generation_config", {}), dict) else 2048,
        )
        raw = client.chat([Message("system", system), Message("user", user)], settings)
        parsed = parse_reply(str(raw or ""))
        parsed = _unwrap_nested_l1_reply(parsed)
        if not parsed.tool_calls and not parsed.actions and not parsed.state_update:
            correction = (
                f"{user}\n\n【格式纠正】上一响应没有返回可审计的 JSON。不要把本轮当作 response-only；"
                "必须只输出一个 JSON 对象，包含 response、actions、tool_calls、events、state_update。"
                "如果当前用户请求列出了模拟工具，必须在 tool_calls 中输出实际调用及 arguments。"
            )
            raw_retry = client.chat([Message("system", system), Message("user", correction)], settings)
            if raw_retry:
                raw = raw_retry
                parsed = parse_reply(str(raw_retry))
                parsed = _unwrap_nested_l1_reply(parsed)
        if not parsed.tool_calls:
            parsed.tool_calls = _extract_l1_tool_calls(str(raw or ""))
        return {
            "text": parsed.text or str(raw or ""),
            "actions": parsed.actions,
            "events": parsed.events,
            "tool_calls": parsed.tool_calls,
            "usage": {**_usage_delta(client, before_usage), "api_calls": max(0, client.calls - before_calls)},
            "actions_are_executed": False,
            "tool_calls_are_executed": False,
            "raw": {"protocol": "l1_json", "model_response": str(raw or ""), "parsed_response": parsed.raw},
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_turn(str(params.get("turn_id", "")))
        if self.state_dir is None or self.skill_dir is None:
            raise RuntimeError("Trace2Skill state is unavailable")
        if not bool(params.get("update_enabled", True)):
            reason = str(params.get("skip_reason") or "run_condition")
            self.turn_id = ""
            self.turn_metadata = {}
            self._agent = None
            self._clients = {}
            return {
                "completed": True,
                "events": [{
                    "type": "evolution.skipped",
                    "policy": self.evolution_policy,
                    "reason": reason,
                }],
                "usage": {},
            }
        history_dir = self.state_dir / "history" / _safe_name(self.turn_id)
        history_dir.mkdir(parents=True, exist_ok=True)
        trajectory = _sanitize_trajectory(params.get("trajectory", []))
        feedback = _sanitize_feedback(params.get("feedback", []))
        _write_json(history_dir / "trajectory.json", trajectory)
        _write_json(history_dir / "feedback.json", feedback)
        events: list[dict[str, Any]] = []
        evolution_client = self._clients.get("evolution")
        before_calls = evolution_client.calls if evolution_client else 0
        before_usage = dict(getattr(evolution_client, "usage_totals", {}))
        if self.evolution_policy == "disabled":
            events.append({"type": "evolution.skipped", "policy": self.evolution_policy})
        else:
            records = _trajectory_records(trajectory, feedback)
            if records:
                skill_before = _skill_file_hashes(self.skill_dir)
                evolver = self._get_evolver(history_dir)
                summary = evolver.run(records, input_mode="records")
                _write_json(history_dir / "evolution.json", _summary_json(summary))
                skill_after = _skill_file_hashes(self.skill_dir)
                events.append({
                    "type": "evolution.completed",
                    "policy": self.evolution_policy,
                    "records": len(records),
                    "changed": _changed_skill_files(skill_before, skill_after),
                })
            else:
                events.append({"type": "evolution.skipped", "reason": "empty_public_trajectory"})
        evolution_client = self._clients.get("evolution")
        evolution_calls = max(0, (evolution_client.calls if evolution_client else 0) - before_calls)
        self.turn_id = ""
        self.turn_metadata = {}
        self._agent = None
        self._clients = {}
        return {
            "completed": True,
            "events": events,
            "usage": {
                **_usage_delta(evolution_client, before_usage),
                "api_calls": evolution_calls,
            },
        }

    def end_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self._agent = None
        self._clients = {}
        self.workspace = None
        self.state_dir = None
        self.skills_dir = None
        self.skill_dir = None
        self.turn_id = ""
        self.turn_metadata = {}
        return {"closed": True}

    def _require_turn(self, turn_id: str) -> None:
        if not self.turn_id:
            raise RuntimeError("Trace2Skill turn has not started")
        if turn_id != self.turn_id:
            raise RuntimeError(f"inactive Trace2Skill turn: {turn_id}")

    def _seed_skills(self) -> None:
        if self.skills_dir is None:
            raise RuntimeError("Trace2Skill skills directory is unavailable")
        source = self._resolve_source(self.options.get("skills_dir"))
        selected = self.options.get("skill")
        if selected:
            selected_path = self._resolve_source(selected)
            target_name = _safe_name(str(self.options.get("skill_name", "xlsx")))
            target = self.skills_dir / target_name
            self._copy_skill_source(selected_path, target)
            return
        if source is None:
            source = self.repo_root / "baseline" / "Trace2Skill" / "spreadsheet_agent" / "skills"
        if not source.is_dir():
            raise RuntimeError(f"Trace2Skill skills directory not found: {source}")
        shutil.copytree(source, self.skills_dir, dirs_exist_ok=True)

    def _select_skill_dir(self) -> Path | None:
        if self.skills_dir is None:
            return None
        name = str(self.options.get("skill_name", "xlsx"))
        candidate = self.skills_dir / name
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            return candidate
        candidates = sorted(path for path in self.skills_dir.iterdir() if (path / "SKILL.md").is_file())
        return candidates[0] if candidates else None

    def _resolve_source(self, value: Any) -> Path | None:
        if value in (None, ""):
            return None
        path = Path(str(value)).expanduser()
        if path.is_absolute():
            return path.resolve()
        candidates = [self.repo_root / path, self.repo_root / "baseline" / "Trace2Skill" / path]
        return next((candidate.resolve() for candidate in candidates if candidate.exists()), candidates[0].resolve())

    @staticmethod
    def _copy_skill_source(source: Path, target: Path) -> None:
        if source.is_dir():
            if not (source / "SKILL.md").is_file():
                raise RuntimeError(f"Trace2Skill skill directory lacks SKILL.md: {source}")
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / "SKILL.md")
        else:
            raise RuntimeError(f"Trace2Skill skill source not found: {source}")

    def _skill_snapshot(self) -> bytes:
        if self.skill_dir is None or not self.skill_dir.is_dir():
            return b""
        chunks: list[bytes] = []
        for path in sorted(item for item in self.skill_dir.rglob("*") if item.is_file()):
            chunks.append(path.relative_to(self.skill_dir).as_posix().encode("utf-8"))
            chunks.append(path.read_bytes())
        return b"\0".join(chunks)

    def _get_client(self, stage: str) -> _CountingClient:
        if stage in self._clients:
            return self._clients[stage]
        client_options = dict(self.options)
        if client_options.get("use_cache") and not client_options.get("cache_path") and self.state_dir:
            client_options["cache_path"] = str(self.state_dir / "home" / f"{stage}-cache")
        if self._client_factory is not None:
            client = self._client_factory(client_options, stage)
        else:
            client = _build_client(client_options, self.repo_root, stage)
        self._clients[stage] = _CountingClient(client)
        return self._clients[stage]

    def _get_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        if self.workspace is None or self.skills_dir is None:
            raise RuntimeError("Trace2Skill flow has not started")
        client = self._get_client("target")
        if self._agent_factory is not None:
            self._agent = self._agent_factory(client, self.options, self.workspace, self.skills_dir)
            return self._agent
        source_root = _trace2skill_root(self.repo_root)
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        if str(source_root / "src") not in sys.path:
            sys.path.insert(0, str(source_root / "src"))
        from spreadsheet_agent import CLIOnlyAgent, CLISkillPreloadedAgent
        from spreadsheet_agent.agents.cli_workspace_agent import CLIWorkspaceAgent

        agent_name = _select_agent_name(self.options, self.turn_metadata)
        agent_class = {
            "cli_only": CLIOnlyAgent,
            "cli_workspace": CLIWorkspaceAgent,
            "cli_skill_preloaded": CLISkillPreloadedAgent,
        }.get(agent_name)
        if agent_class is None:
            raise RuntimeError(f"unsupported Trace2Skill agent: {agent_name}")
        kwargs = {
            "client": client,
            "max_turns": max(1, int(self.options.get("max_turns", 20))),
            "temperature": float(self.options.get("temperature", 0.0)),
            "verbose": bool(self.options.get("verbose", False)),
            "timeout": max(1, int(self.options.get("tool_timeout", 120))),
        }
        if agent_name == "cli_skill_preloaded":
            kwargs["skills_dir"] = str(self.skills_dir)
        log_dir = self.options.get("log_dir")
        if log_dir:
            kwargs["log_dir"] = str(self._resolve_log_dir(log_dir))
            kwargs["log_format"] = str(self.options.get("log_format", "jsonl"))
        self._agent = agent_class(**kwargs)
        return self._agent

    def _resolve_log_dir(self, value: Any) -> Path:
        if self.workspace is None:
            raise RuntimeError("Trace2Skill workspace is unavailable")
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def _build_context(self, request: str, params: dict[str, Any]) -> Any:
        source_root = _trace2skill_root(self.repo_root)
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        if str(source_root / "src") not in sys.path:
            sys.path.insert(0, str(source_root / "src"))
        try:
            from spreadsheet_agent.agents.base import AgentContext
        except (ImportError, TypeError):
            # Trace2Skill's public release targets Python 3.10+, while the
            # protocol contract can still be exercised on older runtimes.
            AgentContext = SimpleNamespace

        if self.workspace is None:
            raise RuntimeError("Trace2Skill workspace is unavailable")
        input_path = self._workbook_path(params, "input_file", "input.xlsx")
        output_path = self._workbook_path(params, "output_file", "output.xlsx")
        item = self.turn_metadata.get("item")
        item = item if isinstance(item, dict) else self.turn_metadata
        instruction_type = str(item.get("instruction_type", self.options.get("instruction_type", "")))
        answer_position = str(item.get("answer_position", self.options.get("answer_position", "")))
        spreadsheet_content = _spreadsheet_content(input_path)
        return AgentContext(
            working_dir=str(self.workspace),
            input_file=str(input_path),
            output_file=str(output_path),
            instruction=request,
            spreadsheet_content=spreadsheet_content,
            instruction_type=instruction_type,
            answer_position=answer_position,
            instance_id=str(item.get("id", f"{self.flow_id}-{self.turn_id}")),
        )

    def _workbook_path(self, params: dict[str, Any], key: str, default: str) -> Path:
        if self.workspace is None:
            raise RuntimeError("Trace2Skill workspace is unavailable")
        metadata = self.turn_metadata.get("item")
        metadata = metadata if isinstance(metadata, dict) else self.turn_metadata
        aliases = (key, "input_path") if key == "input_file" else (key, "output_path")
        value = params.get(key) or self.options.get(key)
        if not value:
            value = next((metadata.get(alias) for alias in aliases if metadata.get(alias)), None)
        value = value or default
        raw = str(value)
        if raw.startswith("/workspace/"):
            raw = raw[len("/workspace/"):]
        path = Path(raw).expanduser()
        path = path if path.is_absolute() else self.workspace / path
        resolved = path.resolve()
        if self.workspace not in resolved.parents and resolved != self.workspace:
            raise RuntimeError(f"Trace2Skill workbook path escapes workspace: {value}")
        if key == "input_file" and not resolved.exists():
            discovered = sorted(
                item for item in self.workspace.rglob("*.xlsx")
                if item.is_file() and not item.name.lower().endswith(("_output.xlsx", "output.xlsx"))
            )
            if discovered:
                return discovered[0].resolve()
        return resolved

    def _get_evolver(self, history_dir: Path) -> Any:
        if self.skill_dir is None:
            raise RuntimeError("Trace2Skill skill directory is unavailable")
        client = self._get_client("evolution")
        settings = dict(self.options)
        output_dir = history_dir / "evolver"
        output_dir.mkdir(parents=True, exist_ok=True)
        if self._evolver_factory is not None:
            return self._evolver_factory(client, self.skill_dir, settings, output_dir)
        source_root = _trace2skill_root(self.repo_root)
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        if str(source_root / "src") not in sys.path:
            sys.path.insert(0, str(source_root / "src"))
        from skill_evolver.parallel_success_evolving_agent import CombinedParallelSkillEvolver

        parse_failure_dir = history_dir / "parse_failures"
        return CombinedParallelSkillEvolver(
            client=client,
            skill_dir=self.skill_dir,
            batch_size=max(1, int(self.options.get("evolution_batch_size", 1))),
            merge_batch_size=max(2, int(self.options.get("merge_batch_size", 5))),
            max_workers=max(1, int(self.options.get("max_workers", 1))),
            max_merge_levels=max(1, int(self.options.get("max_merge_levels", 5))),
            temperature=float(self.options.get("evolution_temperature", 0.3)),
            max_tokens=_optional_int(self.options.get("evolution_max_tokens")),
            verbose=bool(self.options.get("evolution_verbose", False)),
            output_dir=output_dir,
            parse_failure_dir=parse_failure_dir,
            max_skill_lines=max(50, int(self.options.get("max_skill_lines", 500))),
            max_references=max(1, int(self.options.get("max_references", 5))),
            max_verification_rounds=max(0, int(self.options.get("max_verification_rounds", 1))),
            skip_translation=bool(self.options.get("skip_translation", False)),
            patch_pipeline=str(self.options.get("patch_pipeline", "json")),
            semantic_item_marker_format=str(self.options.get("semantic_item_marker_format", "bracket")),
            enable_json_format_self_fix=bool(self.options.get("enable_json_format_self_fix", True)),
        )


def _usage_delta(client: Any, before: dict[str, Any]) -> dict[str, int]:
    after = getattr(client, "usage_totals", {})
    return {key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
            for key in ("input_tokens", "output_tokens", "total_tokens")}


def _build_client(options: dict[str, Any], repo_root: Path, stage: str) -> Any:
    source_root = _trace2skill_root(repo_root)
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    if str(source_root / "src") not in sys.path:
        sys.path.insert(0, str(source_root / "src"))
    model_key = "evolution_model" if stage == "evolution" else "model"
    model = str(options.get(model_key) or options.get("model") or os.environ.get("TRIEVO_MODEL") or "gpt-4o")
    client_name = str(options.get("llm_client", "openai"))
    generation_config = options.get("generation_config", {})
    if isinstance(generation_config, str):
        path = Path(generation_config)
        if not path.is_absolute():
            path = repo_root / path
        generation_config = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else json.loads(generation_config)
        )
    if not isinstance(generation_config, dict):
        raise RuntimeError("Trace2Skill generation_config must be an object or JSON file")
    generation_config = _normalize_generation_config(generation_config)
    use_cache = bool(options.get("use_cache", False))
    cache_path = options.get("cache_path")
    if client_name == "api_chat":
        from src.react_agent.models import ApiChatClient

        config_path = str(options.get("api_chat_config", "config/llm_api.json"))
        return ApiChatClient(
            model=model,
            config_path=config_path,
            generation_config=generation_config or None,
            use_cache=use_cache,
            cache_path=str(cache_path) if cache_path else None,
            timeout=float(options.get("request_timeout", 600)),
        )
    from src.react_agent.models import OpenAIClient

    api_key = options.get("api_key") or os.environ.get("OPENAI_API_KEY") or os.environ.get("TRIEVO_API_KEY")
    base_url = options.get("base_url") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("TRIEVO_BASE_URL")
    return OpenAIClient(
        model=model,
        api_key=str(api_key) if api_key else None,
        base_url=str(base_url) if base_url else None,
        generation_config=generation_config or None,
        use_cache=use_cache,
        cache_path=str(cache_path) if cache_path else None,
        timeout=float(options.get("request_timeout", 600)),
    )


def _normalize_generation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Keep provider-specific knobs out of the OpenAI SDK keyword surface."""
    normalized = dict(config)
    thinking = normalized.pop("thinking", None)
    if thinking is not None:
        extra_body = normalized.get("extra_body")
        extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
        extra_body.setdefault("thinking", thinking)
        normalized["extra_body"] = extra_body
    return normalized


def _trace2skill_root(repo_root: Path) -> Path:
    return repo_root / "baseline" / "Trace2Skill"


def _select_agent_name(options: dict[str, Any], turn_metadata: dict[str, Any]) -> str:
    """Select the spreadsheet-aware agent only for spreadsheet turns.

    ``auto`` keeps the historical XLSX default when the benchmark item clearly
    describes a workbook, while generic file tasks use the tool-capable CLI
    agent instead of spending its turns on spreadsheet-specific actions.
    """
    requested = str(options.get("agent", "cli_skill_preloaded"))
    if requested != "auto":
        return requested
    item = turn_metadata.get("item") if isinstance(turn_metadata, dict) else None
    item = item if isinstance(item, dict) else turn_metadata
    candidates = [
        str(options.get("input_file", "")),
        str(item.get("input_file", "")) if isinstance(item, dict) else "",
        str(item.get("input_path", "")) if isinstance(item, dict) else "",
        str(item.get("instruction_type", "")) if isinstance(item, dict) else "",
    ]
    spreadsheet_markers = (".xlsx", ".xlsm", "spreadsheet", "workbook", "xlsx")
    if any(any(marker in value.lower() for marker in spreadsheet_markers) for value in candidates):
        return "cli_skill_preloaded"
    return "cli_workspace"


def _looks_like_l1_protocol_task(request: str, metadata: dict[str, Any] | None = None) -> bool:
    if isinstance(metadata, dict):
        public = metadata.get("public", metadata)
        source_format = public.get("source_format") if isinstance(public, dict) else None
        if source_format == "l1_demo_task_json":
            return True
        if source_format in {"l1_arena_task_json", "l2_taskstream_task_json"}:
            return False
    text = str(request).lower()
    return "可调用的模拟工具" in text or "available tools" in text


def _unwrap_nested_l1_reply(parsed: Any) -> Any:
    text = getattr(parsed, "text", "")
    if getattr(parsed, "tool_calls", None) or getattr(parsed, "actions", None) or getattr(parsed, "state_update", None):
        return parsed
    if isinstance(text, str) and text.lstrip().startswith("{"):
        from trievo_bench.model import parse_reply
        nested = parse_reply(text)
        if nested.raw != text:
            return nested
    return parsed


def _extract_l1_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    pattern = r"(calendar\.create_event|message\.resolve_group|message\.send_message|file\.create_file)\s*\(([^)]*)\)"
    for name, raw_args in re.findall(pattern, str(text or "")):
        args: dict[str, Any] = {}
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]*)['\"]", raw_args):
            args[key] = value
        calls.append({"tool": name, "arguments": args})
    return calls


def _steps_to_trace(steps: Any) -> dict[str, Any]:
    normalized_steps: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for step in steps if isinstance(steps, list) else []:
        action = getattr(step, "action", None)
        action_data = None
        if action is not None:
            action_data = {
                "name": str(getattr(action, "name", "")),
                "arguments": _json_safe(getattr(action, "arguments", {})),
            }
            call = {
                "turn": int(getattr(step, "turn", 0)),
                **action_data,
                "observation": str(getattr(step, "observation", "") or ""),
            }
            tool_calls.append(call)
            events.append({"type": "tool.completed", **call})
        normalized_steps.append({
            "turn": int(getattr(step, "turn", 0)),
            "thought": str(getattr(step, "thought", "") or ""),
            "action": action_data,
            "observation": str(getattr(step, "observation", "") or ""),
            "is_final": bool(getattr(step, "is_final", False)),
            "is_format_error": bool(getattr(step, "is_format_error", False)),
        })
    return {"steps": normalized_steps, "tool_calls": tool_calls, "events": events}


def _adapt_final_response_carrier(
    answer: str,
    tool_calls: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None]:
    """Bridge Trace2Skill's ReAct result into the declared public carrier.

    The framework may return ``None`` after successfully executing its final
    Python action. Adapt only that framework-level SRA carrier mismatch and
    retain an audit record; never rewrite a model answer to satisfy a strict
    native submission contract.
    """

    public = metadata.get("public", metadata) if isinstance(metadata, dict) else {}
    contract = public.get("submission_contract", {}) if isinstance(public, dict) else {}
    payload_kind = contract.get("payload_kind") if isinstance(contract, dict) else None
    raw_answer = str(answer or "")

    if payload_kind == "python_source":
        if "def task_func" in raw_answer:
            return raw_answer, None
        candidates = [
            str(call.get("arguments", {}).get("code", ""))
            for call in tool_calls
            if call.get("name") == "python" and isinstance(call.get("arguments"), dict)
        ]
        candidates = [code for code in candidates if "def task_func" in code]
        if candidates:
            source = max(candidates, key=len)
            return source, {
                "type": "executed_python_action_to_final_response",
                "raw_answer": raw_answer,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            }

    return raw_answer, None


def _trajectory_records(trajectory: Any, feedback: Any) -> list[dict[str, Any]]:
    feedback_by_index = {
        str(item.get("interaction_index")): item
        for item in feedback
        if isinstance(item, dict)
    } if isinstance(feedback, list) else {}
    records: list[dict[str, Any]] = []
    for position, item in enumerate(trajectory if isinstance(trajectory, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        index = str(item.get("index", item.get("interaction_index", position)))
        judge = feedback_by_index.get(index, {})
        passed = bool(item.get("passed", judge.get("passed", False)))
        score = item.get("score", judge.get("score", 0))
        feedback_text = str(item.get("feedback", judge.get("feedback", "")))
        request = str(item.get("request", ""))
        response = str(item.get("response", item.get("text", "")))
        detail = {
            "request": request,
            "response": response,
            "feedback": feedback_text,
            "score": score,
            "passed": passed,
            "tool_calls": item.get("tool_calls", []),
            "events": item.get("events", []),
        }
        item_type = "success_memory" if passed else "failure_cause"
        records.append({
            "record_source": "success" if passed else "error",
            "instance_id": index,
            "source_file": "tri-evobench-public-trajectory",
            "items": [{
                "type": item_type,
                "title": "Successful trajectory" if passed else "Failed trajectory",
                "description": "Public Judge feedback and execution trace; distill reusable spreadsheet guidance.",
                "content": json.dumps(_json_safe(detail), ensure_ascii=False, indent=2),
            }],
        })
    return records


def _sanitize_trajectory(value: Any) -> list[dict[str, Any]]:
    allowed = {
        "index", "request", "response", "actions", "tool_calls", "events",
        "state_update", "action_error", "score", "passed", "feedback",
        "sandbox", "state_keys", "usage",
    }
    if not isinstance(value, list):
        return []
    return [
        {key: _json_safe(item[key]) for key in allowed if key in item}
        for item in value
        if isinstance(item, dict)
    ]


def _sanitize_feedback(value: Any) -> list[dict[str, Any]]:
    allowed = {"interaction_index", "score", "passed", "feedback"}
    if not isinstance(value, list):
        return []
    return [
        {key: _json_safe(item[key]) for key in allowed if key in item}
        for item in value
        if isinstance(item, dict)
    ]


def _summary_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _summary_json(item)
            for key, item in value.items()
            if key not in {"patches", "final_patch", "edits", "diffs"}
        }
    if isinstance(value, (list, tuple)):
        return [_summary_json(item) for item in value]
    return _json_safe(value)


def _skill_file_hashes(skill_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(skill_dir).as_posix(): _sha256(path.read_bytes())
        for path in sorted(skill_dir.rglob("*"))
        if path.is_file()
    }


def _changed_skill_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _spreadsheet_content(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
        sheet = workbook.active
        rows: list[str] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if index > 5:
                rows.append(f"... ({max(0, sheet.max_row - 5)} more rows)")
                break
            rows.append(str(tuple("" if value is None else str(value) for value in row)))
        workbook.close()
        return "\n".join(rows)
    except Exception as exc:
        return f"[Could not read spreadsheet: {exc}]"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _safe_name(value: str) -> str:
    import re

    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    return clean[:64] or "turn"


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
    # Keep the NDJSON protocol UTF-8 on Windows. Provider control tokens in a
    # model response are not necessarily representable in the system GBK code
    # page and must not crash the adapter while serializing a trace.
    if hasattr(protocol_stdout, "reconfigure"):
        protocol_stdout.reconfigure(encoding="utf-8", errors="replace")
    bridge = Trace2SkillBridge()
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
    project_root = Path(__file__).resolve().parents[3]
    trace_root = project_root / "baseline" / "Trace2Skill"
    if str(trace_root) not in sys.path:
        sys.path.insert(0, str(trace_root))
    if str(trace_root / "src") not in sys.path:
        sys.path.insert(0, str(trace_root / "src"))
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    main()
