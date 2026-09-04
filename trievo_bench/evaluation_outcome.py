"""Structured evaluator outcomes shared by benchmark runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from typing import Any, Mapping


BOUNDED_FEEDBACK_SCHEMA_VERSION = "l2_bounded_feedback/1.0"


class EvaluationOutcomeError(ValueError):
    """Raised when an evaluator outcome violates the runtime contract."""


class EvaluationStatus(str, Enum):
    """Terminal evaluator statuses understood by runtime consumers."""

    PASSED = "passed"
    FAILED = "failed"
    EVALUATION_ERROR = "evaluation_error"


class PublicResultCategory(str, Enum):
    """Public categories allowed in bounded feedback."""

    FAIL = "fail"
    PARTIAL = "partial"
    ERROR = "error"


@dataclass(frozen=True)
class BoundedFeedback:
    """Public evaluator feedback safe to show to a User Simulator."""

    failed_dimensions: tuple[str, ...]
    observable_deficiency: str
    public_result_category: PublicResultCategory | str
    schema_version: str = BOUNDED_FEEDBACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failed_dimensions",
            tuple(self.failed_dimensions),
        )
        object.__setattr__(
            self,
            "public_result_category",
            _parse_public_result_category(self.public_result_category),
        )
        self.validate()

    def validate(self) -> None:
        if self.schema_version != BOUNDED_FEEDBACK_SCHEMA_VERSION:
            raise EvaluationOutcomeError("bounded feedback schema_version is invalid")
        if not self.failed_dimensions:
            raise EvaluationOutcomeError("failed_dimensions must be non-empty")
        if len(set(self.failed_dimensions)) != len(self.failed_dimensions):
            raise EvaluationOutcomeError("failed_dimensions must be unique")
        for dimension in self.failed_dimensions:
            if not isinstance(dimension, str) or not dimension:
                raise EvaluationOutcomeError(
                    "failed_dimensions must contain non-empty strings"
                )
        if not isinstance(self.observable_deficiency, str) or not self.observable_deficiency:
            raise EvaluationOutcomeError("observable_deficiency must be non-empty")

    def to_mapping(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "failed_dimensions": list(self.failed_dimensions),
            "observable_deficiency": self.observable_deficiency,
            "public_result_category": self.public_result_category.value,
        }


@dataclass(frozen=True)
class EvaluationOutcome:
    """Terminal Turn evaluation result.

    ``passed`` and ``failed`` are scored Agent outcomes. ``evaluation_error`` is a
    control-plane evaluator failure and intentionally has no Agent pass/fail
    value and no Turn score.
    """

    evaluation_status: EvaluationStatus | str
    turn_score: float | None
    passed: bool | None
    bounded_feedback: BoundedFeedback | Mapping[str, Any] | None = None
    private_diagnostics: Mapping[str, Any] = field(default_factory=dict)
    audit_details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evaluation_status",
            _parse_evaluation_status(self.evaluation_status),
        )
        if self.turn_score is not None:
            object.__setattr__(self, "turn_score", _coerce_turn_score(self.turn_score))
        if self.bounded_feedback is not None and not isinstance(
            self.bounded_feedback, BoundedFeedback
        ):
            object.__setattr__(
                self,
                "bounded_feedback",
                _bounded_feedback_from_mapping(self.bounded_feedback),
            )
        object.__setattr__(
            self,
            "private_diagnostics",
            _json_compatible_mapping(self.private_diagnostics, "private_diagnostics"),
        )
        object.__setattr__(
            self,
            "audit_details",
            _json_compatible_mapping(self.audit_details, "audit_details"),
        )
        self.validate()

    @classmethod
    def scored(
        cls,
        *,
        turn_score: float,
        passed: bool,
        bounded_feedback: BoundedFeedback | Mapping[str, Any] | None = None,
        private_diagnostics: Mapping[str, Any] | None = None,
        audit_details: Mapping[str, Any] | None = None,
    ) -> "EvaluationOutcome":
        return cls(
            evaluation_status=(
                EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED
            ),
            turn_score=turn_score,
            passed=passed,
            bounded_feedback=bounded_feedback,
            private_diagnostics=private_diagnostics or {},
            audit_details=audit_details or {},
        )

    @classmethod
    def evaluation_error(
        cls,
        *,
        private_diagnostics: Mapping[str, Any] | None = None,
        audit_details: Mapping[str, Any] | None = None,
    ) -> "EvaluationOutcome":
        return cls(
            evaluation_status=EvaluationStatus.EVALUATION_ERROR,
            turn_score=None,
            passed=None,
            bounded_feedback=None,
            private_diagnostics=private_diagnostics or {},
            audit_details=audit_details or {},
        )

    def validate(self) -> None:
        status = _parse_evaluation_status(self.evaluation_status)
        if status is EvaluationStatus.EVALUATION_ERROR:
            if self.turn_score is not None:
                raise EvaluationOutcomeError(
                    "evaluation_error outcomes must have turn_score=None"
                )
            if self.passed is not None:
                raise EvaluationOutcomeError(
                    "evaluation_error outcomes must have passed=None"
                )
            if self.bounded_feedback is not None:
                raise EvaluationOutcomeError(
                    "evaluation_error outcomes must not expose bounded feedback"
                )
            return

        if not isinstance(self.passed, bool):
            raise EvaluationOutcomeError("scored outcomes must carry a boolean passed")
        if status is EvaluationStatus.PASSED and not self.passed:
            raise EvaluationOutcomeError("passed status requires passed=True")
        if status is EvaluationStatus.FAILED and self.passed:
            raise EvaluationOutcomeError("failed status requires passed=False")
        _coerce_turn_score(self.turn_score)
        if self.bounded_feedback is not None:
            if not isinstance(self.bounded_feedback, BoundedFeedback):
                raise EvaluationOutcomeError(
                    "bounded_feedback must be a BoundedFeedback object"
                )
            self.bounded_feedback.validate()

    def bounded_feedback_mapping(self) -> dict[str, Any] | None:
        """Return only public feedback fields allowed in simulator prompts."""

        self.validate()
        if self.bounded_feedback is None:
            return None
        return self.bounded_feedback.to_mapping()

    def to_mapping(self) -> dict[str, Any]:
        """Serialize deterministically to a JSON-compatible mapping."""

        self.validate()
        return {
            "evaluation_status": _parse_evaluation_status(
                self.evaluation_status
            ).value,
            "turn_score": self.turn_score,
            "passed": self.passed,
            "bounded_feedback": self.bounded_feedback_mapping(),
            "private_diagnostics": _json_compatible_mapping(
                self.private_diagnostics, "private_diagnostics"
            ),
            "audit_details": _json_compatible_mapping(
                self.audit_details, "audit_details"
            ),
        }


def _parse_evaluation_status(value: EvaluationStatus | str) -> EvaluationStatus:
    if isinstance(value, EvaluationStatus):
        return value
    if isinstance(value, str):
        try:
            return EvaluationStatus(value)
        except ValueError as exc:
            allowed = tuple(status.value for status in EvaluationStatus)
            raise EvaluationOutcomeError(
                f"unknown evaluation_status {value!r}; expected one of {allowed}"
            ) from exc
    raise EvaluationOutcomeError("evaluation_status must be a string")


def _parse_public_result_category(
    value: PublicResultCategory | str,
) -> PublicResultCategory:
    if isinstance(value, PublicResultCategory):
        return value
    if isinstance(value, str):
        try:
            return PublicResultCategory(value)
        except ValueError as exc:
            allowed = tuple(category.value for category in PublicResultCategory)
            raise EvaluationOutcomeError(
                f"unknown public_result_category {value!r}; expected one of {allowed}"
            ) from exc
    raise EvaluationOutcomeError("public_result_category must be a string")


def _bounded_feedback_from_mapping(raw: Mapping[str, Any]) -> BoundedFeedback:
    allowed_keys = {
        "schema_version",
        "failed_dimensions",
        "observable_deficiency",
        "public_result_category",
    }
    extra_keys = set(raw) - allowed_keys
    if extra_keys:
        raise EvaluationOutcomeError(
            f"bounded_feedback has unknown public field(s): {sorted(extra_keys)}"
        )
    return BoundedFeedback(
        schema_version=str(raw.get("schema_version", "")),
        failed_dimensions=tuple(raw.get("failed_dimensions", ())),
        observable_deficiency=str(raw.get("observable_deficiency", "")),
        public_result_category=raw.get("public_result_category", ""),
    )


def _coerce_turn_score(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EvaluationOutcomeError("scored outcomes must carry a numeric turn_score")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise EvaluationOutcomeError("turn_score must be in [0, 1]")
    return score


def _json_compatible_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationOutcomeError(f"{field_name} must be a mapping")
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise EvaluationOutcomeError(f"{field_name} must be JSON-compatible") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise EvaluationOutcomeError(f"{field_name} must serialize to an object")
    return decoded
