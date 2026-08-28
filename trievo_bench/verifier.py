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
        interaction_trace = json.dumps(trajectory.get("interactions", []), ensure_ascii=False)
        rubric = rubric.replace("{events}", event_trace)
        rubric = rubric.replace("{final_state}", json.dumps(trajectory.get("state", {}), ensure_ascii=False))
        if not has_event_placeholder:
            rubric += "\n\n【轨迹 events】\n" + event_trace
        if "{interactions}" in rubric:
            rubric = rubric.replace("{interactions}", interaction_trace)
        else:
            rubric += "\n\n【当前 Turn 已发生的 interaction 轨迹】\n" + interaction_trace
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


class L1LifeJudge:
    """Evaluator for the sealed, component-scored L1-Life protocol.

    The dataset supplies an immutable literal-template prompt per turn.  This
    class deliberately keeps that prompt and every expected value on the
    evaluator side; the baseline only receives the agent-input projection.
    """

    _allowed_scores = {0.0, 0.5, 1.0}

    def __init__(self, client: Any) -> None:
        self.client = client

    def evaluate(self, check: dict[str, Any], response: str, sandbox: Sandbox, trajectory: Any = None) -> CheckResult:
        trajectory = trajectory if isinstance(trajectory, dict) else {}
        values = {
            "stream_id": check.get("stream_id"),
            "turn_id": check.get("turn_id"),
            "turn_type": check.get("turn_type"),
            "turn_origin": check.get("turn_origin"),
            "agent_input": check.get("agent_input", {}),
            "final_response": response,
            "interaction_transcript": trajectory.get("interactions", []),
            "tool_calls": trajectory.get("tool_calls", []),
            "tool_results": trajectory.get("tool_results", []),
            "state_before": trajectory.get("state_before", {}),
            "state_after": trajectory.get("state_after", {}),
            "state_diff": trajectory.get("state_diff", []),
            "workspace_before": trajectory.get("workspace_before", {}),
            "workspace_after": trajectory.get("workspace_after", {}),
            "reference_context": check.get("reference_context"),
            "rubrics": check.get("rubrics", {}),
            "checks": check.get("checks", []),
            "expected_state": check.get("expected_state", {}),
            "expected_tool_call": check.get("expected_tool_call"),
            "expected_workspace_effect": check.get("expected_workspace_effect"),
            "gt": check.get("gt", {}),
        }
        prompt = self._render(str(check.get("judge_prompt", "")), values)
        max_tokens = int(check.get("max_output_tokens", 12000))
        result: dict[str, Any] | None = None
        scores: dict[str, float | None] | None = None
        source_scores: dict[int, float | None] | None = None
        last_error = "unknown judge parse error"
        for attempt in range(2):
            retry_note = "" if attempt == 0 else (
                "\n\n[RETRY AFTER INVALID OUTPUT]\n"
                "Return the required JSON object again. Every score must be exactly 0, 0.5, 1, or null; "
                "include every required check_id and every required source-rubric index exactly once."
            )
            try:
                text = self.client.complete(
                    [
                        {"role": "system", "content": "You are a strict L1-Life reference evaluator. Return JSON only."},
                        {"role": "user", "content": prompt + retry_note},
                    ],
                    max_tokens=max_tokens,
                    json_mode=True,
                )
            except RuntimeError:
                raise
            except Exception as exc:
                return self._invalid(f"judge request failed: {exc}")
            try:
                result = self._parse(text)
                scores, source_scores = self._validate(result, check)
                break
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
        if result is None or scores is None or source_scores is None:
            return self._invalid(last_error)
        total = self._aggregate(scores, source_scores, check)
        threshold = float(check.get("pass_threshold", 1.0))
        failed_dimensions = [
            str(item.get("dimension", item.get("check_id", "unknown")))
            for item in check.get("checks", [])
            if scores.get(str(item.get("check_id"))) not in {None, 1.0}
        ]
        feedback = "Task succeeded." if not failed_dimensions else "Failed dimensions: " + ", ".join(dict.fromkeys(failed_dimensions)) + "."
        return CheckResult(
            score=total,
            passed=total >= threshold,
            feedback=feedback,
            details={
                "type": "l1_life_llm_judge",
                "check_scores": scores,
                "source_rubric_scores": source_scores,
                "rationale": str(result.get("rationale", ""))[:1000],
            },
        )

    @staticmethod
    def _render(template: str, values: dict[str, Any]) -> str:
        if not template:
            raise ValueError("L1-Life judge prompt is missing")
        rendered = template
        for name, value in values.items():
            rendered = rendered.replace("{" + name + "}", json.dumps(value, ensure_ascii=False))
        return rendered

    @staticmethod
    def _parse(text: Any) -> dict[str, Any]:
        raw = str(text).strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                raise
            value = json.loads(raw[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("judge output must be a JSON object")
        return value

    def _validate(self, result: dict[str, Any], check: dict[str, Any]) -> tuple[dict[str, float | None], dict[int, float | None]]:
        if result.get("stream_id") != check.get("stream_id") or result.get("turn_id") != check.get("turn_id"):
            raise ValueError("judge output has an unexpected stream_id or turn_id")
        expected_checks = [str(item["check_id"]) for item in check.get("checks", [])]
        raw_checks = result.get("check_scores")
        if not isinstance(raw_checks, list):
            raise ValueError("judge output is missing check_scores")
        scores: dict[str, float | None] = {}
        for item in raw_checks:
            if not isinstance(item, dict):
                raise ValueError("check_scores items must be objects")
            check_id = str(item.get("check_id", ""))
            if check_id not in expected_checks or check_id in scores:
                raise ValueError("judge output has duplicate or unknown check_id")
            scores[check_id] = self._score(item.get("score"))
        if set(scores) != set(expected_checks):
            raise ValueError("judge output must cover every check_id exactly once")

        expected_indices = list(range(1, len(check.get("rubrics", {}).get("source_rubrics", [])) + 1))
        raw_source = result.get("source_rubric_scores")
        if not isinstance(raw_source, list):
            raise ValueError("judge output is missing source_rubric_scores")
        source_scores: dict[int, float | None] = {}
        for item in raw_source:
            if not isinstance(item, dict):
                raise ValueError("source_rubric_scores items must be objects")
            index = int(item.get("rubric_index", 0))
            if index not in expected_indices or index in source_scores:
                raise ValueError("judge output has duplicate or unknown rubric_index")
            source_scores[index] = self._score(item.get("score"))
        if set(source_scores) != set(expected_indices):
            raise ValueError("judge output must cover every source rubric exactly once")
        return scores, source_scores

    def _score(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("judge scores must be 0, 0.5, 1, or null")
        score = float(value)
        if score not in self._allowed_scores:
            raise ValueError("judge scores must be 0, 0.5, 1, or null")
        return score

    @staticmethod
    def _aggregate(scores: dict[str, float | None], source_scores: dict[int, float | None], check: dict[str, Any]) -> float:
        usable_source = [score for score in source_scores.values() if score is not None]
        source_mean = sum(usable_source) / len(usable_source) if usable_source else None
        numerator = 0.0
        denominator = 0.0
        for item in check.get("checks", []):
            score = source_mean if item.get("scoring") == "mean_source_rubrics" else scores[str(item["check_id"])]
            if score is None:
                continue
            weight = float(item.get("weight", 0.0))
            numerator += weight * score
            denominator += weight
        return numerator / denominator if denominator else 0.0

    @staticmethod
    def _invalid(error: str) -> CheckResult:
        return CheckResult(
            score=0.0,
            passed=False,
            feedback="L1-Life judge returned invalid output; retry is allowed.",
            details={"type": "l1_life_judge_parse_error", "error": error[:1000]},
        )
