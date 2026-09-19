"""Run the native oracle through the package-local verifier, then break it."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from verifier import FileArtifactVerifier


HERE = Path(__file__).resolve().parent
EPISODE_DIR = HERE.parent
STREAM_DIR = EPISODE_DIR.parents[1]


def _episode_data_dir() -> Path:
    episode = __import__("json").loads((EPISODE_DIR / "task.json").read_text())
    for asset in episode["agent_input"]["assets"]:
        if "stock-descriptions" in asset:
            relative = asset.removeprefix("workspace/")
            return (STREAM_DIR / "workspace" / relative).parent
    raise RuntimeError("episode has no stock-description data asset")


def build_known_correct(root: Path) -> Path:
    output = root / "output"
    npm_temp = root / "npm"
    npm_temp.mkdir()
    source_data = _episode_data_dir()
    script = (HERE / "oracle_solution.sh").read_text()
    script = script.replace("/tmp", str(npm_temp))
    script = script.replace("/root/output", str(output))
    script = script.replace("/root/data", str(source_data))
    script = script.replace("cd /root", f"cd {root}")
    adapted = root / "oracle_solution_adapted.sh"
    adapted.write_text(script)
    subprocess.run(
        ["bash", str(adapted)],
        cwd=root,
        env={"HOME": str(root), "LANG": os.environ.get("LANG", "C.UTF-8"), "PATH": os.environ.get("PATH", os.defpath)},
        check=True,
        timeout=180,
    )
    return output


def main() -> None:
    verifier = FileArtifactVerifier(allow_unsafe_execution=True)
    with tempfile.TemporaryDirectory(prefix="l2-file-gold-") as temp:
        correct = build_known_correct(Path(temp))
        good = verifier.verify(correct, EPISODE_DIR)
    with tempfile.TemporaryDirectory(prefix="l2-file-broken-") as temp:
        broken = Path(temp) / "output"
        broken.mkdir()
        bad = verifier.verify(broken, EPISODE_DIR)
    assert (good.category, good.score) == ("pass", 1.0), good.details
    assert (bad.category, bad.score) == ("fail", 0.0), bad.details
    print(f"known-correct: {good.category} ({good.score})")
    print(f"deliberately-broken: {bad.category} ({bad.score})")


if __name__ == "__main__":
    main()
