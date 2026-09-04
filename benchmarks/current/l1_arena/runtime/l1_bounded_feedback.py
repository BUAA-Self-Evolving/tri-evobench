"""Deterministic non-leaking projection for L1 User Simulator feedback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "l1_bounded_feedback/1.0"
PACKAGED_SCHEMA_PATH = ROOT / "schemas" / "l1-bounded-feedback.schema.json"
SOURCE_SCHEMA_PATH = (
    ROOT / "scripts" / "l1_user_simulator_assets" / "l1-bounded-feedback.schema.json"
)
SCHEMA_PATH = PACKAGED_SCHEMA_PATH if PACKAGED_SCHEMA_PATH.is_file() else SOURCE_SCHEMA_PATH
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

PUBLIC_MESSAGES = {
    "incomplete_task": "The original task is incomplete. Please finish the requested work and verify the observable result.",
    "incorrect_result": "The observable result is incorrect. Please re-check the public task and revise the result without changing its scope.",
    "invalid_artifact": "The requested artifact is missing or invalid. Please complete it and verify that it is usable.",
    "missing_evidence": "The response does not provide enough public evidence that the requested work was completed. Please verify the observable result.",
    "execution_error": "The attempted action did not complete successfully. Please retry the original task using the available public information.",
    "unanswered_requirement": "One or more original requirements remain unanswered. Please complete the original request without adding new scope.",
    "stale_or_missing_context": "The response appears to rely on stale or missing public context. Please re-check the available public information and retry.",
    "unintended_persistence": "The interaction caused an unintended persistent or workspace side effect. Please correct it while preserving the original task scope.",
}

ALLOWED_SCORES = {0, 0.5, 1, None}
DIMENSION = re.compile(r"^[a-z][a-z0-9_]*$")


def project_bounded_feedback(
    component_results: list[dict[str, Any]],
    public_result_category: str,
) -> dict[str, Any]:
    """Project only dimension IDs and scores; never accept raw reasons or gold text."""
    if public_result_category not in PUBLIC_MESSAGES:
        raise ValueError(f"Unsupported public result category: {public_result_category}")
    failed_dimensions: list[str] = []
    for index, item in enumerate(component_results):
        if set(item) != {"dimension", "score"}:
            raise ValueError(
                f"component_results[{index}] must contain only dimension and score"
            )
        dimension = item["dimension"]
        score = item["score"]
        if not isinstance(dimension, str) or not DIMENSION.fullmatch(dimension):
            raise ValueError(f"Invalid dimension: {dimension!r}")
        if score not in ALLOWED_SCORES:
            raise ValueError(f"Invalid component score: {score!r}")
        if score is not None and score < 1 and dimension not in failed_dimensions:
            failed_dimensions.append(dimension)
    if not failed_dimensions:
        raise ValueError("Bounded feedback requires at least one failed dimension")
    result = {
        "schema_version": SCHEMA_VERSION,
        "failed_dimensions": failed_dimensions,
        "observable_deficiency": PUBLIC_MESSAGES[public_result_category],
        "public_result_category": public_result_category,
    }
    validate_bounded_feedback(result)
    return result


def validate_bounded_feedback(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("bounded feedback must be an object")
    required = {
        "schema_version",
        "failed_dimensions",
        "observable_deficiency",
        "public_result_category",
    }
    if set(value) != required:
        raise ValueError(f"bounded feedback fields mismatch: {sorted(set(value) ^ required)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("wrong bounded feedback schema version")
    category = value["public_result_category"]
    if category not in PUBLIC_MESSAGES:
        raise ValueError("invalid bounded feedback category")
    if value["observable_deficiency"] != PUBLIC_MESSAGES[category]:
        raise ValueError("observable deficiency must use the frozen public template")
    dimensions = value["failed_dimensions"]
    if not isinstance(dimensions, list) or not dimensions or len(dimensions) != len(set(dimensions)):
        raise ValueError("failed_dimensions must be a non-empty unique list")
    if any(not isinstance(item, str) or not DIMENSION.fullmatch(item) for item in dimensions):
        raise ValueError("invalid failed dimension")
