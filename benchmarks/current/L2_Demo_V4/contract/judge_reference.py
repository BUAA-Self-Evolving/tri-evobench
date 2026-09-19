#!/usr/bin/env python3
"""Executable reference for L2 Judge prompt rendering and score aggregation."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent
JUDGE_OUTPUT_SCHEMA = json.loads(
    (ROOT / "schemas" / "l2-judge-output.schema.json").read_text(encoding="utf-8")
)
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def validate_evaluation_profile(task: dict[str, Any]) -> None:
    """Validate cross-field rules that JSON Schema cannot express cleanly."""

    evaluation = task["evaluation"]
    profile = evaluation.get("evaluation_profile")
    if profile is None:
        # Legacy l2_judge/1.0 Tasks predate explicit profile routing.
        if evaluation["judge_config"]["prompt_version"] != "l2_judge/1.0":
            raise ValueError("explicit evaluation_profile is required for Judge Prompt V2")
        return

    if profile == "general":
        if evaluation["judge_config"]["prompt_version"] != "l2_judge/1.0":
            raise ValueError("Contract V1 General profile requires Judge Prompt V1")
        return

    if profile != "auto_research":
        raise ValueError(f"unsupported evaluation profile: {profile}")

    metric = evaluation["metric_profile"]
    bands = metric["performance_bands"]
    if bands["full_min_inclusive"] < bands["partial_min_inclusive"]:
        raise ValueError("full performance threshold must be >= partial threshold")

    checks = evaluation["checks"]
    if len(checks) != 1 or checks[0]["check_id"] != "auto_research_overall":
        raise ValueError("AutoResearch must use one auto_research_overall ordinal check")
    if checks[0]["scoring"] != "ordinal" or not math.isclose(checks[0]["weight"], 1.0):
        raise ValueError("AutoResearch overall check must be ordinal with weight 1")

    judge_config = evaluation["judge_config"]
    if judge_config["prompt_version"] != "l2_judge/2.0":
        raise ValueError("AutoResearch requires Judge Prompt V2")
    if judge_config["rubric_route"]["domain_rubric"]["rubric_id"] != "auto_research":
        raise ValueError("AutoResearch must route the auto_research domain rubric")

    success_policy = evaluation["success_policy"]
    if success_policy.get("interaction_continuation") != "invalid_or_unverifiable_submission_only":
        raise ValueError("AutoResearch retry must be based on submission validity, not score")
    if success_policy.get("low_metric_is_retryable") is not False:
        raise ValueError("AutoResearch low metric must not be retryable")
    if task["user_simulator"]["trigger"] != "submission_invalid_or_unverifiable_and_budget_remaining":
        raise ValueError("AutoResearch User Simulator trigger must follow submission validity")


def validate_native_metric_result(task: dict[str, Any], result: dict[str, Any]) -> None:
    """Validate Task-declared AutoResearch metric evidence after Schema validation."""

    if task["evaluation"].get("evaluation_profile") != "auto_research":
        return
    metric_profile = task["evaluation"]["metric_profile"]
    metric = result.get("metric")
    if not isinstance(metric, dict):
        raise ValueError("AutoResearch native result must retain structured raw metric evidence")
    if metric["name"] != metric_profile["metric_name"]:
        raise ValueError("native metric name does not match Task metric_profile")
    if metric["raw_direction"] != metric_profile["raw_direction"]:
        raise ValueError("native metric direction does not match Task metric_profile")
    if result.get("score") is None or not math.isclose(
        metric["normalized_value"], result["score"], abs_tol=1e-12
    ):
        raise ValueError("native metric normalized_value must equal top-level score")


def render_judge_prompt(template: str, declared_inputs: list[str], context: dict[str, Any]) -> str:
    """Render only declared literal placeholders after deterministic JSON serialization."""

    placeholders = set(PLACEHOLDER.findall(template))
    declared = set(declared_inputs)
    if placeholders != declared:
        raise ValueError(
            f"template/declaration mismatch: missing={sorted(placeholders - declared)}, "
            f"unused={sorted(declared - placeholders)}"
        )
    missing = placeholders - set(context)
    if missing:
        raise ValueError(f"missing Judge inputs: {sorted(missing)}")
    rendered = template
    for name in sorted(placeholders):
        serialized = json.dumps(
            context[name], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        rendered = rendered.replace("{" + name + "}", serialized)
    return rendered


def load_routed_rubrics(task: dict[str, Any]) -> tuple[str, str | None]:
    """Load versioned rubric text from the contract-owned route."""

    validate_evaluation_profile(task)
    route = task["evaluation"]["judge_config"].get("rubric_route")
    if route is None:
        raise ValueError("rubric route is required for Judge Prompt V2")

    loaded: list[str | None] = []
    for ref in (route["domain_rubric"], route["benchmark_rubric"]):
        if ref is None:
            loaded.append(None)
            continue
        path = (ROOT / ref["path"]).resolve()
        rubric_root = (ROOT / "prompts" / "rubrics").resolve()
        if path.parent != rubric_root or not path.is_file():
            raise ValueError(f"rubric route is missing or escapes rubric root: {ref['path']}")
        loaded.append(path.read_text(encoding="utf-8"))
    return loaded[0] or "", loaded[1]


def validate_judge_output(task: dict[str, Any], interaction_index: int, output: dict[str, Any]) -> None:
    """Validate schema, identity, and exact Task-owned check/rubric coverage."""

    validate_evaluation_profile(task)
    Draft202012Validator(JUDGE_OUTPUT_SCHEMA).validate(output)
    if output["stream_id"] != task["stream_id"] or output["turn_id"] != task["turn_id"]:
        raise ValueError("Judge output identity does not match the Task")
    if output["interaction_index"] != interaction_index:
        raise ValueError("Judge output Interaction index does not match")

    declared_checks = [item["check_id"] for item in task["evaluation"]["checks"]]
    returned_checks = [item["check_id"] for item in output["check_scores"]]
    if len(returned_checks) != len(set(returned_checks)) or set(returned_checks) != set(declared_checks):
        raise ValueError("Judge output must cover every declared check exactly once")

    rubric_count = len(task["evaluation"]["rubrics"]["source_rubrics"])
    returned_rubrics = [item["rubric_index"] for item in output["source_rubric_scores"]]
    expected_rubrics = list(range(1, rubric_count + 1))
    if sorted(returned_rubrics) != expected_rubrics or len(returned_rubrics) != len(set(returned_rubrics)):
        raise ValueError("Judge output must cover every source rubric exactly once")


def aggregate_judge_score(task: dict[str, Any], output: dict[str, Any]) -> float:
    """Recompute the weighted non-null score; never trust a model-reported total."""

    validate_evaluation_profile(task)
    score_by_check = {item["check_id"]: item["score"] for item in output["check_scores"]}
    source_scores = [item["score"] for item in output["source_rubric_scores"]]
    non_null_source = [score for score in source_scores if score is not None]
    source_mean = sum(non_null_source) / len(non_null_source) if non_null_source else None

    numerator = 0.0
    denominator = 0.0
    for check in task["evaluation"]["checks"]:
        component = (
            score_by_check[check["check_id"]]
            if check["scoring"] == "ordinal"
            else source_mean
        )
        if component is None:
            continue
        numerator += check["weight"] * component
        denominator += check["weight"]
    if math.isclose(denominator, 0.0, abs_tol=1e-12):
        raise ValueError("all Judge score components are null")
    return numerator / denominator
