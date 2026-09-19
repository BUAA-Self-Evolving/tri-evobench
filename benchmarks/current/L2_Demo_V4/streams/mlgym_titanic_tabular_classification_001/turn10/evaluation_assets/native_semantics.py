"""Package-local MLGym Titanic Workspace-artifact verifier."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from verification import VerifyResult


class MLGymTitanicVerifier:
    def __init__(self, *, allow_unsafe_execution: bool, timeout_seconds: float = 120.0):
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
            raise TypeError("MLGym AutoResearch requires a Workspace artifact path")
        try:
            accuracy = _native_accuracy(
                turn_dir / "evaluation_assets" / "ground_truth.csv",
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


def _native_accuracy(ground_truth_path: Path, submission_path: Path) -> float:
    if not submission_path.is_file():
        raise ValueError("submission.csv was not created")
    truth = pd.read_csv(ground_truth_path)
    submission = pd.read_csv(submission_path)
    required = ["PassengerId", "Survived"]
    if list(submission.columns) != required:
        raise ValueError(f"submission must contain exactly the columns {required}")
    if len(submission) != len(truth):
        raise ValueError(
            f"submission row count must be {len(truth)}, got {len(submission)}"
        )
    if submission["PassengerId"].isna().any() or submission["PassengerId"].duplicated().any():
        raise ValueError("PassengerId values must be non-null and unique")
    if set(submission["PassengerId"]) != set(truth["PassengerId"]):
        raise ValueError("submission PassengerIds must exactly match the hidden cohort")
    survived = pd.to_numeric(submission["Survived"], errors="coerce")
    if survived.isna().any() or not survived.isin([0, 1]).all():
        raise ValueError("Survived predictions must be integers in the declared set {0,1}")
    if not (survived == survived.astype(int)).all():
        raise ValueError("Survived predictions must be integer-valued")
    normalized_submission = submission.copy()
    normalized_submission["Survived"] = survived.astype(int)
    merged = normalized_submission.merge(
        truth, on="PassengerId", how="inner", suffixes=("_pred", "_true"), validate="one_to_one"
    )
    if len(merged) != len(truth):
        raise ValueError("submission does not cover the hidden cohort exactly once")
    return float((merged["Survived_pred"] == merged["Survived_true"]).mean())


NativeVerifier = MLGymTitanicVerifier
