"""tau2-bench native verifier for live L2 V5 Turns.

The submission is the sealed record of the Turn's live τ² session
(``trievo_tau2_live_session/1.0``), passed inline by the L2 runtime. The
record's task, revision and harness must match this Turn; then
``tau2_scoring.py`` re-runs the official
``evaluate_simulation(EvaluationType.ALL)`` on the recorded simulation in the
τ² interpreter, with the recorded NL-assertion judge result substituted (the
verifier has no network). The re-scored reward must equal the recorded one.
pass = official reward 1.0.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

TAU2_PYTHON = os.environ.get("TAU2_PYTHON", "/opt/tau2/venv/bin/python")
TAU2_DATA_DIR = os.environ.get("TAU2_DATA_DIR", "/opt/tau2/data")


def _fail(reason, **extra):
    details = {"carrier": "trievo_tau2_live_session/1.0", "reason": reason}
    details.update(extra)
    return {"status": "fail", "score": 0.0, "raw_metric": None, "details": details}


def verify(submission, turn_dir):
    assets = Path(turn_dir) / "evaluation_assets"
    if not isinstance(submission, str) or not submission.strip():
        return _fail("empty_submission")
    try:
        json.loads(submission)
    except json.JSONDecodeError:
        return _fail("submission_is_not_a_session_record")
    if not Path(TAU2_PYTHON).is_file():
        raise RuntimeError(f"tau2 interpreter missing: {TAU2_PYTHON}")
    with tempfile.TemporaryDirectory(prefix="tau2_verify_") as scratch:
        record_path = Path(scratch) / "record.json"
        record_path.write_text(submission, encoding="utf-8")
        result_path = Path(scratch) / "result.json"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": scratch, "TAU2_DATA_DIR": TAU2_DATA_DIR,
               "LOGURU_LEVEL": "ERROR", "PYTHONDONTWRITEBYTECODE": "1"}
        run = subprocess.run(
            [TAU2_PYTHON, str(assets / "tau2_scoring.py"), "--record", str(record_path),
             "--task", str(assets / "tau2_task.json"), "--meta", str(assets / "tau2_meta.json"),
             "--result", str(result_path)],
            cwd=scratch, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
        if not result_path.is_file():
            raise RuntimeError(f"tau2 re-score failed (exit {run.returncode}): {run.stdout[-3000:]}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
    if "error" in result:
        # A record that does not belong to this Turn, or a session that failed
        # for infrastructure reasons, is an evaluation error, not a task score.
        raise RuntimeError(result["error"])
    meta = json.loads((assets / "tau2_meta.json").read_text(encoding="utf-8"))
    result.setdefault("details", {})["source_id"] = meta["source_id"]
    return result
