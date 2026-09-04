"""Package-local adapter for the materialized native browser verifier.

The verifier executes submission JavaScript in Chromium. It is deterministic
for a fixed artifact, but it is not a security sandbox and therefore requires
an explicit opt-in.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerifyResult:
    """Field-compatible with the pilot SRA VerifyResult."""

    category: str
    score: float
    details: dict[str, object]


class FileArtifactVerifier:
    def __init__(self, *, allow_unsafe_execution: bool, timeout_seconds: float = 300.0) -> None:
        self.allow_unsafe_execution = allow_unsafe_execution
        self.timeout_seconds = timeout_seconds

    def verify(self, submission_dir: Path, episode_dir: Path) -> VerifyResult:
        if not self.allow_unsafe_execution:
            raise RuntimeError("browser execution is disabled; pass the explicit unsafe opt-in")
        submission_dir = submission_dir.resolve()
        assets = episode_dir.resolve() / "evaluation_assets"
        native_test = assets / "native_test_outputs.py"
        started = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="l2-file-verifier-") as temp:
            temp_dir = Path(temp)
            adapted_test = temp_dir / "test_outputs.py"
            test_text = native_test.read_text()
            test_text = test_text.replace(
                'OUTPUT_DIR = "/root/output"',
                'OUTPUT_DIR = os.environ["L2_SUBMISSION_DIR"]',
            )
            test_text = test_text.replace(
                'verifier_dir = "/logs/verifier"',
                'verifier_dir = os.environ["L2_VERIFIER_LOG_DIR"]',
            )
            test_text = test_text.replace(
                "PORT = 8765",
                'PORT = int(os.environ["L2_VERIFIER_PORT"])',
            )
            adapted_test.write_text(test_text)
            env = {
                "HOME": str(temp_dir),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "L2_SUBMISSION_DIR": str(submission_dir),
                "L2_VERIFIER_LOG_DIR": str(temp_dir / "logs"),
                "L2_VERIFIER_PORT": str(_available_port()),
                "PATH": os.environ.get("PATH", os.defpath),
                "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                    "PLAYWRIGHT_BROWSERS_PATH",
                    str(Path.home() / ".cache" / "ms-playwright"),
                ),
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": str(temp_dir),
            }
            try:
                process = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", str(adapted_test)],
                    cwd=temp_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return VerifyResult(
                    category="timeout",
                    score=0.0,
                    details={"duration_ms": round((time.monotonic() - started) * 1000), "stdout_tail": _tail(exc.stdout), "stderr_tail": _tail(exc.stderr)},
                )

        passed = process.returncode == 0
        return VerifyResult(
            category="pass" if passed else "fail",
            score=1.0 if passed else 0.0,
            details={"returncode": process.returncode, "duration_ms": round((time.monotonic() - started) * 1000), "stdout_tail": _tail(process.stdout), "stderr_tail": _tail(process.stderr)},
        )


def _tail(value: str | bytes | None, limit: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value[-limit:]


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


NativeVerifier = FileArtifactVerifier
