from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


RUNTIME_PROTOCOL_VERSION = "1.0"


class SkillOptBridge:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.repo_root = Path(os.environ.get("TRIEVO_REPO_ROOT", Path.cwd())).resolve()
        self.workspace: Path | None = None
        self.state_dir: Path | None = None
        self.skill_file: Path | None = None
        self.flow_id = ""
        self.turn_id = ""
        self.turn_metadata: dict[str, Any] = {}
        self.turn_evolved = False
        self._last_summary: dict[str, Any] = {}
        self._initialized = False
        self.evolution_policy = "turn_boundary"

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
            raise RuntimeError("SkillOpt options must be an object")
        self.options = dict(options)
        evolution = params.get("evolution", {})
        if isinstance(evolution, dict):
            self.evolution_policy = str(evolution.get("policy", "turn_boundary"))
        if self.evolution_policy not in {"turn_boundary", "native", "disabled"}:
            raise RuntimeError(f"unsupported SkillOpt evolution policy: {self.evolution_policy}")
        self.repo_root = Path(
            str(self.options.get("repo_root") or os.environ.get("TRIEVO_REPO_ROOT") or Path.cwd())
        ).resolve()
        self._initialized = True
        return {
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "adapter": "skillopt",
            "capabilities": [
                "target_chat",
                "target_exec",
                "skill_reflection",
                "direct_workspace",
                "usage",
                "evolution_barrier",
            ],
        }

    def start_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            raise RuntimeError("SkillOpt bridge has not been initialized")
        workspace = Path(str(params.get("workspace", ""))).resolve()
        state_dir = Path(str(params.get("state_dir", ""))).resolve()
        if not workspace.is_dir():
            raise RuntimeError(f"SkillOpt workspace does not exist: {workspace}")
        state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.state_dir = state_dir
        self.flow_id = str(params.get("flow_id", ""))
        self.config = self._load_config()
        self._configure_models()
        self.skill_file = state_dir / "skill.md"
        self.skill_file.write_text(self._initial_skill(), encoding="utf-8")
        self._last_summary = self._token_summary()
        return {
            "framework": "SkillOpt",
            "workspace": str(workspace),
            "state_dir": str(state_dir),
            "environment": str(self.config.get("env", self.options.get("env", "generic"))),
            "skill_file": str(self.skill_file),
        }

    def start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.skill_file is None:
            raise RuntimeError("SkillOpt flow has not started")
        if self.turn_id and not self.turn_evolved:
            raise RuntimeError(f"SkillOpt turn {self.turn_id} has not crossed the evolution barrier")
        metadata = params.get("metadata", {})
        self.turn_id = str(params.get("turn_id", ""))
        self.turn_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self.turn_evolved = False
        return {"fresh_session": True, "skill_sha256": _sha256(self.skill_file.read_bytes())}

    def interact(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.skill_file is None or not self.turn_id:
            raise RuntimeError("SkillOpt turn has not started")
        turn_id = str(params.get("turn_id", ""))
        if turn_id != self.turn_id:
            raise RuntimeError(f"inactive SkillOpt turn: {turn_id}")
        request = str(params.get("request", "")).strip()
        if not request:
            raise RuntimeError("SkillOpt interaction request is empty")
        skill = self.skill_file.read_text(encoding="utf-8")
        system, user = self._build_prompt(skill, request, params)
        from skillopt.model import chat_target, is_target_exec_backend

        before = self._token_summary()
        if is_target_exec_backend():
            from skillopt.model.codex_harness import prepare_workspace, run_target_exec

            work_dir = tempfile.mkdtemp(prefix="trievo-skillopt-target-")
            try:
                prepare_workspace(work_dir=work_dir, skill_md=skill, task_text=f"{system}\n\n{user}")
                text, raw_usage = run_target_exec(
                    work_dir=work_dir,
                    prompt="Read task.md and solve the task using the skillopt-target skill. Return only the final answer.",
                    model=str(self.config.get("target_model", "")),
                    timeout=_optional_int(self.config.get("exec_timeout")) or 120,
                )
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
        else:
            text, raw_usage = chat_target(
                system=system,
                user=user,
                max_completion_tokens=int(self.config.get("max_completion_tokens", 16384)),
                retries=int(self.options.get("retries", 3)),
                stage="target",
                reasoning_effort=self.config.get("reasoning_effort"),
                timeout=_optional_int(self.config.get("exec_timeout")),
            )
        after = self._token_summary()
        usage = _usage_from_mapping(raw_usage)
        tracker_usage = _usage_delta(before, after)
        if tracker_usage["total_tokens"] or tracker_usage["api_calls"]:
            usage = tracker_usage
        self._last_summary = after
        response_text = _extract_answer(text) if str(self.config.get("env", "")) == "searchqa" else str(text or "")
        return {
            "text": response_text,
            "events": [{"type": "target.completed"}],
            "tool_calls": [],
            "usage": usage,
            "actions_are_executed": True,
            "tool_calls_are_executed": True,
            "raw": {"provider_usage": raw_usage, "model_response": str(text or "")},
        }

    def evolve(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.skill_file is None or not self.turn_id:
            raise RuntimeError("SkillOpt turn has not started")
        turn_id = str(params.get("turn_id", ""))
        if turn_id != self.turn_id:
            raise RuntimeError(f"inactive SkillOpt turn: {turn_id}")
        if self.turn_evolved:
            raise RuntimeError(f"SkillOpt turn {self.turn_id} was already evolved")
        before_summary = self._token_summary()
        policy = self.evolution_policy
        events: list[dict[str, Any]] = []
        changed_files: list[str] = []
        if policy == "disabled":
            events.append({"type": "evolution.skipped", "policy": policy})
        else:
            trajectory = params.get("trajectory", [])
            feedback = params.get("feedback", [])
            changed_files = self._reflect_and_update(trajectory, feedback)
            events.append({
                "type": "evolution.completed",
                "policy": policy,
                "changed_files": changed_files,
            })
        self.turn_evolved = True
        after_summary = self._token_summary()
        self._last_summary = after_summary
        return {
            "completed": True,
            "events": events,
            "usage": _usage_delta(before_summary, after_summary),
        }

    def end_flow(self, params: dict[str, Any]) -> dict[str, Any]:
        self.workspace = None
        self.state_dir = None
        self.skill_file = None
        self.turn_id = ""
        self.turn_metadata = {}
        return {"closed": True}

    def _load_config(self) -> dict[str, Any]:
        config_path = self.options.get("config")
        if config_path:
            path = self._resolve_path(str(config_path), expect_file=True)
            from skillopt.config import flatten_config, load_config

            config = flatten_config(load_config(str(path), overrides=self._config_overrides()))
        else:
            config = {}
        config.update(self._option_config_overrides())
        if str(config.get("optimizer_backend", "")) == "openai_compatible" or str(config.get("target_backend", "")) == "openai_compatible":
            config.setdefault("openai_compatible_base_url", os.environ.get("TRIEVO_BASE_URL", ""))
            config.setdefault("openai_compatible_api_key", os.environ.get("TRIEVO_API_KEY", ""))
            config.setdefault("openai_compatible_model", os.environ.get("TRIEVO_MODEL", ""))
            config.setdefault("optimizer_model", os.environ.get("TRIEVO_MODEL", config.get("optimizer_model", "gpt-5.5")))
            config.setdefault("target_model", os.environ.get("TRIEVO_MODEL", config.get("target_model", "gpt-5.5")))
        config.setdefault("env", self.options.get("env", "generic"))
        config.setdefault("max_completion_tokens", self.options.get("max_completion_tokens", 16384))
        config.setdefault("reasoning_effort", self.options.get("reasoning_effort", "medium"))
        config.setdefault("optimizer_backend", self.options.get("optimizer_backend", "openai_chat"))
        config.setdefault("target_backend", self.options.get("target_backend", "openai_chat"))
        config.setdefault("optimizer_model", self.options.get("optimizer_model", "gpt-5.5"))
        config.setdefault("target_model", self.options.get("target_model", "gpt-5.5"))
        return config

    def _config_overrides(self) -> list[str]:
        value = self.options.get("config_options", [])
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value] if isinstance(value, list) else []

    def _option_config_overrides(self) -> dict[str, Any]:
        allowed = {
            "env", "skill_init", "optimizer_backend", "target_backend", "optimizer_model",
            "target_model", "reasoning_effort", "max_completion_tokens", "exec_timeout",
            "workers", "analyst_workers", "failure_only", "minibatch_size", "edit_budget",
            "skill_update_mode", "use_skill_aware_reflection", "skill_aware_appendix_source",
            "seed", "merge_batch_size", "rewrite_reasoning_effort", "rewrite_max_completion_tokens",
            "openai_compatible_base_url", "openai_compatible_api_key", "openai_compatible_model",
            "optimizer_openai_compatible_base_url", "optimizer_openai_compatible_api_key",
            "optimizer_openai_compatible_model", "target_openai_compatible_base_url",
            "target_openai_compatible_api_key", "target_openai_compatible_model",
            "qwen_chat_base_url", "qwen_chat_api_key", "target_qwen_chat_base_url",
            "target_qwen_chat_api_key", "optimizer_qwen_chat_base_url", "optimizer_qwen_chat_api_key",
            "minimax_region", "minimax_base_url", "minimax_api_key",
            "azure_openai_endpoint", "azure_openai_api_version", "azure_openai_api_key",
            "azure_openai_auth_mode", "optimizer_azure_openai_endpoint",
            "optimizer_azure_openai_api_version", "optimizer_azure_openai_api_key",
            "optimizer_azure_openai_auth_mode", "target_azure_openai_endpoint",
            "target_azure_openai_api_version", "target_azure_openai_api_key",
            "target_azure_openai_auth_mode",
        }
        return {key: self.options[key] for key in allowed if key in self.options}

    def _initial_skill(self) -> str:
        candidate = self.options.get("skill") or self.config.get("skill_init")
        if not candidate:
            return "# SkillOpt Skill\n\nApply careful, evidence-grounded reasoning to the task.\n"
        path = self._resolve_path(str(candidate), expect_file=True)
        if not path.is_file():
            raise RuntimeError(f"SkillOpt skill file not found: {path}")
        return path.read_text(encoding="utf-8")

    def _resolve_path(self, value: str, *, expect_file: bool = False) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        candidates = [self.repo_root / path, self.repo_root / "baseline" / "SkillOpt" / path]
        if expect_file:
            for candidate in candidates:
                if candidate.is_file():
                    return candidate
        return candidates[0]

    def _configure_models(self) -> None:
        from skillopt.model import (
            configure_azure_openai,
            configure_claude_code_exec,
            configure_codex_exec_from_config,
            configure_minimax_chat,
            configure_openai_compatible,
            configure_qwen_chat,
            set_optimizer_backend,
            set_optimizer_deployment,
            set_reasoning_effort,
            set_target_backend,
            set_target_deployment,
        )

        optimizer_backend = str(self.config.get("optimizer_backend", "openai_chat"))
        target_backend = str(self.config.get("target_backend", "openai_chat"))
        set_optimizer_backend(optimizer_backend)
        set_target_backend(target_backend)
        set_optimizer_deployment(str(self.config.get("optimizer_model", "gpt-5.5")))
        set_target_deployment(str(self.config.get("target_model", "gpt-5.5")))
        configure_azure_openai(
            endpoint=_value(self.config, "azure_openai_endpoint", "azure_endpoint"),
            api_version=_value(self.config, "azure_openai_api_version", "azure_api_version"),
            api_key=_value(self.config, "azure_openai_api_key", "azure_api_key"),
            auth_mode=self.config.get("azure_openai_auth_mode"),
            optimizer_endpoint=self.config.get("optimizer_azure_openai_endpoint"),
            optimizer_api_version=self.config.get("optimizer_azure_openai_api_version"),
            optimizer_api_key=self.config.get("optimizer_azure_openai_api_key"),
            optimizer_auth_mode=self.config.get("optimizer_azure_openai_auth_mode"),
            target_endpoint=self.config.get("target_azure_openai_endpoint"),
            target_api_version=self.config.get("target_azure_openai_api_version"),
            target_api_key=self.config.get("target_azure_openai_api_key"),
            target_auth_mode=self.config.get("target_azure_openai_auth_mode"),
        )
        configure_openai_compatible(
            base_url=self.config.get("openai_compatible_base_url"),
            api_key=self.config.get("openai_compatible_api_key"),
            model=self.config.get("openai_compatible_model"),
            optimizer_base_url=self.config.get("optimizer_openai_compatible_base_url"),
            optimizer_api_key=self.config.get("optimizer_openai_compatible_api_key"),
            optimizer_model=self.config.get("optimizer_openai_compatible_model"),
            target_base_url=self.config.get("target_openai_compatible_base_url"),
            target_api_key=self.config.get("target_openai_compatible_api_key"),
            target_model=self.config.get("target_openai_compatible_model"),
        )
        configure_qwen_chat(
            base_url=self.config.get("qwen_chat_base_url"),
            api_key=self.config.get("qwen_chat_api_key"),
            target_base_url=self.config.get("target_qwen_chat_base_url"),
            target_api_key=self.config.get("target_qwen_chat_api_key"),
            optimizer_base_url=self.config.get("optimizer_qwen_chat_base_url"),
            optimizer_api_key=self.config.get("optimizer_qwen_chat_api_key"),
        )
        configure_minimax_chat(
            region=self.config.get("minimax_region"),
            base_url=self.config.get("minimax_base_url"),
            api_key=self.config.get("minimax_api_key"),
        )
        configure_codex_exec_from_config(self.config)
        configure_claude_code_exec(
            path=self.config.get("claude_code_exec_path", "claude"),
            profile=self.config.get("claude_code_exec_profile", ""),
            effort=self.config.get("claude_code_exec_effort", self.config.get("reasoning_effort", "medium")),
        )
        set_reasoning_effort(self.config.get("reasoning_effort"))

    def _build_prompt(self, skill: str, request: str, params: dict[str, Any]) -> tuple[str, str]:
        history = params.get("history", [])
        sandbox = params.get("sandbox", {})
        environment = str(self.config.get("env", self.options.get("env", "generic")))
        output_rule = ""
        if environment == "searchqa":
            output_rule = " Return the answer inside <answer>...</answer> tags."
        system = (
            "You are the SkillOpt target agent. Follow the Skill below as durable "
            "task-solving guidance. Answer the current request using only the supplied "
            "request, conversation history, and workspace snapshot. Do not claim that "
            "files or tools were changed unless the benchmark explicitly provides such a tool."
            + output_rule
            + "\n\n"
            f"## Skill\n{skill.strip()}"
        )
        public_item = self.turn_metadata.get("item")
        if isinstance(public_item, dict):
            request = f"{request}\n\n## Benchmark item\n{json.dumps(public_item, ensure_ascii=False)}"
        user = (
            f"## Request\n{request}\n\n"
            f"## Conversation history\n{json.dumps(history, ensure_ascii=False)}\n\n"
            f"## Workspace snapshot\n{json.dumps(sandbox, ensure_ascii=False)}"
        )
        return system, user

    def _reflect_and_update(self, trajectory: Any, feedback: Any) -> list[str]:
        if not isinstance(trajectory, list) or not trajectory:
            return []
        if self.state_dir is None or self.skill_file is None:
            raise RuntimeError("SkillOpt state is unavailable")
        from skillopt.gradient.reflect import run_minibatch_reflect
        from skillopt.gradient.aggregate import merge_patches
        from skillopt.optimizer.clip import rank_and_select
        from skillopt.optimizer.skill import apply_patch_with_report
        from skillopt.optimizer.rewrite import rewrite_skill_from_suggestions
        from skillopt.optimizer.update_modes import (
            get_payload_items,
            is_full_rewrite_minibatch_mode,
            normalize_update_mode,
        )

        results = _trajectory_results(trajectory, feedback)
        skill = self.skill_file.read_text(encoding="utf-8")
        history_dir = self.state_dir / "history" / _safe_name(self.turn_id)
        prediction_dir = history_dir / "predictions"
        patches_dir = history_dir / "patches"
        self._write_prediction_conversations(results, prediction_dir)
        raw_patches = run_minibatch_reflect(
            results=results,
            skill_content=skill,
            prediction_dir=str(prediction_dir),
            patches_dir=str(patches_dir),
            workers=max(1, int(self.config.get("analyst_workers", self.options.get("workers", 1)))),
            failure_only=bool(self.config.get("failure_only", False)),
            minibatch_size=max(1, int(self.config.get("minibatch_size", 8))),
            edit_budget=max(1, int(self.config.get("edit_budget", 4))),
            random_seed=_optional_int(self.config.get("seed")),
            update_mode=str(self.config.get("skill_update_mode", "patch")),
            skill_aware_reflection=self.config.get("use_skill_aware_reflection"),
            skill_aware_appendix_source=self.config.get("skill_aware_appendix_source"),
        )
        failure: list[dict[str, Any]] = []
        success: list[dict[str, Any]] = []
        for item in raw_patches:
            if not isinstance(item, dict):
                continue
            patch = item.get("patch", item)
            if not isinstance(patch, dict):
                continue
            if item.get("source_type") == "success":
                success.append(patch)
            else:
                failure.append(patch)
        merged = merge_patches(
            skill,
            failure,
            success,
            batch_size=max(2, int(self.config.get("merge_batch_size", 8))),
            workers=max(1, int(self.config.get("analyst_workers", self.options.get("workers", 1)))),
            update_mode=str(self.config.get("skill_update_mode", "patch")),
        )
        selected = rank_and_select(
            skill,
            merged,
            max_edits=max(1, int(self.config.get("edit_budget", 4))),
            update_mode=str(self.config.get("skill_update_mode", "patch")),
        )
        update_mode = normalize_update_mode(str(self.config.get("skill_update_mode", "patch")))
        report: list[dict[str, Any]] = []
        if update_mode == "rewrite_from_suggestions":
            rewrite = rewrite_skill_from_suggestions(
                skill,
                selected,
                env=self.config.get("env"),
                reasoning_effort=self.config.get("rewrite_reasoning_effort", self.config.get("reasoning_effort")),
                max_completion_tokens=int(self.config.get("rewrite_max_completion_tokens", 64000)),
            )
            updated = str(rewrite.get("new_skill", skill)) if isinstance(rewrite, dict) else skill
        elif is_full_rewrite_minibatch_mode(update_mode):
            candidates = get_payload_items(selected, update_mode)
            candidate = next(
                (item for item in candidates if isinstance(item, dict) and str(item.get("new_skill", "")).strip()),
                None,
            )
            updated = str(candidate["new_skill"]).rstrip() + "\n" if candidate else skill
        else:
            updated, report = apply_patch_with_report(skill, selected)
        if updated != skill:
            self.skill_file.write_text(updated, encoding="utf-8")
        (history_dir / "evolution.json").write_text(
            json.dumps({"results": results, "feedback": feedback, "patches": raw_patches, "edit_report": report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        changed = ["skill.md"] if updated != skill else []
        if (history_dir / "evolution.json").is_file():
            changed.append((history_dir / "evolution.json").relative_to(self.state_dir).as_posix())
        return changed

    @staticmethod
    def _write_prediction_conversations(results: list[dict[str, Any]], prediction_dir: Path) -> None:
        for item in results:
            item_id = _safe_name(str(item.get("id", "item")))
            item_dir = prediction_dir / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            conversation = [
                {"role": "user", "content": str(item.get("question", ""))},
                {"role": "assistant", "content": str(item.get("response", ""))},
                {"role": "system", "content": str(item.get("feedback", ""))},
            ]
            (item_dir / "conversation.json").write_text(
                json.dumps(conversation, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _token_summary(self) -> dict[str, Any]:
        try:
            from skillopt.model import get_token_summary

            return get_token_summary()
        except Exception:
            return {}


def _trajectory_results(trajectory: list[Any], feedback: Any) -> list[dict[str, Any]]:
    feedback_by_index = {
        str(item.get("interaction_index")): item
        for item in feedback
        if isinstance(item, dict)
    } if isinstance(feedback, list) else {}
    results: list[dict[str, Any]] = []
    for item in trajectory:
        if not isinstance(item, dict):
            continue
        index = str(item.get("index", item.get("interaction_index", len(results) + 1)))
        judge = feedback_by_index.get(index, {})
        score = float(item.get("score", judge.get("score", 0)) or 0)
        response = str(item.get("response", item.get("text", "")))
        results.append({
            "id": index,
            "hard": 1 if bool(item.get("passed", judge.get("passed", score >= 1))) else 0,
            "soft": score,
            "question": str(item.get("request", "")),
            "task_description": str(item.get("request", "")),
            "predicted_answer": response,
            "response": response,
            "feedback": str(item.get("feedback", judge.get("feedback", ""))),
        })
    return results


def _usage_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "api_calls": 0}
    input_tokens = int(value.get("input_tokens", value.get("prompt_tokens", 0)) or 0)
    output_tokens = int(value.get("output_tokens", value.get("completion_tokens", 0)) or 0)
    total_tokens = int(value.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
        "api_calls": max(0, int(value.get("api_calls", value.get("calls", 1 if total_tokens else 0)) or 0)),
    }


def _usage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_total = before.get("_total", {}) if isinstance(before, dict) else {}
    after_total = after.get("_total", {}) if isinstance(after, dict) else {}
    input_tokens = max(0, int(after_total.get("prompt_tokens", 0) or 0) - int(before_total.get("prompt_tokens", 0) or 0))
    output_tokens = max(0, int(after_total.get("completion_tokens", 0) or 0) - int(before_total.get("completion_tokens", 0) or 0))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "api_calls": max(0, int(after_total.get("calls", 0) or 0) - int(before_total.get("calls", 0) or 0)),
    }


def _value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


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


def _extract_answer(value: Any) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    start = lower.find("<answer>")
    end = lower.find("</answer>", start + len("<answer>")) if start >= 0 else -1
    if start >= 0 and end >= 0:
        return text[start + len("<answer>"):end].strip()
    return text


def main() -> None:
    protocol_stdout = sys.stdout
    bridge = SkillOptBridge()
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


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[3]
    skillopt_root = project_root / "baseline" / "SkillOpt"
    if str(skillopt_root) not in sys.path:
        sys.path.insert(0, str(skillopt_root))
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    main()
