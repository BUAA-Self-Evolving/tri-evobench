from __future__ import annotations

import json
import math
import unittest

from trievo_bench.evaluation_outcome import (
    BOUNDED_FEEDBACK_SCHEMA_VERSION,
    BoundedFeedback,
    EvaluationOutcome,
    EvaluationOutcomeError,
    EvaluationStatus,
)


class EvaluationOutcomeTest(unittest.TestCase):
    def _feedback(self) -> BoundedFeedback:
        return BoundedFeedback(
            failed_dimensions=("output_schema", "evidence"),
            observable_deficiency=(
                "The produced artifact is missing externally visible evidence."
            ),
            public_result_category="fail",
        )

    def test_scored_passed_outcome_serializes_deterministically(self) -> None:
        outcome = EvaluationOutcome.scored(
            turn_score=0.8,
            passed=True,
            private_diagnostics={"z": 2, "a": {"nested": True}},
            audit_details={"retry_count": 0},
        )

        mapping = outcome.to_mapping()

        self.assertEqual(mapping["evaluation_status"], "passed")
        self.assertEqual(mapping["turn_score"], 0.8)
        self.assertTrue(mapping["passed"])
        self.assertIsNone(mapping["bounded_feedback"])
        self.assertEqual(list(mapping["private_diagnostics"]), ["a", "z"])
        self.assertEqual(
            json.dumps(mapping, sort_keys=True),
            json.dumps(outcome.to_mapping(), sort_keys=True),
        )

    def test_scored_failed_outcome_carries_only_public_bounded_feedback(self) -> None:
        outcome = EvaluationOutcome.scored(
            turn_score=0.25,
            passed=False,
            bounded_feedback=self._feedback(),
            private_diagnostics={
                "private_diagnostics": "hidden stack trace",
                "gt": "gold answer",
                "exact_fix_steps": ["patch file.py"],
            },
            audit_details={"judge_raw_output": {"rationale": "private reason"}},
        )

        feedback = outcome.bounded_feedback_mapping()

        self.assertEqual(
            feedback,
            {
                "schema_version": BOUNDED_FEEDBACK_SCHEMA_VERSION,
                "failed_dimensions": ["output_schema", "evidence"],
                "observable_deficiency": (
                    "The produced artifact is missing externally visible evidence."
                ),
                "public_result_category": "fail",
            },
        )
        self.assertNotIn("private_diagnostics", feedback)
        self.assertNotIn("gt", feedback)
        self.assertNotIn("exact_fix_steps", feedback)
        self.assertIn("private_diagnostics", outcome.to_mapping())

    def test_mapping_feedback_is_normalized(self) -> None:
        outcome = EvaluationOutcome.scored(
            turn_score=0.5,
            passed=False,
            bounded_feedback={
                "schema_version": BOUNDED_FEEDBACK_SCHEMA_VERSION,
                "failed_dimensions": ["submission"],
                "observable_deficiency": "The submission is externally unverifiable.",
                "public_result_category": "partial",
            },
        )

        self.assertEqual(
            outcome.to_mapping()["bounded_feedback"]["public_result_category"],
            "partial",
        )

    def test_evaluation_error_is_unscored_and_not_passed_or_feedback_bearing(self) -> None:
        outcome = EvaluationOutcome.evaluation_error(
            private_diagnostics={"stage": "judge", "raw_error": "invalid json"},
            audit_details={"retry_count": 1},
        )

        self.assertEqual(outcome.evaluation_status, EvaluationStatus.EVALUATION_ERROR)
        self.assertEqual(
            outcome.to_mapping(),
            {
                "evaluation_status": "evaluation_error",
                "turn_score": None,
                "passed": None,
                "bounded_feedback": None,
                "private_diagnostics": {
                    "raw_error": "invalid json",
                    "stage": "judge",
                },
                "audit_details": {"retry_count": 1},
            },
        )

    def test_invalid_states_are_rejected_at_construction(self) -> None:
        with self.assertRaisesRegex(EvaluationOutcomeError, "turn_score"):
            EvaluationOutcome.scored(turn_score=1.01, passed=True)

        with self.assertRaisesRegex(EvaluationOutcomeError, "numeric turn_score"):
            EvaluationOutcome.scored(turn_score=True, passed=True)  # type: ignore[arg-type]

        with self.assertRaisesRegex(EvaluationOutcomeError, "turn_score"):
            EvaluationOutcome.scored(turn_score=math.nan, passed=True)

        with self.assertRaisesRegex(EvaluationOutcomeError, "passed=True"):
            EvaluationOutcome(
                evaluation_status="passed",
                turn_score=0.9,
                passed=False,
            )

        with self.assertRaisesRegex(EvaluationOutcomeError, "turn_score=None"):
            EvaluationOutcome(
                evaluation_status="evaluation_error",
                turn_score=0.0,
                passed=None,
            )

        with self.assertRaisesRegex(EvaluationOutcomeError, "passed=None"):
            EvaluationOutcome(
                evaluation_status="evaluation_error",
                turn_score=None,
                passed=False,
            )

    def test_invalid_state_mutations_are_rejected_before_serialization(self) -> None:
        scored = EvaluationOutcome.scored(turn_score=0.7, passed=True)
        object.__setattr__(scored, "turn_score", -0.1)

        with self.assertRaisesRegex(EvaluationOutcomeError, "turn_score"):
            scored.to_mapping()

        error = EvaluationOutcome.evaluation_error()
        object.__setattr__(error, "bounded_feedback", self._feedback())

        with self.assertRaisesRegex(EvaluationOutcomeError, "bounded feedback"):
            error.to_mapping()

    def test_invalid_bounded_feedback_is_rejected(self) -> None:
        with self.assertRaisesRegex(EvaluationOutcomeError, "unique"):
            BoundedFeedback(
                failed_dimensions=("schema", "schema"),
                observable_deficiency="Duplicate visible dimensions.",
                public_result_category="fail",
            )

        with self.assertRaisesRegex(EvaluationOutcomeError, "public_result_category"):
            BoundedFeedback(
                failed_dimensions=("schema",),
                observable_deficiency="Bad public category.",
                public_result_category="private",
            )

        with self.assertRaisesRegex(EvaluationOutcomeError, "unknown public field"):
            EvaluationOutcome.scored(
                turn_score=0.2,
                passed=False,
                bounded_feedback={
                    "schema_version": BOUNDED_FEEDBACK_SCHEMA_VERSION,
                    "failed_dimensions": ["schema"],
                    "observable_deficiency": "Visible schema issue.",
                    "public_result_category": "fail",
                    "private_diagnostics": "must not be accepted as public feedback",
                },
            )

    def test_private_and_audit_details_must_be_json_compatible(self) -> None:
        with self.assertRaisesRegex(EvaluationOutcomeError, "JSON-compatible"):
            EvaluationOutcome.scored(
                turn_score=1.0,
                passed=True,
                private_diagnostics={"bad": object()},
            )


if __name__ == "__main__":
    unittest.main()
