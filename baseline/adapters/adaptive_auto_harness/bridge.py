from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

PROTOCOL_VERSION = "1.0"

try:
    from agent_evolve.protocol.base_agent import BaseAgent as _BaseAgent
except ImportError:
    class _BaseAgent:  # type: ignore[no-redef]
        """Allows manifest-only tests without installing optional A-Evolve dependencies."""

        pass


@dataclass
class _Completion:
    text: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    finish_reason: str = "stop"


@dataclass
class _EvolutionOptions:
    evolve_prompts: bool
    evolve_skills: bool
    evolve_memory: bool
    evolve_tools: bool


class OpenAIChatBackend:
    def __init__(self, options: dict[str, Any], *, stage: str) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Adaptive Auto-Harness requires the openai package") from exc
        prefix = "evolver_" if stage == "evolution" else ""
        self.model = str(options.get(f"{prefix}model") or options.get("model") or os.getenv("TRIEVO_MODEL", ""))
        if not self.model:
            raise ValueError("options.model (or TRIEVO_MODEL) is required")
        key_env = str(options.get(f"{prefix}api_key_env") or options.get("api_key_env") or "TRIEVO_API_KEY")
        api_key = os.getenv(key_env) or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(f"missing model API key in {key_env} or OPENAI_API_KEY")
        base_url = str(
            options.get(f"{prefix}base_url")
            or options.get("base_url")
            or os.getenv("TRIEVO_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or ""
        ).strip()
        self.client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.temperature = float(options.get(f"{prefix}temperature", options.get("temperature", 0.0)))
        self.max_tokens = int(options.get(f"{prefix}max_tokens", options.get("max_output_tokens", 4096)))
        self.thinking = str(options.get(f"{prefix}thinking") or options.get("thinking") or os.getenv("TRIEVO_THINKING", "")).strip().lower()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> _Completion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.thinking in {"enabled", "disabled"}:
            kwargs["extra_body"] = {"thinking": {"type": self.thinking}}
        response = self.client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            status = getattr(response, "status", None)
            message = getattr(response, "message", None) or "response has no usable choice"
            raise RuntimeError(f"OpenAI-compatible provider error (status {status}): {message}")
        choice = choices[0]
        message = choice.message
        calls: list[dict[str, Any]] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"raw": call.function.arguments}
            calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": arguments,
                }
            )
        raw_usage = response.usage
        input_tokens = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(raw_usage, "completion_tokens", 0) or 0)
        return _Completion(
            text=message.content or "",
            tool_calls=calls,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "api_calls": 1,
            },
            finish_reason=str(choice.finish_reason or "stop"),
        )


class AdaptiveWorkspaceAgent(_BaseAgent):
    """Generic A-Evolve solver backed by the framework filesystem contract."""

    def __init__(
        self,
        workspace_dir: str | Path,
        task_workspace: str | Path,
        backend: OpenAIChatBackend,
        options: dict[str, Any],
    ) -> None:
        self.task_workspace = Path(task_workspace).resolve()
        self.backend = backend
        self.options = options
        self.last_usage: dict[str, Any] = {}
        if _BaseAgent.__module__.startswith("agent_evolve"):
            super().__init__(workspace_dir)
        else:
            self.workspace = _contract_workspace(workspace_dir)
            self.system_prompt = ""
            self.skills: list[Any] = []
            self.memories: list[dict[str, Any]] = []
            self.reload_from_fs()

    def reload_from_fs(self) -> None:
        self.system_prompt = self.workspace.read_prompt()
        self.skills = self.workspace.list_skills()
        self.memories = self.workspace.read_all_memories(limit=200)

    def solve(self, task_id: str, task_input: str) -> dict[str, Any]:
        system = self._system_prompt()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task_input},
        ]
        steps: list[dict[str, Any]] = []
        total_usage: dict[str, Any] = {}
        final_text = ""
        max_steps = max(1, int(self.options.get("max_steps", 24)))
        for _ in range(max_steps):
            completion = self.backend.complete(messages, _TASK_TOOLS)
            _add_usage(total_usage, completion.usage)
            if completion.text:
                final_text = completion.text
            assistant: dict[str, Any] = {"role": "assistant", "content": completion.text or None}
            if completion.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                        },
                    }
                    for call in completion.tool_calls
                ]
            messages.append(assistant)
            if not completion.tool_calls:
                break
            for call in completion.tool_calls:
                result = self._execute_tool(call["name"], call["arguments"])
                steps.append(
                    {
                        "tool": call["name"],
                        "arguments": call["arguments"],
                        "result": result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )
        self.last_usage = total_usage
        return {"task_id": task_id, "output": final_text, "steps": steps, "conversation": messages}

    def _system_prompt(self) -> str:
        parts = [
            self.system_prompt
            or (
                "You are an autonomous agent operating in /workspace. Use the terminal tool to inspect "
                "and modify the workspace, complete the task, and verify the result before answering."
            )
        ]
        if self.memories:
            parts.append("## Learned memory\n" + "\n".join(f"- {item.get('content', item)}" for item in self.memories))
        if self.skills:
            rendered = []
            for skill in self.skills:
                rendered.append(self.get_skill_content(skill.name))
            parts.append("## Learned skills\n" + "\n\n".join(rendered))
        return "\n\n".join(part for part in parts if part.strip())

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "terminal":
            return f"ERROR: unknown tool {name}"
        command = str(arguments.get("command", ""))
        timeout = max(1.0, float(self.options.get("tool_timeout", 120)))
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                cwd=self.task_workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env={**os.environ, "HOME": str(self.task_workspace)},
            )
            output = (result.stdout or "") + (result.stderr or "")
            if not output.strip():
                output = f"(exit code {result.returncode}; no output)"
            limit = int(self.options.get("max_tool_output_chars", 30000))
            if len(output) > limit:
                half = max(1, limit // 2)
                output = output[:half] + "\n... [truncated] ...\n" + output[-half:]
            return output
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout:g} seconds"
        except Exception as exc:
            return f"ERROR: {exc}"


class AdaptiveAutoHarnessBridge:
    def __init__(self, backend_factory: Callable[[dict[str, Any], str], Any] | None = None) -> None:
        self.options: dict[str, Any] = {}
        self.evolution: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.agent: AdaptiveWorkspaceAgent | None = None
        self.turn_id: str | None = None
        self._backend_factory = backend_factory or (lambda options, stage: OpenAIChatBackend(options, stage=stage))

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("protocol_version", "")) != PROTOCOL_VERSION:
            raise ValueError("unsupported baseline runtime protocol")
        baseline_id = str(params.get("baseline_id", "adaptive-auto-harness"))
        if baseline_id != "adaptive-auto-harness":
            raise ValueError(f"unexpected baseline id: {baseline_id}")
        self.options = dict(params.get("options", {}))
        self.evolution = dict(params.get("evolution", {}))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "framework": "adaptive-auto-harness",
            "integration": "base-agent+aevolve",
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self.workspace = Path(str(params["workspace"])).resolve()
        self.state_dir = Path(str(params["state_dir"])).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._seed_contract_workspace()
        _contract_workspace(self.state_dir)
        return {"flow_id": str(params.get("flow_id", "")), "contract_version": "1.0"}

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.workspace is None or self.state_dir is None:
            raise RuntimeError("flow has not started")
        self.turn_id = str(params["turn_id"])
        backend = self._backend_factory(self.options, "interaction")
        self.agent = AdaptiveWorkspaceAgent(self.state_dir, self.workspace, backend, self.options)
        return {"turn_id": self.turn_id, "fresh_session": True}

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.agent is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("interaction belongs to an inactive turn")
        trajectory = self.agent.solve(
            f"{self.turn_id}-{int(params.get('interaction_index', 1))}",
            str(params.get("request", "")),
        )
        return {
            "text": trajectory["output"],
            "actions": [],
            "tool_calls": list(trajectory["steps"]),
            "events": [{"type": "tool.completed", "tool": step.get("tool")} for step in trajectory["steps"]],
            "usage": self.agent.last_usage,
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.state_dir is None or str(params.get("turn_id")) != self.turn_id:
            raise RuntimeError("evolution belongs to an inactive turn")
        if not bool(params.get("update_enabled", True)):
            return {
                "completed": True,
                "events": [{"type": "evolution.skipped", "reason": params.get("skip_reason")}],
                "usage": {},
            }
        if str(self.evolution.get("policy", "turn_boundary")) == "disabled":
            return {
                "completed": True,
                "events": [{"type": "evolution.skipped", "reason": "policy_disabled"}],
                "usage": {},
            }

        public_context = _public_evolution_context(params)
        backend = self._backend_factory(self.options, "evolution")
        result, changed = _run_native_evolution(
            self.state_dir,
            public_context,
            backend,
            self.options,
        )
        history_dir = self.state_dir / "evolution" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "turn_id": self.turn_id,
            "summary": _evolution_summary(result),
            "changed_files": changed,
        }
        (history_dir / f"{_safe_name(self.turn_id or 'turn')}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if self.agent is not None:
            self.agent.reload_from_fs()
        return {
            "completed": True,
            "events": [{"type": "evolution.completed", **record}],
            "usage": result.get("usage", {}),
        }

    def end_flow(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.agent = None
        self.turn_id = None
        return {"completed": True}

    def _seed_contract_workspace(self) -> None:
        if self.state_dir is None:
            return
        defaults = {
            "manifest.yaml": (
                "name: trievo-adaptive-auto-harness\n"
                "version: 0.1.0\n"
                "contract_version: \"1.0\"\n"
                "agent:\n  type: trievo-generic\n"
                "evolvable_layers:\n  - prompts\n  - skills\n  - memory\n  - tools\n"
                "reload_strategy: hot\n"
            ),
            "prompts/system.md": (
                "You are an autonomous software and research agent operating in /workspace. "
                "Use the terminal to inspect the available files, implement the requested result, "
                "and verify it before returning a concise final response.\n"
            ),
            "memory/memories.jsonl": "",
            "tools/registry.yaml": "tools: []\n",
        }
        for relative, content in defaults.items():
            target = self.state_dir / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (self.state_dir / "skills").mkdir(parents=True, exist_ok=True)


def _contract_workspace(root: str | Path) -> Any:
    """Load A-Evolve lazily so manifest/selection tests need no optional dependencies."""
    from agent_evolve.contract.workspace import AgentWorkspace

    return AgentWorkspace(root)


def _run_native_evolution(
    state_dir: Path,
    public_context: dict[str, Any],
    backend: Any,
    options: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Run the upstream AEvolveEngine while adapting its tool pass to NDJSON."""
    from agent_evolve.algorithms.aevolve.engine import AEvolveEngine
    from agent_evolve.config import EvolveConfig
    from agent_evolve.contract.workspace import AgentWorkspace

    config = EvolveConfig(
        max_cycles=1,
        evolve_prompts=bool(options.get("evolve_prompts", True)),
        evolve_skills=bool(options.get("evolve_skills", True)),
        evolve_memory=bool(options.get("evolve_memory", True)),
        evolve_tools=bool(options.get("evolve_tools", False)),
        evolve_infra=False,
        evolver_model=str(options.get("evolver_model") or options.get("model") or ""),
        evolver_max_tokens=int(options.get("evolver_max_tokens", 4096)),
        evolver_temperature=float(options.get("evolver_temperature", 0.0)),
    )
    changed: list[str] = []

    class TriEvoEngine(AEvolveEngine):
        def _run_llm(
            self,
            prompt: str,
            workspace_root: Path,
            evolver_workspace: Path | None = None,
            system_prompt: str | None = None,
        ) -> dict[str, Any]:
            del evolver_workspace, system_prompt
            proposal_prompt = (
                prompt
                + "\n\n### Complete public evidence\n"
                + json.dumps(public_context, ensure_ascii=False, indent=2)
                + "\n\n### Current editable state\n"
                + _evolution_prompt(Path(workspace_root), public_context, config)
            )
            completion = backend.complete(
                [
                    {"role": "system", "content": _EVOLVER_SYSTEM_PROMPT},
                    {"role": "user", "content": proposal_prompt},
                ]
            )
            proposal = _extract_json_object(completion.text)
            changed.extend(_apply_proposal(Path(workspace_root), proposal, config))
            return {"content": str(proposal.get("summary", "")), "usage": completion.usage}

    logs = _native_observation_logs(public_context)
    engine = TriEvoEngine(config)
    try:
        result = engine.evolve(AgentWorkspace(state_dir), logs, evo_number=_next_evolution_number(state_dir))
    finally:
        # AEvolveEngine uses Git as a transactional implementation detail.
        # The canonical learned state is the declared filesystem contract,
        # so do not leave a second, undeclared persistence channel behind.
        shutil.rmtree(state_dir / ".git", ignore_errors=True)
    return result, sorted(set(changed))


def _native_observation_logs(public_context: dict[str, Any]) -> list[dict[str, Any]]:
    feedback_by_index = {
        int(item.get("interaction_index", 0)): item
        for item in public_context.get("feedback", [])
        if isinstance(item, dict)
    }
    result: list[dict[str, Any]] = []
    for position, item in enumerate(public_context.get("trajectory", []), 1):
        if not isinstance(item, dict):
            continue
        index = int(item.get("index", position))
        feedback = feedback_by_index.get(index, {})
        request = str(item.get("request", ""))
        response = str(item.get("response", ""))
        record: dict[str, Any] = {
            "task_id": f"{public_context.get('turn_id', 'turn')}-{index}",
            "task_input": request,
            "batch": _next_evolution_number_from_context(public_context),
            "conversation": [
                {"role": "user", "content": request},
                {"role": "assistant", "content": response},
            ],
            "steps": list(item.get("tool_calls", [])) if isinstance(item.get("tool_calls"), list) else [],
        }
        for key in ("score", "passed"):
            if key in feedback:
                record["success" if key == "passed" else key] = feedback[key]
            elif key in item:
                record["success" if key == "passed" else key] = item[key]
        if "feedback" in feedback:
            record["feedback"] = feedback["feedback"]
        result.append(record)
    return result


def _next_evolution_number(state_dir: Path) -> int:
    history = state_dir / "evolution" / "history"
    return len(list(history.glob("*.json"))) + 1 if history.exists() else 1


def _next_evolution_number_from_context(public_context: dict[str, Any]) -> int:
    del public_context
    return 1


def _evolution_summary(result: dict[str, Any]) -> str:
    changed = []
    for key, label in (
        ("prompt_changed", "prompt"),
        ("memory_changed", "memory"),
        ("tools_changed", "tools"),
    ):
        if result.get(key):
            changed.append(label)
    new_skills = int(result.get("new_skills", 0) or 0)
    if new_skills:
        changed.append(f"{new_skills} skill(s)")
    return "A-Evolve updated " + ", ".join(changed) if changed else "A-Evolve made no state change"


_TASK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "Run a shell command in /workspace. Use it to inspect, edit, and verify task files.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


_EVOLVER_SYSTEM_PROMPT = """You are the A-Evolve meta-learning component. Improve only the adaptive agent's
declared filesystem-contract state using evidence from the public trajectory and public judge feedback.
Return JSON only with this shape:
{
  "summary": "short explanation",
  "files": {
    "prompts/system.md": "complete replacement content",
    "memory/memories.jsonl": "complete replacement JSONL content",
    "skills/<name>/SKILL.md": "complete skill content",
    "tools/registry.yaml": "complete replacement YAML"
  },
  "delete": ["skills/<obsolete>/SKILL.md"]
}
Only include files that need to change. Prefer small evidence-backed changes. Never mention or infer hidden
verifier data. Do not write executable tool files; the Tri-EvoBench adapter intentionally limits evolution
to reviewable prompt, skill, memory, and registry artifacts."""


def _evolve_config(options: dict[str, Any]) -> _EvolutionOptions:
    return _EvolutionOptions(
        evolve_prompts=bool(options.get("evolve_prompts", True)),
        evolve_skills=bool(options.get("evolve_skills", True)),
        evolve_memory=bool(options.get("evolve_memory", True)),
        evolve_tools=bool(options.get("evolve_tools", False)),
    )


def _evolution_prompt(state_dir: Path, public_context: dict[str, Any], config: _EvolutionOptions) -> str:
    current: dict[str, str] = {}
    for relative in ("manifest.yaml", "prompts/system.md", "memory/memories.jsonl", "tools/registry.yaml"):
        path = state_dir / relative
        if path.is_file():
            current[relative] = path.read_text(encoding="utf-8")
    for path in sorted((state_dir / "skills").glob("*/SKILL.md")):
        current[path.relative_to(state_dir).as_posix()] = path.read_text(encoding="utf-8")
    permissions = {
        "prompts": config.evolve_prompts,
        "skills": config.evolve_skills,
        "memory": config.evolve_memory,
        "tools": config.evolve_tools,
        "infra": False,
    }
    return json.dumps(
        {"permissions": permissions, "current_state": current, "public_evidence": public_context},
        ensure_ascii=False,
        indent=2,
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("A-Evolve returned no JSON proposal")
        value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("A-Evolve proposal must be a JSON object")
    return value


def _apply_proposal(state_dir: Path, proposal: dict[str, Any], config: _EvolutionOptions) -> list[str]:
    changed: list[str] = []
    files = proposal.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("A-Evolve proposal files must be an object")
    for raw_relative, raw_content in files.items():
        relative = _allowed_state_path(str(raw_relative), config)
        if relative is None or not isinstance(raw_content, str):
            continue
        target = state_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != raw_content:
            target.write_text(raw_content, encoding="utf-8")
            changed.append(relative.as_posix())
    deletes = proposal.get("delete", [])
    if isinstance(deletes, list):
        for raw_relative in deletes:
            relative = _allowed_state_path(str(raw_relative), config)
            if relative is None:
                continue
            target = state_dir / relative
            if target.is_file():
                target.unlink()
                changed.append(relative.as_posix())
    return sorted(set(changed))


def _allowed_state_path(value: str, config: _EvolutionOptions) -> Path | None:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    normalized = relative.as_posix()
    if config.evolve_prompts and normalized == "prompts/system.md":
        return Path(*relative.parts)
    if config.evolve_memory and normalized.startswith("memory/") and normalized.endswith(".jsonl"):
        return Path(*relative.parts)
    if config.evolve_skills and normalized.startswith("skills/") and normalized.endswith("/SKILL.md"):
        return Path(*relative.parts)
    if config.evolve_tools and normalized == "tools/registry.yaml":
        return Path(*relative.parts)
    return None


def _public_evolution_context(params: dict[str, Any]) -> dict[str, Any]:
    trajectory = []
    for raw in params.get("trajectory", []):
        if isinstance(raw, dict):
            trajectory.append(
                {key: raw[key] for key in ("index", "request", "response", "score", "passed", "tool_calls", "events") if key in raw}
            )
    feedback = []
    for raw in params.get("feedback", []):
        if isinstance(raw, dict):
            feedback.append(
                {key: raw[key] for key in ("interaction_index", "feedback", "score", "passed") if key in raw}
            )
    return {"turn_id": str(params.get("turn_id", "")), "trajectory": trajectory, "feedback": feedback}


def _add_usage(total: dict[str, Any], update: dict[str, Any]) -> None:
    for key in ("input_tokens", "output_tokens", "cached_read_tokens", "cached_write_tokens", "total_tokens", "api_calls"):
        total[key] = int(total.get(key, 0)) + int(update.get(key, 0))


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "_.-" else "-" for char in value)[:80] or "turn"


def _serve() -> None:
    bridge = AdaptiveAutoHarnessBridge()
    handlers = {
        "initialize": bridge.initialize,
        "start_flow": bridge.start_flow,
        "start_turn": bridge.start_turn,
        "interact": bridge.interact,
        "evolve": bridge.evolve,
        "end_flow": bridge.end_flow,
    }
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            method = str(request.get("method", ""))
            if method not in handlers:
                raise ValueError(f"unknown method: {method}")
            result = handlers[method](dict(request.get("params", {})))
            response = {"id": request.get("id"), "result": result}
        except Exception as exc:
            response = {"id": request.get("id"), "error": {"message": str(exc)}}
        print(json.dumps(response, ensure_ascii=True), flush=True)
        if request.get("method") == "end_flow":
            break


if __name__ == "__main__":
    _serve()
