#!/usr/bin/env python3
"""Reference CLI adapter for an L2 V1 package-local native verifier.

The Benchmark Runtime supplies one JSON request on stdin and receives one
`l2_native_verifier_result/1.0` JSON object on stdout.  The package-local
`native_semantics.py` module preserves the selected source verifier semantics.
"""

from __future__ import annotations

import json
import inspect
import os
import sys
from pathlib import Path

from native_semantics import NativeVerifier


def main() -> int:
    turn_dir = Path(__file__).resolve().parent.parent
    task = json.loads((turn_dir / "task.json").read_text(encoding="utf-8"))
    verifier_id = task["evaluation"]["native_verifier"]["verifier_id"]
    try:
        request = json.load(sys.stdin)
        if request.get("schema_version") != "l2_native_verifier_request/1.0":
            raise ValueError("unsupported native verifier request schema")
        declared_submission = next(
            binding
            for binding in task["evaluation"]["native_verifier"]["input_bindings"]
            if binding["name"] == task["evaluation"]["submission_contract"]["source_binding"]
        )
        submission = resolve_submission(
            request["bindings"]["submission"],
            declared_submission,
            turn_dir,
        )
        verifier = NativeVerifier(allow_unsafe_execution=True)
        verify_parameters = inspect.signature(verifier.verify).parameters
        if "workspace" in verify_parameters:
            workspace = Path(os.environ.get("L2_VERIFIER_WORKSPACE", turn_dir.parent / "agent_workspace"))
            result = verifier.verify(
                submission,
                turn_dir,
                workspace=workspace,
            )
        else:
            result = verifier.verify(submission, turn_dir)
        output = normalize_result(verifier_id, result, task)
    except Exception as exc:  # Runtime must distinguish verifier error from task failure.
        output = error_result(verifier_id, f"{type(exc).__name__}: {exc}")
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def resolve_submission(binding: dict, declared_binding: dict, turn_dir: Path) -> str | Path:
    kind = binding.get("kind")
    declared_source = declared_binding["source"]
    if kind == "inline":
        if declared_source != "interaction_record":
            raise ValueError("inline submission violates the declared submission carrier")
        value = binding.get("value")
        if not isinstance(value, str):
            raise TypeError("inline submission must be a string")
        return value
    if kind == "path":
        if declared_source not in {"workspace", "evaluation_assets"}:
            raise ValueError("path submission violates the declared submission carrier")
        relative = binding.get("path")
        if not isinstance(relative, str):
            raise TypeError("path submission must be a string")
        if relative != declared_binding["path"]:
            raise ValueError("submission path does not match the declared binding")
        if relative == "agent_workspace" or relative.startswith("agent_workspace/"):
            workspace = Path(
                os.environ.get("L2_VERIFIER_WORKSPACE", turn_dir.parent / "agent_workspace")
            ).resolve()
            suffix = Path(relative).relative_to("agent_workspace")
            resolved = (workspace / suffix).resolve()
            if resolved != workspace and workspace not in resolved.parents:
                raise ValueError("submission path escapes the verifier Workspace")
            return resolved
        stream_dir = turn_dir.parent.resolve()
        resolved = (stream_dir / relative).resolve()
        if resolved != stream_dir and stream_dir not in resolved.parents:
            raise ValueError("submission path escapes the Stream package")
        return resolved
    raise ValueError("bindings.submission must use kind=inline or kind=path")


def normalize_result(verifier_id: str, result: object, task: dict) -> dict[str, object]:
    category = result.category
    # A NativeVerifier timeout is a completed semantic outcome with score 0.
    # Docker transport timeouts remain evaluator errors in the outer runtime.
    if category == "timeout":
        category = "fail"
    if category not in {"pass", "fail"}:
        return error_result(verifier_id, f"unknown verifier category: {category}")
    raw_score = float(result.score)
    auto_research = task["evaluation"].get("evaluation_profile") == "auto_research"
    score = normalize_auto_research_score(raw_score, task) if auto_research else raw_score
    output = {
        "schema_version": "l2_native_verifier_result/1.0",
        "verifier_id": verifier_id,
        "status": category,
        "score": score,
        "checks": [
            {
                "check_id": "native_verifier_outcome",
                "status": category,
                "score": score,
                "evidence": ["package-local native verifier completed"],
            }
        ],
        "artifacts": [],
        "public_summary": {
            "result_category": category,
            "observable_deficiencies": []
            if category == "pass"
            else ["The submitted result did not satisfy the native success condition."],
        },
        "private_diagnostics": result.details,
        "error": None,
    }
    if auto_research and category == "pass":
        metric_profile = task["evaluation"]["metric_profile"]
        output["metric"] = {
            "name": metric_profile["metric_name"],
            "raw_value": raw_score,
            "raw_direction": metric_profile["raw_direction"],
            "normalized_value": score,
        }
    return output


def normalize_auto_research_score(raw_score: float, task: dict) -> float:
    normalization = task["evaluation"]["reference_context"]["normalization"]
    method = normalization["method"]
    if method == "identity_0_1":
        return min(1.0, max(0.0, raw_score))
    raise ValueError(f"unsupported AutoResearch normalization method: {method}")


def error_result(
    verifier_id: str,
    message: str,
    diagnostics: object | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "l2_native_verifier_result/1.0",
        "verifier_id": verifier_id,
        "status": "error",
        "score": None,
        "checks": [],
        "artifacts": [],
        "public_summary": {
            "result_category": "error",
            "observable_deficiencies": ["The evaluator could not complete native verification."],
        },
        "private_diagnostics": {"details": diagnostics},
        "error": message,
    }


if __name__ == "__main__":
    raise SystemExit(main())
