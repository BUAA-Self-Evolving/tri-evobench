"""Package-local adapter for materialized SRA BigCodeBench Episodes.

This is an intentionally unsafe local execution adapter for the current demo.
It runs generated Python in a subprocess and temporary directory, but it is
not a security sandbox. Callers must opt in explicitly.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from l2_runtime.sra_sanitize import sanitize
from l2_runtime.verification import VerifyResult


_TEST_HARNESS = r'''
_l2_loader = unittest.TestLoader()
_l2_suite = _l2_loader.loadTestsFromTestCase(TestCases)
_l2_result = unittest.TextTestRunner(verbosity=1).run(_l2_suite)
raise SystemExit(0 if _l2_result.wasSuccessful() else 1)
'''


class SraBigCodeBenchVerifier:
    def __init__(
        self,
        *,
        allow_unsafe_execution: bool,
        timeout_seconds: float = 120.0,
    ) -> None:
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
            raise RuntimeError(
                "generated-code execution is disabled; pass the explicit unsafe opt-in"
            )
        evaluation = json.loads((episode_dir / "evaluation.json").read_text())
        assets = episode_dir / "evaluation_assets"
        item = json.loads((assets / "source_item.json").read_text())
        eval_data = item["eval_data"]
        entrypoint = eval_data["entry_point"]
        extracted = sanitize(raw_model_output, entrypoint=entrypoint)
        solution = eval_data["code_prompt"] + "\n    pass\n" + extracted
        test_code = (assets / "tests.py").read_text()
        script = solution + "\n" + test_code + "\n" + _TEST_HARNESS
        limits = evaluation["verifier"]["resource_limits_from_native"]

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="l2-bcb-") as temp_dir:
            script_path = Path(temp_dir) / "evaluate_submission.py"
            script_path.write_text(script)
            env = _clean_subprocess_environment(Path(temp_dir))
            try:
                process = subprocess.run(
                    [sys.executable, str(script_path)],
                    cwd=temp_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    preexec_fn=lambda: _apply_resource_limits(limits),
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

        category = "pass" if process.returncode == 0 else "fail"
        return VerifyResult(
            category=category,
            score=1.0 if category == "pass" else 0.0,
            details={
                "returncode": process.returncode,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "stdout_tail": _tail(process.stdout),
                "stderr_tail": _tail(process.stderr),
            },
        )


def _apply_resource_limits(limits: dict[str, int]) -> None:
    address_space = limits["max_address_space_mb"] * 1024 * 1024
    data = limits["max_data_mb"] * 1024 * 1024
    stack = limits["max_stack_mb"] * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_DATA, (data, data))
    resource.setrlimit(resource.RLIMIT_STACK, (stack, stack))


def _clean_subprocess_environment(temp_dir: Path) -> dict[str, str]:
    """Build a minimal environment that cannot inherit model/API credentials."""

    return {
        "HOME": str(temp_dir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(temp_dir / ".matplotlib"),
        "OMP_NUM_THREADS": "1",
        "PATH": os.environ.get("PATH", os.defpath),
        "TF_CPP_MIN_LOG_LEVEL": "3",
        "TMPDIR": str(temp_dir),
    }


def _tail(value: str | bytes | None, limit: int = 8000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value[-limit:]
