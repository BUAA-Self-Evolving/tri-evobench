"""tau2-bench native verifier for the L2 V5 pool (offline trajectory replay).

The submission is ``submission/trajectory.json`` inside the Turn task
directory. The tool calls are executed in order against the upstream tau2
environment for this task and graded with the upstream tau2 evaluator
(see tau2_replay.py). pass = native reward 1.0 over the graded reward basis.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

TRAJECTORY = "submission/trajectory.json"
TAU2_PYTHON = os.environ.get("TAU2_PYTHON", "/opt/tau2/venv/bin/python")
TAU2_DATA_DIR = os.environ.get("TAU2_DATA_DIR", "/opt/tau2/data")


def _fail(reason, **extra):
    details = {"carrier": "tau2_native_replay/1.0", "reason": reason}
    details.update(extra)
    return {"status": "fail", "score": 0.0, "raw_metric": None, "details": details}


def verify(submission, turn_dir):
    assets = Path(turn_dir) / "evaluation_assets"
    meta = json.loads((assets / "tau2_meta.json").read_text(encoding="utf-8"))
    if not isinstance(submission, Path) or not submission.is_dir():
        return _fail("workspace_required")
    trajectory = submission / TRAJECTORY
    if not trajectory.is_file():
        return _fail("missing_trajectory", expected=TRAJECTORY)
    if not Path(TAU2_PYTHON).is_file():
        return _fail("tau2_runtime_missing", tau2_python=TAU2_PYTHON)
    with tempfile.TemporaryDirectory(prefix="tau2_verify_") as scratch:
        scratch_path = Path(scratch)
        copied = scratch_path / "trajectory.json"
        copied.write_bytes(trajectory.read_bytes())
        result_path = scratch_path / "result.json"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": scratch,
               "TAU2_DATA_DIR": TAU2_DATA_DIR, "LOGURU_LEVEL": "ERROR", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            run = subprocess.run(
                [TAU2_PYTHON, str(assets / "tau2_replay.py"), "score", "--task", str(assets / "tau2_task.json"),
                 "--domain", meta["domain"], "--trajectory", str(copied), "--result", str(result_path)],
                cwd=scratch, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
        except subprocess.TimeoutExpired:
            return _fail("replay_timeout")
        if not result_path.is_file():
            return _fail("replay_error", exit_code=run.returncode, output_tail=run.stdout[-4000:])
        result = json.loads(result_path.read_text(encoding="utf-8"))
    result.setdefault("details", {})["source_id"] = meta["source_id"]
    return result
