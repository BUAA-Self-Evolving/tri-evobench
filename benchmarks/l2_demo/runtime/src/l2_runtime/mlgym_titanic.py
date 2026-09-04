"""Package-local MLGym Titanic submission adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from l2_runtime.sra_bigcodebench import _clean_subprocess_environment
from l2_runtime.verification import VerifyResult


class MLGymTitanicVerifier:
    def __init__(self, *, allow_unsafe_execution: bool, timeout_seconds: float = 120.0):
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
        code = _extract_python(raw_model_output)
        started = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="l2-mlgym-titanic-") as temp_dir:
            temp_path = Path(temp_dir)
            execution_workspace = temp_path / "workspace"
            shutil.copytree(source_workspace, execution_workspace)
            script_path = execution_workspace / "train_and_predict.py"
            script_path.write_text(code)
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
                    episode_dir / "evaluation_assets" / "ground_truth.csv",
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
                "accuracy": accuracy,
                "success_threshold": threshold,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )


def _native_accuracy(ground_truth_path: Path, submission_path: Path) -> float:
    if not submission_path.is_file():
        raise ValueError("submission.csv was not created")
    truth = pd.read_csv(ground_truth_path)
    submission = pd.read_csv(submission_path)
    required = ["PassengerId", "Survived"]
    if not all(column in submission.columns for column in required):
        raise ValueError(f"submission must contain columns {required}")
    if len(submission) != len(truth):
        raise ValueError(
            f"submission row count must be {len(truth)}, got {len(submission)}"
        )
    merged = submission.merge(
        truth, on="PassengerId", how="inner", suffixes=("_pred", "_true")
    )
    if merged.empty:
        raise ValueError("no matching PassengerIds")
    return float((merged["Survived_pred"] == merged["Survived_true"]).mean())


def _extract_python(text: str) -> str:
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    for index in range(1, len(parts), 2):
        candidate = parts[index].strip()
        if candidate.startswith("python"):
            candidate = candidate[6:].lstrip("\n")
        if candidate:
            return candidate
    return text.strip()


def _tail(value: str | bytes | None, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value[-limit:]
