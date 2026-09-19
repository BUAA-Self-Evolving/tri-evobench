#!/usr/bin/env python3
"""Build the package-local oracle, then prove the native verifier goes red."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from native_semantics import NativeVerifier


HERE = Path(__file__).resolve().parent
TURN_DIR = HERE.parent


def build_known_correct(root: Path) -> Path:
    output = root / "output"
    npm_temp = root / "npm"
    npm_temp.mkdir()
    script = (HERE / "oracle_solution.sh").read_text(encoding="utf-8")
    script = script.replace("/tmp", str(npm_temp))
    script = script.replace("/root/output", str(output))
    script = script.replace("/root/data", str(TURN_DIR / "assets" / "data"))
    script = script.replace("cd /root", f"cd {root}")
    adapted = root / "oracle_solution_adapted.sh"
    adapted.write_text(script, encoding="utf-8")
    subprocess.run(
        ["bash", str(adapted)],
        cwd=root,
        env={"HOME": str(root), "LANG": os.environ.get("LANG", "C.UTF-8"), "PATH": os.environ.get("PATH", os.defpath)},
        check=True,
        timeout=240,
    )
    return output


def main() -> None:
    verifier = NativeVerifier(allow_unsafe_execution=True)
    with tempfile.TemporaryDirectory(prefix="l2-file-gold-") as temp:
        good = verifier.verify(build_known_correct(Path(temp)), TURN_DIR)
    with tempfile.TemporaryDirectory(prefix="l2-file-broken-") as temp:
        broken = Path(temp) / "output"
        broken.mkdir()
        bad = verifier.verify(broken, TURN_DIR)
    assert (good.category, good.score) == ("pass", 1.0), good.details
    assert (bad.category, bad.score) == ("fail", 0.0), bad.details
    print("PASS: file-artifact native verifier known-correct/known-broken")


if __name__ == "__main__":
    main()
