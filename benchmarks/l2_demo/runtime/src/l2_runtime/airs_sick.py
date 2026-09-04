"""Package-local AIRS SICK textual-entailment adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from l2_runtime.mlgym_titanic import _extract_python, _tail
from l2_runtime.sra_bigcodebench import _clean_subprocess_environment
from l2_runtime.verification import VerifyResult


class AirsSickAccuracyVerifier:
    def __init__(self, *, allow_unsafe_execution: bool, timeout_seconds: float = 180.0):
        self.allow_unsafe_execution = allow_unsafe_execution
        self.timeout_seconds = timeout_seconds

    def verify(
        self,
        raw_model_output: str,
        episode_dir: Path,
        *,
        workspace: Path | None = None,
    ) -> VerifyResult:
        if not self.allow_unsafe_execution:
            raise RuntimeError("generated-code execution requires explicit unsafe opt-in")
        evaluation = json.loads((episode_dir / "evaluation.json").read_text())
        threshold = float(evaluation["scoring"]["success_threshold"])
        stream_dir = episode_dir.parents[1]
        source_workspace = workspace or (stream_dir / "workspace")
        started = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="l2-airs-sick-") as temp_dir:
            temp_path = Path(temp_dir)
            execution_workspace = temp_path / "workspace"
            shutil.copytree(source_workspace, execution_workspace)
            script_path = execution_workspace / "train_and_predict.py"
            script_path.write_text(_extract_python(raw_model_output))
            try:
                process = subprocess.run(
                    [sys.executable, str(script_path)],
                    cwd=execution_workspace,
                    env=_clean_subprocess_environment(temp_path),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return VerifyResult(
                    category="timeout",
                    score=0.0,
                    details={
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "stdout_tail": _tail(exc.stdout),
                        "stderr_tail": _tail(exc.stderr),
                    },
                )
            if process.returncode != 0:
                return VerifyResult(
                    category="fail",
                    score=0.0,
                    details={
                        "reason": "submission_script_failed",
                        "returncode": process.returncode,
                        "stdout_tail": _tail(process.stdout),
                        "stderr_tail": _tail(process.stderr),
                    },
                )
            try:
                accuracy = _native_accuracy(
                    episode_dir / "evaluation_assets/test_labels.csv",
                    execution_workspace / "submission.csv",
                )
            except (ValueError, OSError, pd.errors.ParserError) as exc:
                return VerifyResult(
                    category="fail",
                    score=0.0,
                    details={"reason": "invalid_submission", "error": str(exc)},
                )

        return VerifyResult(
            category="pass" if accuracy >= threshold else "fail",
            score=accuracy,
            details={
                "Accuracy": accuracy,
                "success_threshold": threshold,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
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
    return float((predictions.to_numpy() == labels.to_numpy()).mean())
