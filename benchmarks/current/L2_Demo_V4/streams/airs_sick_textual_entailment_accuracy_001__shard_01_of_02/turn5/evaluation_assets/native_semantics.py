"""Package-local AIRS SICK Workspace-artifact verifier."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from verification import VerifyResult


class AirsSickAccuracyVerifier:
    def __init__(self, *, allow_unsafe_execution: bool, timeout_seconds: float = 180.0):
        # Kept for the common verifier constructor; this verifier never executes
        # Agent code and only reads the frozen Workspace artifact.
        self.allow_unsafe_execution = allow_unsafe_execution
        self.timeout_seconds = timeout_seconds

    def verify(
        self,
        submission: Path,
        turn_dir: Path,
        *,
        workspace: Path | None = None,
    ) -> VerifyResult:
        if not isinstance(submission, Path):
            raise TypeError("AIRS AutoResearch requires a Workspace artifact path")
        try:
            accuracy = _native_accuracy(
                turn_dir / "evaluation_assets/test_labels.csv",
                submission,
            )
        except (ValueError, OSError, pd.errors.ParserError) as exc:
            return VerifyResult(
                category="fail",
                score=0.0,
                details={"reason": "invalid_submission", "error": str(exc)},
            )
        return VerifyResult(
            category="pass",
            score=accuracy,
            details={"Accuracy": accuracy, "artifact": str(submission)},
        )


def _native_accuracy(labels_path: Path, submission_path: Path) -> float:
    if not submission_path.is_file():
        raise ValueError("submission.csv was not created")
    labels = pd.read_csv(labels_path)["label"]
    submission = pd.read_csv(submission_path)
    if list(submission.columns) != ["label"]:
        raise ValueError("submission must contain exactly one label column")
    predictions = submission["label"]
    if len(predictions) != len(labels):
        raise ValueError(
            f"submission row count must be {len(labels)}, got {len(predictions)}"
        )
    if predictions.isna().any():
        raise ValueError("submission labels must not contain null values")
    numeric = pd.to_numeric(predictions, errors="coerce")
    if numeric.isna().any() or not numeric.isin([0, 1, 2]).all():
        raise ValueError("submission labels must be integers in the declared set {0,1,2}")
    if not (numeric == numeric.astype(int)).all():
        raise ValueError("submission labels must be integer-valued")
    return float((numeric.astype(int).to_numpy() == labels.to_numpy()).mean())


NativeVerifier = AirsSickAccuracyVerifier
