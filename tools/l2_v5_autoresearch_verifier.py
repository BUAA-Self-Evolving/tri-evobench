"""Source verifier for one AutoResearch task in the rebuilt L2 V5 pool.

This file is copied unchanged into every AutoResearch pool task. The task's
``evaluation_assets/task_config.json`` selects the runner, the native metric,
and the upstream normalization. Each runner executes the upstream evaluation
script (AIRS-Bench ``evaluate.py`` or MLGym ``evaluate.py``) in a scratch
directory exactly the way the upstream harness invokes it, then parses the
native metric from its stdout. Agent-visible copies of evaluation scripts are
never trusted: the pristine upstream copies under ``evaluation_assets/upstream``
are used.

Score = AIRS-Bench normalized score, clipped to [0, 1]:
    NS = (phi(s) - phi(s_worst)) / (phi(s_ref) - phi(s_worst)),
    phi(s) = -log10(|s - s_opt|)                      (method "airs_log")
or the linear variant (s - s_worst) / (s_ref - s_worst) ("airs_linear").
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_AIRS_RESULT = re.compile(r"--- EVALUATION RESULT ---\s*(\{[\s\S]*?\})", re.DOTALL)


class InvalidSubmission(Exception):
    pass


def verify(submission: Any, turn_dir: Any) -> dict[str, Any]:
    assets = Path(turn_dir) / "evaluation_assets"
    cfg = json.loads((assets / "task_config.json").read_text(encoding="utf-8"))
    if submission is None or isinstance(submission, str):
        return _fail("workspace artifact required; final-response submissions are not scored", cfg)
    submission = Path(submission)
    try:
        raw, details = RUNNERS[cfg["kind"]](submission, assets, cfg)
    except InvalidSubmission as exc:
        return _fail(str(exc), cfg)
    except subprocess.TimeoutExpired:
        return _fail(f"native evaluation exceeded {cfg['verify_timeout_seconds']} s", cfg)
    if raw is None or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        return _fail(f"native metric is not a finite number: {raw!r}", cfg, details)
    raw = float(raw)
    score = normalize(raw, cfg["normalization"])
    details.update({"metric_name": cfg["metric_name"], "normalization": cfg["normalization"]})
    return {"status": "pass", "score": score, "raw_metric": raw, "details": details}


def normalize(raw: float, norm: dict[str, Any]) -> float:
    worst, ref = float(norm["worst"]), float(norm["reference"])
    if norm["method"] == "airs_log":
        opt = float(norm["optimal"])
        if raw == opt:
            return 1.0

        def phi(value: float) -> float:
            return -math.log10(abs(value - opt))

        value = (phi(raw) - phi(worst)) / (phi(ref) - phi(worst))
    elif norm["method"] == "airs_linear":
        value = (raw - worst) / (ref - worst)
    else:
        raise ValueError(f"unknown normalization method {norm['method']}")
    if not math.isfinite(value):
        value = 0.0
    return min(1.0, max(0.0, value))


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _artifact(submission: Path, relative: str) -> Path:
    path = submission / relative if submission.is_dir() else submission
    if not path.exists():
        raise InvalidSubmission(f"required artifact {relative} was not produced")
    return path


def _run(cmd: list[str], cwd: Path, cfg: dict[str, Any], assets: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HF_DATASETS_OFFLINE="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    shim = assets / "shim"
    if shim.is_dir():  # sitecustomize: datasets caching off (evaluation assets may be read-only)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(shim.resolve()), env.get("PYTHONPATH")) if p)
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True,
                          timeout=cfg["verify_timeout_seconds"])


def _tail(text: str, limit: int = 1500) -> str:
    return text[-limit:] if text else ""


def _metric(result: dict[str, Any], cfg: dict[str, Any]) -> float:
    if cfg["metric_key"] not in result:
        raise InvalidSubmission(f"native result lacks {cfg['metric_key']}: {result}")
    return result[cfg["metric_key"]]


def _first_json_line(stdout: str) -> dict[str, Any]:
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    raise InvalidSubmission("native evaluation printed no JSON metrics")


def run_airs_csv(submission: Path, assets: Path, cfg: dict[str, Any]):
    """AIRS-Bench: evaluate_prepare output mounted at ./data, submission at ./data/submission.csv."""
    artifact = _artifact(submission, cfg["relative_path"])
    with tempfile.TemporaryDirectory(prefix="l2v5-airs-") as temp:
        work = Path(temp)
        data = work / "data"
        data.mkdir()
        for item in (assets / "data").iterdir():
            (data / item.name).symlink_to(item.resolve())
        shutil.copy2(artifact, data / "submission.csv")
        proc = _run([sys.executable, str(assets / "upstream" / "evaluate.py"),
                     "--submission-file", "./data/submission.csv"], work, cfg, assets)
    details = {"returncode": proc.returncode, "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr)}
    match = _AIRS_RESULT.search(proc.stdout or "")
    if proc.returncode != 0 or not match:
        raise InvalidSubmission(f"native evaluate.py rejected the submission (rc={proc.returncode}): "
                                f"{_tail(proc.stderr, 600)}")
    result = json.loads(match.group(1))
    details["native_result"] = result
    return _metric(result, cfg), details


def _materialize_eval_script(assets: Path, work: Path, cfg: dict[str, Any]) -> Path:
    source = (assets / "upstream" / "evaluate.py").read_text(encoding="utf-8")
    for old, new in cfg.get("eval_substitutions", []):
        new = new.replace("{assets}", str(assets.resolve())).replace("{work}", str(work))
        if old not in source:
            raise RuntimeError(f"path mapping target not found in upstream evaluate.py: {old}")
        source = source.replace(old, new)
    target = work / "evaluate.py"
    target.write_text(source, encoding="utf-8")
    return target


def run_mlgym_csv(submission: Path, assets: Path, cfg: dict[str, Any]):
    """MLGym CSVSubmissionTasks: `python evaluate.py --submission_file <ws>/submission.csv`."""
    artifact = _artifact(submission, cfg["relative_path"])
    with tempfile.TemporaryDirectory(prefix="l2v5-mlgym-") as temp:
        work = Path(temp)
        shutil.copy2(artifact, work / "submission.csv")
        (work / "data").mkdir()
        for item in (assets / "data").iterdir():
            (work / "data" / item.name).symlink_to(item.resolve())
        script = _materialize_eval_script(assets, work, cfg)
        proc = _run([sys.executable, str(script), "--submission_file", str(work / "submission.csv")], work, cfg, assets)
    details = {"returncode": proc.returncode, "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr)}
    if proc.returncode != 0:
        raise InvalidSubmission(f"native evaluate.py failed (rc={proc.returncode}): {_tail(proc.stderr, 600)}")
    result = _first_json_line(proc.stdout)
    details["native_result"] = result
    return _metric(result, cfg), details


def _copy_workspace(submission: Path, work: Path, required: str) -> None:
    if submission.is_dir():
        shutil.copytree(submission, work, dirs_exist_ok=True, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__"))
    elif submission.is_file():
        shutil.copy2(submission, work / required)
    if not (work / required).exists():
        raise InvalidSubmission(f"required artifact {required} was not produced")


def run_mlgym_python(submission: Path, assets: Path, cfg: dict[str, Any]):
    """MLGym PythonSubmissionTasks: `python evaluate.py` in the task workspace."""
    with tempfile.TemporaryDirectory(prefix="l2v5-mlgym-") as temp:
        work = Path(temp)
        _copy_workspace(submission, work, cfg["relative_path"])
        for name in cfg["pristine_files"]:
            shutil.copy2(assets / "upstream" / name, work / name)
        proc = _run([sys.executable, "evaluate.py"], work, cfg, assets)
    details = {"returncode": proc.returncode, "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr)}
    if proc.returncode != 0:
        raise InvalidSubmission(f"native evaluate.py failed (rc={proc.returncode}): {_tail(proc.stderr, 600)}")
    result = _first_json_line(proc.stdout)
    details["native_result"] = result
    return _metric(result, cfg), details


def run_mlgym_model(submission: Path, assets: Path, cfg: dict[str, Any]):
    """MLGym ModelSubmissionTasks: `python evaluate.py --config_fname <first *.yaml>`."""
    if not submission.is_dir():
        raise InvalidSubmission("model submission must be the task workspace directory")
    with tempfile.TemporaryDirectory(prefix="l2v5-mlgym-") as temp:
        work = Path(temp)
        _copy_workspace(submission, work, cfg["relative_path"])
        for name in cfg["pristine_files"]:
            shutil.copy2(assets / "upstream" / name, work / name)
        configs = sorted(str(p.relative_to(work)) for p in work.rglob("*.yaml"))
        if not configs:
            raise InvalidSubmission("no *.yaml config found in the workspace")
        config = "src/config.yaml" if "src/config.yaml" in configs else configs[0]
        proc = _run([sys.executable, "evaluate.py", "--config_fname", config], work, cfg, assets)
    details = {"returncode": proc.returncode, "config_fname": config,
               "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr)}
    if proc.returncode != 0:
        raise InvalidSubmission(f"native evaluate.py failed (rc={proc.returncode}): {_tail(proc.stderr, 600)}")
    result = _first_json_line(proc.stdout)
    details["native_result"] = result
    return _metric(result, cfg), details


def run_mlgym_script(submission: Path, assets: Path, cfg: dict[str, Any]):
    """MLGym task whose evaluate.py loads a submitted artifact from the workspace (e.g. a saved model)."""
    artifact = _artifact(submission, cfg["relative_path"])
    with tempfile.TemporaryDirectory(prefix="l2v5-mlgym-") as temp:
        work = Path(temp)
        (work / cfg["relative_path"]).symlink_to(artifact.resolve())
        script = _materialize_eval_script(assets, work, cfg)
        proc = _run([sys.executable, str(script), *cfg.get("eval_args", [])], work, cfg, assets)
    details = {"returncode": proc.returncode, "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr)}
    if proc.returncode != 0:
        raise InvalidSubmission(f"native evaluate.py failed (rc={proc.returncode}): {_tail(proc.stderr, 600)}")
    result = _first_json_line(proc.stdout)
    details["native_result"] = result
    return _metric(result, cfg), details


RUNNERS = {
    "mlgym_script": run_mlgym_script,
    "airs_csv": run_airs_csv,
    "mlgym_csv": run_mlgym_csv,
    "mlgym_python": run_mlgym_python,
    "mlgym_model": run_mlgym_model,
}


def _fail(reason: str, cfg: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(details or {})
    payload.update({"reason": reason, "metric_name": cfg.get("metric_name")})
    return {"status": "fail", "score": 0.0, "raw_metric": None, "details": payload}
