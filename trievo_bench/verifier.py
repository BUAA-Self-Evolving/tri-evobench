from __future__ import annotations

import json
import importlib.util
from dataclasses import dataclass
from typing import Any

from .sandbox import Sandbox


@dataclass
class CheckResult:
    score: float
    passed: bool
    feedback: str
    details: dict[str, Any]


class DeterministicVerifier:
    def evaluate(self, check: dict[str, Any], response: str, sandbox: Sandbox) -> CheckResult:
        kind = check.get("type")
        if kind == "response_contains":
            passed = str(check.get("value", "")) in response
            return self._result(passed, check, "The response did not include the required information.")
        if kind == "file_exists":
            try:
                sandbox.read_file(str(check["path"]))
                passed = True
            except Exception:
                passed = False
            return self._result(passed, check, "The required file was not created.")
        if kind == "file_contains":
            try:
                content = sandbox.read_file(str(check["path"]))
                passed = str(check.get("value", "")) in content
            except Exception:
                passed = False
            return self._result(passed, check, "The required artifact is missing or does not satisfy the content constraint.")
        if kind == "json_file_equals":
            try:
                actual = json.loads(sandbox.read_file(str(check["path"])))
                passed = actual == check.get("value")
            except Exception:
                passed = False
            return self._result(passed, check, "The structured artifact does not match the required state.")
        if kind in {"all", "any"}:
            children = [self.evaluate(child, response, sandbox) for child in check.get("checks", [])]
            passed = all(child.passed for child in children) if kind == "all" else any(child.passed for child in children)
            details = {"children": [child.details for child in children]}
            message = "One or more required checks failed." if kind == "all" else "No acceptable check passed."
            return self._result(passed, check, message, details)
        raise ValueError(f"unsupported verifier type: {kind}")

    def _result(self, passed: bool, check: dict[str, Any], default_feedback: str, details: dict[str, Any] | None = None) -> CheckResult:
        return CheckResult(
            score=1.0 if passed else 0.0,
            passed=passed,
            feedback=str(check.get("feedback", "Task succeeded." if passed else default_feedback)),
            details=details or {"type": check.get("type"), "passed": passed},
        )


class PythonVerifier:
    """Run a task-owned verify.py against an exported sandbox workspace."""

    def evaluate(self, check: dict[str, Any], response: str, sandbox: Sandbox, trajectory: Any = None) -> CheckResult:
        script = str(check["path"])
        module_spec = importlib.util.spec_from_file_location("trievo_task_verifier", script)
        if module_spec is None or module_spec.loader is None:
            raise ValueError(f"cannot load verifier: {script}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        workspace = sandbox.export_workspace()
        try:
            result = module.verify(response, str(workspace), trajectory or {})
        finally:
            sandbox.cleanup_export(workspace)
        score = float(result.get("score", 0.0))
        threshold = float(check.get("pass_threshold", 0.8))
        feedback = result.get("feedback", "")
        if isinstance(feedback, list):
            feedback = "; ".join(str(item) for item in feedback)
        return CheckResult(
            score=max(0.0, min(1.0, score)),
            passed=score >= threshold,
            feedback=str(feedback) if feedback else ("Task succeeded." if score >= threshold else "Program verifier did not pass."),
            details={"type": "python_verifier", "result": result},
        )


class LLMJudge:
    """Stateless rubric judge instantiated independently for each Interaction."""

    def __init__(self, client: Any, rubric: str, max_output_tokens: int = 2048) -> None:
        self.client = client
        self.rubric = rubric
        self.max_output_tokens = max_output_tokens

    def evaluate(self, check: dict[str, Any], response: str, sandbox: Sandbox, trajectory: Any = None) -> CheckResult:
        context_files: dict[str, str] = {}
        for virtual_path in check.get("context_files", []):
            try:
                context_files[str(virtual_path)] = sandbox.read_file(str(virtual_path))
            except Exception:
                context_files[str(virtual_path)] = "(file unavailable)"
        rubric = str(check.get("rubric", self.rubric))
        rubric = rubric.replace("{final_response}", response)
        rubric = rubric.replace("{workspace_files}", json.dumps(context_files, ensure_ascii=False))
        trajectory = trajectory or {}
        has_event_placeholder = "{events}" in rubric
        rubric = rubric.replace("{tool_calls}", json.dumps(trajectory.get("tool_calls", []), ensure_ascii=False))
        event_trace = json.dumps(trajectory.get("events", []), ensure_ascii=False)
        rubric = rubric.replace("{events}", event_trace)
        rubric = rubric.replace("{final_state}", json.dumps(trajectory.get("state", {}), ensure_ascii=False))
        if not has_event_placeholder:
            rubric += "\n\n【轨迹 events】\n" + event_trace
        for placeholder, virtual_path in check.get("file_placeholders", {}).items():
            try:
                content = sandbox.read_file(str(virtual_path))
            except Exception:
                content = "(file unavailable)"
            rubric = rubric.replace("{" + str(placeholder) + "}", content)
        prompt = {
            "rubric": rubric,
            "response": response,
            "sandbox": sandbox.snapshot(),
            "format": {"score": "number in [0,1]", "passed": "boolean", "feedback": "brief local feedback"},
        }
        text = self.client.complete([
            {"role": "system", "content": "You are a strict evaluator. Return JSON only."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ], max_tokens=self.max_output_tokens, json_mode=True)
        try:
            try:
                result = json.loads(text.strip())
            except json.JSONDecodeError:
                start = text.find("{")
                end = text.rfind("}")
                if start < 0 or end <= start:
                    raise
                result = json.loads(text[start : end + 1])
            score = float(result["score"])
            threshold = float(check.get("pass_threshold", 1.0))
            passed = bool(result.get("passed", score >= threshold))
            feedback = result.get("feedback", result.get("rationale", ""))
            return CheckResult(score=max(0.0, min(1.0, score)), passed=passed, feedback=str(feedback), details={"type": "llm", "raw": result})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return CheckResult(score=0.0, passed=False, feedback="LLM judge returned invalid JSON; retry is allowed.", details={"type": "llm_parse_error", "raw": text[:1000], "error": str(exc)})
