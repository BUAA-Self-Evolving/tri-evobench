#!/usr/bin/env python3
"""Build and qualify AutoResearch pool tasks (AIRS-Bench rad + MLGym) for L2 V5.

Every pool task is one distinct native upstream task with its full native test
set and native metric. Nothing is split into folds or cohorts.

* ``prepare`` runs the upstream data preparation inside the declared CPU task
  image with the network disabled: AIRS ``prepare.py`` (agent-visible data)
  and ``evaluate_prepare.py`` (private test labels); MLGym workspace data plus
  starter code with the gold files moved to ``evaluation_assets``. It also
  creates fixtures: ``known_correct`` (a valid upstream-format submission),
  ``known_broken`` (a malformed submission), ``sanity_gold`` (the gold labels,
  where they exist) and ``sanity_strong`` (games). For AIRS, ``known_correct`` is the heavily
  row-switched gold submission and doubles as the band anchor.
* ``qualify`` copies ``evaluation_assets`` and ``fixtures`` of each task into a
  fresh scratch directory and runs ``source_verifier.verify`` on every fixture
  in the same image with ``--network none``. Results come back through a
  bind-mounted output directory. Performance bands are derived here (see
  ``derive_bands``) because they use the shuffled-gold anchor.
* ``index`` writes ``INDEX.jsonl`` (one line per candidate, admitted or not).

Third-party checkouts are read-only inputs. Docker bind mounts only use fresh
``mkdtemp`` directories (rootless Docker on PHAI keeps stale inodes for
deleted-and-recreated paths).
"""

from __future__ import annotations

import argparse
import csv
import os
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
AIRS_ROOT = (REPO_ROOT / "third_party" / "src" / "airs-bench").resolve()
MLGYM_ROOT = (REPO_ROOT / "third_party" / "src" / "mlgym").resolve()
AIRS_REV = "18e4f1d5"
MLGYM_REV = "9d40c1b5"
DATA_ROOT = Path("/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_autoresearch")
POOL_ROOT = Path("/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_pool/autoresearch")
SCRATCH = Path("/data_storage/yl_test/lgx/data-1/tmp/l2v5")
CPU_IMAGE = "trievo-l2-v5-ar-cpu:0.1"
GPU_IMAGE = "trievo-l2-v5-ar-gpu:0.1"
VERIFIER_TEMPLATE = Path(__file__).with_name("l2_v5_autoresearch_verifier.py")
SCHEMA = "l2_v5_pool_task/1.0"
# PHAI: the default DOCKER_HOST is a proxy that drops attached stdout and fakes `docker exec` exit
# codes, but it is the only endpoint that can bind-mount /data_storage. All runs here therefore use
# the default endpoint and read results exclusively from files in a bind-mounted /out directory
# (including the exit code). L2V5_DOCKER_HOST overrides the endpoint if needed.
DOCKER_ENV = dict(os.environ)
if os.environ.get("L2V5_DOCKER_HOST"):
    DOCKER_ENV["DOCKER_HOST"] = os.environ["L2V5_DOCKER_HOST"]
PARTIAL_FRACTION = 0.5
FULL_FRACTION = 0.8

AIRS_LICENSE = ("AIRS-Bench task code: CC BY-NC 4.0 (facebookresearch/airs-bench). Data is the upstream "
                "Hugging Face dataset named in benchmark_native_labels.dataset under its own license; "
                "materialized only for non-commercial research use.")
MLGYM_LICENSE = ("MLGym task code: CC BY-NC 4.0 (facebookresearch/MLGym LICENSE). Bundled data keeps its "
                 "original license (see benchmark_native_labels.dataset).")

CAP = {
    "text": ("ar.tabular_and_text_prediction", "classification/regression/QA on tabular or natural-language data"),
    "seq": ("ar.sequence_and_math_reasoning", "time-series forecasting, math word problems, code retrieval/generation"),
    "game": ("ar.decision_and_game_strategy", "game-theoretic strategies and reinforcement learning policies"),
    "deep": ("ar.vision_and_deep_models", "image, molecular and graph models that need deep-network training"),
}

# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

_GPU_AIRS_NOTE = ("AIRS assumes one GPU per agent; competitive solutions need pretrained-transformer "
                  "fine-tuning or deep models, so the task is GPU-class. The verifier itself is CPU-only.")


def _airs(task: str, cap: str, types: list[str], gpu: bool, why: str, exclude: str | None = None) -> dict[str, Any]:
    return {"source": "airs", "task": task, "kind": "airs_csv", "cap": cap, "task_types": types,
            "requires_gpu": gpu, "rationale": why, "exclude": exclude}


CANDIDATES: list[dict[str, Any]] = [
    # --- AIRS-Bench, CPU-class: small/medium data where a CPU-only pipeline trained from scratch
    #     produces a valid and improvable submission inside the 3600 s budget.
    _airs("TextualClassificationSickAccuracy", "text", ["textual_entailment", "text_classification"], False,
          "SICK NLI, 4.4k train pairs; feature/linear models train on CPU in seconds."),
    _airs("TextualSimilaritySickSpearmanCorrelation", "text", ["semantic_similarity", "regression"], False,
          "SICK relatedness regression, 4.4k pairs; CPU feature models suffice for valid submissions."),
    _airs("SentimentAnalysisYelpReviewFullAccuracy", "text", ["sentiment_classification", "text_classification"], False,
          "650k reviews, 5 classes; sparse linear models train on CPU within minutes."),
    _airs("CodeRetrievalCodeXGlueMRR", "seq", ["code_retrieval", "ranking"], False,
          "NL-to-code retrieval scored by MRR; lexical retrieval (TF-IDF/BM25) runs on CPU."),
    _airs("TimeSeriesForecastingKaggleWebTrafficMASE", "seq", ["time_series_forecasting"], False,
          "Univariate forecasting, statistical/GBM methods run on CPU.",
          exclude="qualified in run 1 but not admitted: upstream normalization is degenerate "
                  "(estimated_worst_score = 5.03e14), the row-switched gold submission already scores MASE 2.2e-5 "
                  "-> NS = 1.0, so no band can separate a trivial from a strong submission; also 5.7 GB "
                  "materialized (>2 GB) and 6.5 min per native evaluation"),
    _airs("TimeSeriesForecastingRideshareMAE", "seq", ["time_series_forecasting"], False,
          "Hourly rideshare series, statistical/GBM methods run on CPU."),
    _airs("TimeSeriesForecastingSolarWeeklyMAE", "seq", ["time_series_forecasting"], False,
          "Weekly solar series (small), statistical methods run on CPU."),
    # --- AIRS-Bench, GPU-class
    _airs("CodeGenerationAPPSPassAt5", "seq", ["code_generation"], True, _GPU_AIRS_NOTE,
          exclude="upstream verifier imports pyext.RuntimeModule (unmaintained package, not installable for the "
                  "Python 3.11 task image) and executes up to 5x5000 candidate programs against test cases, "
                  "which takes hours on CPU; not built in this round"),
    _airs("CoreferenceResolutionSuperGLUEWSCAccuracy", "text", ["coreference_resolution"], True, _GPU_AIRS_NOTE),
    _airs("CoreferenceResolutionWinograndeAccuracy", "text", ["coreference_resolution"], True, _GPU_AIRS_NOTE),
    _airs("CvMolecularPropertyPredictionQm9MeanAbsoluteError", "deep", ["molecular_property_regression"], True,
          _GPU_AIRS_NOTE),
    _airs("GMolecularPropertyPredictionQm9MeanAbsoluteError", "deep", ["molecular_property_regression"], True,
          _GPU_AIRS_NOTE),
    _airs("R2AbsMolecularPropertyPredictionQm9MeanAbsoluteError", "deep", ["molecular_property_regression"], True,
          _GPU_AIRS_NOTE),
    _airs("U0MolecularPropertyPredictionQm9MeanAbsoluteError", "deep", ["molecular_property_regression"], True,
          _GPU_AIRS_NOTE),
    _airs("GraphRegressionZincMae", "deep", ["graph_regression"], True, _GPU_AIRS_NOTE),
    _airs("MathQuestionAnsweringSVAMPAccuracy", "seq", ["math_word_problems"], True, _GPU_AIRS_NOTE),
    _airs("QuestionAnsweringDuoRCAccuracy", "text", ["reading_comprehension"], True, _GPU_AIRS_NOTE),
    _airs("QuestionAnsweringEli5RougeL", "text", ["long_form_qa", "text_generation"], True, _GPU_AIRS_NOTE),
    _airs("QuestionAnsweringFinqaAccuracy", "seq", ["numerical_reasoning_qa"], True, _GPU_AIRS_NOTE),
    _airs("ReadingComprehensionSquadExactMatch", "text", ["reading_comprehension"], True, _GPU_AIRS_NOTE),
]

# MLGym: normalization anchors. worst = upstream baseline_scores (task config); reference = best score
# over all models in results/tables/best_scores_test_results.csv (MLGym-Bench paper); optimal = the
# metric's attainable optimum.
MLGYM_RESULTS_ROW = {
    "regressionKaggleHousePrice": "House Price", "imageClassificationCifar10": "CIFAR-10",
    "imageClassificationFMnist": "Fashion MNIST", "battleOfSexes": "Battle of Sexes",
    "prisonersDilemma": "Prisoners Dilemma", "blotto": "Blotto", "rlBreakoutMinAtar": "Breakout",
    "rlMetaMaze": "Meta Maze", "rlMountainCarContinuous": "Mountain Car Continuous",
    "naturalLanguageInferenceMNLI": "MNLI",
}


def _mlgym(task: str, kind: str, cap: str, types: list[str], gpu: bool, why: str, *, metric: str,
           optimal: float | None, method: str = "airs_log", data_dir: str | None = None,
           reference: tuple[float, str] | None = None, hf: str | None = None,
           exclude: str | None = None, verify_timeout: int = 1800,
           baseline_key: str | None = None) -> dict[str, Any]:
    return {"source": "mlgym", "task": task, "kind": kind, "cap": cap, "task_types": types,
            "requires_gpu": gpu, "rationale": why, "metric_key": metric, "optimal": optimal,
            "method": method, "data_dir": data_dir or task, "reference_override": reference,
            "hf": hf, "exclude": exclude, "verify_timeout": verify_timeout,
            "baseline_key": baseline_key or metric}


CANDIDATES += [
    _mlgym("titanic", "mlgym_csv", "text", ["tabular_classification"], False,
           "891-row tabular classification; CPU.", metric="accuracy", optimal=1.0, baseline_key="Accuracy",
           reference=(0.80383, "no MLGym-Bench result exists for titanic (added after the paper); "
                               "builder-chosen provisional reference: 336/418 = 0.80383, a commonly reported "
                               "strong non-leaky Kaggle Titanic score"),
           exclude="upstream data/titanic/data/ground_truth.csv is byte-identical to sample_submission.csv and "
                   "agrees 418/418 with the gender rule (female -> survived), i.e. it is Kaggle's "
                   "gender_submission.csv, not the true labels; the native metric rewards reproducing the gender "
                   "heuristic (it scores 1.0) while baseline_scores 0.76555 refers to real labels"),
    _mlgym("regressionKaggleHousePrice", "mlgym_csv", "text", ["tabular_regression"], False,
           "1.4k-row tabular regression; CPU.", metric="r2", optimal=1.0),
    _mlgym("imageClassificationFMnist", "mlgym_csv", "deep", ["image_classification"], False,
           "28x28 grayscale, 60k images; small CNNs/GBMs train on CPU within the 1800 s budget.",
           metric="accuracy", optimal=1.0, hf="zalando-datasets/fashion_mnist"),
    _mlgym("battleOfSexes", "mlgym_python", "game", ["iterated_game_strategy"], False,
           "Pure-Python game simulation; CPU.", metric="Score", optimal=2.0),
    _mlgym("prisonersDilemma", "mlgym_python", "game", ["iterated_game_strategy"], False,
           "Pure-Python game simulation; CPU.", metric="Score", optimal=5.0),
    _mlgym("blotto", "mlgym_python", "game", ["iterated_game_strategy"], False,
           "Pure-Python game simulation; CPU.", metric="Score", optimal=1.0),
    _mlgym("imageClassificationCifar10", "mlgym_csv", "deep", ["image_classification"], True,
           "50k colour images; competitive CNN training needs a GPU (upstream baseline uses CUDA when present).",
           metric="accuracy", optimal=1.0, hf="uoft-cs/cifar10"),
    _mlgym("rlBreakoutMinAtar", "mlgym_model", "game", ["reinforcement_learning"], True,
           "PPO in gymnax/JAX; upstream requirements pin jax[cuda12].", metric="Reward Mean",
           optimal=None, method="airs_linear"),
    _mlgym("rlMetaMaze", "mlgym_model", "game", ["reinforcement_learning"], True,
           "PPO in gymnax/JAX; upstream requirements pin jax[cuda12].", metric="Reward Mean",
           optimal=None, method="airs_linear", data_dir="rlMetaMazeMisc"),
    _mlgym("rlMountainCarContinuous", "mlgym_model", "game", ["reinforcement_learning"], True,
           "PPO in gymnax/JAX; upstream requirements pin jax[cuda12]. Return is bounded above by 100.",
           metric="Reward Mean", optimal=100.0),
    _mlgym("naturalLanguageInferenceMNLI", "mlgym_mnli", "text", ["textual_entailment"], True,
           "BERT fine-tuning on 393k pairs; GPU.", metric="validation_accuracy", optimal=100.0,
           data_dir="bertMNLI", verify_timeout=3600),
    _mlgym("3SATTime", "mlgym_python", "seq", ["heuristic_search"], False, "", metric="Time", optimal=0.0,
           exclude="upstream data/3SATTime/data/dataset.pkl is not in the MLGym repository; metric is wall-clock "
                   "time, which is not comparable across machines"),
    _mlgym("imageCaptioningCOCO", "mlgym_csv", "deep", ["image_captioning"], True, "", metric="BLEU Score",
           optimal=1.0, exclude="COCO data (data/imageCaptioningCOCO/data) is not in the MLGym repository"),
    _mlgym("languageModelingFineWeb", "mlgym_lm", "deep", ["language_modeling"], True, "", metric="val_loss",
           optimal=0.0, exclude="FineWeb token shards (2.4B tokens) are not in the MLGym repository"),
    _mlgym("regressionHousePrice", "mlgym_csv", "text", ["tabular_regression"], False, "", metric="r2",
           optimal=1.0, exclude="dataset config datasets/housePrice.yaml and its data are not in the MLGym repository"),
    _mlgym("regressionHousingPrice", "mlgym_csv", "text", ["tabular_regression"], False, "", metric="r2",
           optimal=1.0, exclude="dataset config datasets/housingPrice.yaml and its data are not in the MLGym repository"),
    _mlgym("regressionKaggleHousePriceL1", "mlgym_csv", "text", ["tabular_regression"], False, "", metric="r2",
           optimal=1.0, exclude="variant of regressionKaggleHousePrice (same data/metric, prompt states the "
                                "baseline); variants are reserved for application turns"),
    _mlgym("imageClassificationCifar10L1", "mlgym_csv", "deep", ["image_classification"], True, "",
           metric="accuracy", optimal=1.0,
           exclude="variant of imageClassificationCifar10 (same id/data/metric); variants are reserved for "
                   "application turns"),
    _mlgym("rlMountainCarContinuousReinforce", "mlgym_model", "game", ["reinforcement_learning"], True, "",
           metric="Reward Mean", optimal=100.0,
           exclude="variant of rlMountainCarContinuous (same environment and metric, REINFORCE starter); "
                   "variants are reserved for application turns"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(paths: list[Path], base: Path) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for root in paths:
        files.extend([root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()])
    for path in sorted(set(files)):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def du_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def benchmark_id(spec: dict[str, Any]) -> str:
    return "airs_bench" if spec["source"] == "airs" else "mlgym"


def source_id(spec: dict[str, Any]) -> str:
    return f"{benchmark_id(spec)}:{spec['task']}"


def task_dir_name(spec: dict[str, Any]) -> str:
    # directory names predate the airs_bench id convention and are kept stable
    prefix = "airs-bench" if spec["source"] == "airs" else "mlgym"
    return f"{prefix}__{spec['task']}".replace("/", "__")


def fresh_dir(prefix: str) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(SCRATCH)))


def image_id(image: str) -> str | None:
    proc = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                          capture_output=True, text=True, env=DOCKER_ENV)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def docker_run(image: str, mounts: list[tuple[Path, str, str]], script: str, timeout: int,
               cpus: int = 16, memory: str = "64g") -> tuple[int, Path]:
    """Run ``script`` (bash) in ``image`` with the network disabled; /out is a fresh bind mount."""
    out_dir = fresh_dir("ar-out-")
    name = f"l2v5-ar-{hashlib.sha256((script + str(time.time())).encode()).hexdigest()[:12]}"
    cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--cpus", str(cpus),
           "--memory", memory, "--pids-limit", "4096", "-e", "HF_DATASETS_OFFLINE=1", "-e", "HF_HUB_OFFLINE=1"]
    for host, container, mode in mounts:
        cmd += ["-v", f"{Path(host).resolve()}:{container}{':' + mode if mode else ''}"]
    cmd += ["-v", f"{out_dir}:/out", "--entrypoint", "/bin/bash", image, "-lc",
            f"( {script} ) > /out/run.log 2>&1; echo $? > /out/exit_code"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=DOCKER_ENV)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=DOCKER_ENV)
        (out_dir / "exit_code").write_text("124\n")
    code_file = out_dir / "exit_code"
    code = int(code_file.read_text().strip()) if code_file.exists() else 125
    return code, out_dir


# ---------------------------------------------------------------------------
# Upstream metadata
# ---------------------------------------------------------------------------


def airs_meta(task: str) -> dict[str, Any]:
    return yaml.safe_load((AIRS_ROOT / "airsbench/tasks/rad" / task / "metadata.yaml").read_text())


def airs_timeout(task: str) -> int:
    cfg = yaml.safe_load((AIRS_ROOT / "airsbench/tasks/mlgym" / task / "configs/tasks" / f"{task}.yaml").read_text())
    return int(cfg["training_timeout"])


def mlgym_cfg(task: str) -> dict[str, Any]:
    return yaml.safe_load((MLGYM_ROOT / "configs/tasks" / f"{task}.yaml").read_text())


def mlgym_reference(task: str) -> tuple[float, str] | None:
    row_name = MLGYM_RESULTS_ROW.get(task)
    if not row_name:
        return None
    with open(MLGYM_ROOT / "results/tables/best_scores_test_results.csv") as handle:
        for row in csv.DictReader(handle):
            if row["Task"] == row_name:
                models = {k: float(v) for k, v in row.items() if k not in ("Task", "baseline") and v}
                best = max(models, key=models.get)
                return models[best], (f"results/tables/best_scores_test_results.csv row '{row_name}': "
                                      f"max over models = {models[best]} ({best})")
    return None


def mlgym_prompt(cfg: dict[str, Any]) -> str:
    docs = ""
    for rel in cfg.get("dataset_configs") or []:
        dataset = yaml.safe_load((MLGYM_ROOT / "configs" / rel).read_text())
        docs += f"{dataset['name'].upper()}:\n{dataset['description']}\n\n"
    return cfg["description"].format(dataset_docs=docs)


def normalization_for(spec: dict[str, Any]) -> dict[str, Any]:
    if spec["source"] == "airs":
        meta = airs_meta(spec["task"])
        info = meta["logging_info"]
        return {"method": "airs_log", "worst": float(info["estimated_worst_score"]),
                "reference": float(info["sota"][0]["sota_score"]), "optimal": float(info["optimal_score"]),
                "source": (f"third_party/src/airs-bench/airsbench/tasks/rad/{spec['task']}/metadata.yaml "
                           "(logging_info.estimated_worst_score, sota[0].sota_score, optimal_score); formula from "
                           "README.md and notebooks/create_summary_plots.ipynb normalize_score_log")}
    cfg = mlgym_cfg(spec["task"])
    baseline = float(cfg["baseline_scores"][0][spec["baseline_key"]])
    ref = spec["reference_override"] or mlgym_reference(spec["task"])
    norm = {"method": spec["method"], "worst": baseline, "reference": float(ref[0]),
            "optimal": spec["optimal"],
            "source": (f"worst = third_party/src/mlgym/configs/tasks/{spec['task']}.yaml baseline_scores"
                       f"[0].{spec['baseline_key']}; reference = {ref[1]}; optimal = attainable optimum of the metric")}
    return norm


# ---------------------------------------------------------------------------
# In-container preparation scripts
# ---------------------------------------------------------------------------

AIRS_PREP = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
import numpy as np, pandas as pd, yaml
from datasets import load_from_disk
TASK, RAW, OUT = Path("/task"), "/raw", Path("/out")
meta = yaml.safe_load((TASK / "metadata.yaml").read_text())
info = meta["logging_info"]
def run(script, *args, cwd=None):
    print("+", script, *args, flush=True)
    subprocess.run([sys.executable, str(TASK / script), *map(str, args)], check=True, cwd=cwd)
# prepare.py writes to container-local /tmp first (post-processing deletes memory-mapped files, which fails on
# the FUSE-backed bind mount), then the result is copied to /out/seed/data
seed = Path("/tmp/agent_data"); seed.mkdir(parents=True)
log = OUT / "agent_log"; log.mkdir()
run("prepare.py", "--global-shared-data-dir", RAW, "--agent-data-mount-dir", seed, "--agent-log-dir", log)
# upstream QuestionAnsweringDuoRCAccuracy/prepare.py keeps the scoring label `no_answer` in the agent test split
# although its prompt says it is not available; it is dropped here (hidden labels stay private)
LEAKS = {"QuestionAnsweringDuoRCAccuracy": ["no_answer"]}
if os.environ.get("TASK") in LEAKS:
    test_dir = seed / "test"
    ds = load_from_disk(str(test_dir)).remove_columns(LEAKS[os.environ["TASK"]])
    tmp = Path("/tmp/test_clean"); ds.save_to_disk(str(tmp)); del ds
    shutil.rmtree(test_dir); shutil.move(str(tmp), str(test_dir))
    print("dropped leaked columns", LEAKS[os.environ["TASK"]])
cols = info["scoring_column"] if isinstance(info["scoring_column"], list) else [info["scoring_column"]]
if (seed / "test").exists():
    try:
        test_cols = load_from_disk(str(seed / "test")).column_names
    except Exception:
        test_cols = []
    leaked = [c for c in cols if c in test_cols]
    assert not leaked, f"scoring column visible in agent test split: {leaked}"
shutil.copytree(seed, OUT / "seed" / "data")
(log / "submission.csv").write_text("placeholder\n")
evm = OUT / "eval_data"; evm.mkdir()
run("evaluate_prepare.py", "--global-shared-data-dir", RAW, "--agent-data-mount-dir", evm, "--agent-log-dir", log)
(evm / "submission.csv").unlink()
gold = OUT / "gold"; gold.mkdir()
if (TASK / "gold_submission.csv").exists():
    for name in ("gold_submission.csv", "gold_submission_permuted_2.csv"):
        if (TASK / name).exists():
            shutil.copy2(TASK / name, gold / name)
    print("gold: upstream gold_submission.csv")
elif (TASK / "custom_labels.py").exists():
    run("custom_labels.py", "--global-shared-data-dir", RAW, "--output-directory", gold, cwd=str(gold))
    print("gold: upstream custom_labels.py")
else:
    ds = load_from_disk(os.path.join(RAW, info["dataset"], info["config"]))[info["test_split"]]
    col = info["scoring_column"]
    values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in ds[col]]
    pd.Series(values).to_csv(gold / "gold_submission.csv", index=False, header=[col])
    print("gold: test split scoring_column", col)
# upstream custom_labels.py of QuestionAnsweringDuoRCAccuracy writes header "has_answer,answers" while the
# prompt and evaluate.py use "has_answer,answer"; fixtures follow the prompt/evaluator.
if os.environ.get("TASK") == "QuestionAnsweringDuoRCAccuracy":
    sub = pd.read_csv(gold / "gold_submission.csv", keep_default_na=False)
    sub.rename(columns={"answers": "answer"}).to_csv(gold / "gold_submission.csv", index=False)
    print("gold: renamed answers -> answer")
if not (gold / "gold_submission_permuted_2.csv").exists():
    # upstream test_task_folder.create_gold_permutations_csvs: switch_rows(gold, len(gold)); seeded here
    np.random.seed(0)
    sub = pd.read_csv(gold / "gold_submission.csv")
    perm = sub.copy()
    for _ in range(len(perm)):
        i, j = np.random.choice(len(perm), 2, replace=False)
        perm.iloc[[i, j]] = perm.iloc[[j, i]].values
    perm.to_csv(gold / "gold_submission_permuted_2.csv", index=False)
print("PREP_OK")
'''

MLGYM_CSV_PREP = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
SRC, OUT, TASK = Path("/src"), Path("/out"), os.environ["TASK"]
seed = OUT / "seed"; seed.mkdir(); evd = OUT / "eval_data"; evd.mkdir(); fx = OUT / "fx"; fx.mkdir()
data_dir = SRC / "data" / os.environ["DATA_DIR"]
starter = json.loads(os.environ["STARTER"])
def copy_starter(dest):
    for rel in starter:
        p = SRC / rel
        (shutil.copytree(p, dest / p.name) if p.is_dir() else shutil.copy2(p, dest / p.name))
if TASK == "titanic":
    (seed / "data").mkdir()
    for name in ("train.csv", "test.csv"):
        shutil.copy2(data_dir / "data" / name, seed / "data" / name)
    shutil.copy2(data_dir / "data" / "ground_truth.csv", evd / "ground_truth.csv")
    copy_starter(seed)
    # upstream sample_submission.csv equals ground_truth.csv, so known_correct is the gender rule
    # (female -> survived), i.e. the Kaggle gender baseline that matches baseline_scores 0.76555
    import pandas as pd
    test = pd.read_csv(data_dir / "data" / "test.csv")
    pd.DataFrame({"PassengerId": test["PassengerId"], "Survived": (test["Sex"] == "female").astype(int)}).to_csv(
        fx / "known_correct.csv", index=False)
    shutil.copy2(data_dir / "data" / "ground_truth.csv", fx / "gold.csv")
elif TASK == "regressionKaggleHousePrice":
    work = Path("/tmp/hp"); shutil.copytree(data_dir, work)
    subprocess.run([sys.executable, "_generate_split.py"], cwd=work / "_helpers", check=True)
    (seed / "data").mkdir()
    for name in ("train.csv", "validation.csv", "test.csv"):
        shutil.copy2(work / "data" / name, seed / "data" / name)
    shutil.copy2(work / "data" / "answer.csv", evd / "answer.csv")
    copy_starter(seed)
    run = Path("/tmp/hp_run"); shutil.copytree(seed, run)
    subprocess.run([sys.executable, "baseline.py"], cwd=run, check=True)
    shutil.copy2(run / "submission.csv", fx / "known_correct.csv")
    shutil.copy2(work / "data" / "answer.csv", fx / "gold.csv")
else:  # HF image datasets (fashion_mnist / cifar10)
    from datasets import DatasetDict, load_from_disk
    import pandas as pd
    raw = load_from_disk("/raw/" + os.environ["HF_NAME"] + "/default")
    test = raw["test"]
    DatasetDict({"train": raw["train"], "test": test.remove_columns(["label"])}).save_to_disk(str(seed / "data"))
    test.save_to_disk(str(evd / "test_with_labels"))
    copy_starter(seed)
    hf = os.environ["HF_NAME"]
    base = (seed / "baseline.py").read_text()
    old = f'load_dataset("{hf}")'
    assert old in base, old
    (seed / "baseline.py").write_text(base.replace(old, 'load_from_disk("./data")').replace(
        "from datasets import load_dataset", "from datasets import load_dataset, load_from_disk"))
    # upstream sample_submission.csv has 9 rows only; known_correct = train majority class for every test row
    majority = pd.Series(raw["train"]["label"]).mode()[0]
    pd.DataFrame({"label": [int(majority)] * len(test)}).to_csv(fx / "known_correct.csv", index=False)
    pd.DataFrame({"label": test["label"]}).to_csv(fx / "gold.csv", index=False)
print("PREP_OK")
'''

MLGYM_RL_PREP = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
SRC, OUT = Path("/src"), Path("/out")
seed = OUT / "seed"; seed.mkdir(); fx = OUT / "fx"; fx.mkdir()
for rel in json.loads(os.environ["STARTER"]):
    p = SRC / rel
    (shutil.copytree(p, seed / p.name) if p.is_dir() else shutil.copy2(p, seed / p.name))
# known_correct: the starter workspace plus 5 randomly initialised checkpoints in the upstream format
# (src/train.py stores {"network": params, ...} as checkpoints/<config stem>-<seed>-<i>.pkl).
kc = fx / "known_correct"; shutil.copytree(seed, kc)
code = """
import jax
from src.helpers import load_config, save_pkl_object
from src.networks import get_model_ready
cfg = load_config("./src/config.yaml")
for i in range(5):
    _, params = get_model_ready(jax.random.PRNGKey(i), cfg.train_config)
    save_pkl_object({"network": params, "log_steps": [], "log_return": [], "train_config": cfg.train_config},
                    f"./checkpoints/config-42-{i}.pkl")
"""
(kc / "checkpoints").mkdir()
subprocess.run([sys.executable, "-c", code], cwd=kc, check=True)
shutil.rmtree(kc / "src" / "__pycache__", ignore_errors=True)
print("PREP_OK")
'''

# datasets.map() writes cache files next to the source dataset; raw data and evaluation assets are
# mounted read-only, so caching is disabled (map output then goes to a temporary directory).
SITECUSTOMIZE = """try:
    import datasets
    datasets.disable_caching()
except Exception:
    pass
"""

MLGYM_MNLI_PREP = r'''
import json, os, shutil
from pathlib import Path
import torch
from datasets import DatasetDict, load_from_disk
from transformers import BertForSequenceClassification
SRC, OUT = Path("/src"), Path("/out")
seed = OUT / "seed"; seed.mkdir(); evd = OUT / "eval_data"; evd.mkdir(); fx = OUT / "fx"; fx.mkdir()
for rel in json.loads(os.environ["STARTER"]):
    shutil.copy2(SRC / rel, seed / Path(rel).name)
raw = load_from_disk("/raw/nyu-mll/multi_nli/default")
def setfit(ds):  # starter baseline reads SetFit/mnli columns text1/text2/label
    keep = {"premise", "hypothesis", "label", "genre"}
    ds = ds.remove_columns([c for c in ds.column_names if c not in keep])
    return ds.rename_columns({"premise": "text1", "hypothesis": "text2"})
DatasetDict({"train": setfit(raw["train"]), "validation": setfit(raw["validation_mismatched"])}).save_to_disk(
    str(seed / "data"))
DatasetDict({"validation_matched": raw["validation_matched"]}).save_to_disk(str(evd / "mnli_eval"))
shutil.copytree("/models/bert-base-uncased", seed / "bert-base-uncased",
                ignore=shutil.ignore_patterns(".cache"))
(evd / "tokenizer").mkdir()
for name in ("vocab.txt", "tokenizer.json", "tokenizer_config.json", "config.json"):
    shutil.copy2(seed / "bert-base-uncased" / name, evd / "tokenizer" / name)
from transformers import BertConfig
torch.manual_seed(0)
# tiny randomly initialised BERT (same vocab/tokenizer) keeps the fixture small; evaluate.py loads any
# BertForSequenceClassification checkpoint saved with save_pretrained
tiny = BertConfig.from_pretrained(str(seed / "bert-base-uncased"), num_labels=3, hidden_size=64, num_hidden_layers=2,
                                  num_attention_heads=2, intermediate_size=128)
BertForSequenceClassification(tiny).save_pretrained(str(fx / "known_correct" / "bert_mnli_model"))
(fx / "known_broken" / "bert_mnli_model").mkdir(parents=True)
shutil.copy2(fx / "known_correct" / "bert_mnli_model" / "config.json", fx / "known_broken" / "bert_mnli_model")
base = (seed / "baseline.py").read_text()
for old, new in [('load_dataset("SetFit/mnli")', 'load_from_disk("./data")'),
                 ("from datasets import load_dataset", "from datasets import load_dataset, load_from_disk"),
                 ("'bert-base-uncased'", "'./bert-base-uncased'"),
                 ("from transformers import AdamW, BertForSequenceClassification, BertTokenizer",
                  "from torch.optim import AdamW\nfrom transformers import BertForSequenceClassification, BertTokenizer")]:
    assert old in base, old
    base = base.replace(old, new)
(seed / "baseline.py").write_text(base)
print("PREP_OK")
'''

STRONG_STRATEGIES = {
    # Direction sanity only: a hand-written strategy that should beat the upstream baseline.
    "battleOfSexes": "def row_strategy(history):\n    return 0\n",
    "prisonersDilemma": "def row_strategy(history):\n    return 1\n",
    "blotto": ("def row_strategy(history):\n    return [30, 30, 30, 30, 0, 0]\n"),
}

# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


def _base_task_config(spec: dict[str, Any], norm: dict[str, Any], relative: str, metric_key: str,
                      metric_name: str, verify_timeout: int) -> dict[str, Any]:
    return {"kind": spec["kind"], "source_id": source_id(spec), "relative_path": relative,
            "metric_key": metric_key, "metric_name": metric_name, "normalization": norm,
            "verify_timeout_seconds": verify_timeout}


def _write_truncated(source: Path, target: Path, rows: int = 10) -> None:
    """known_broken: header plus the first ``rows`` rows (row count no longer matches the test set)."""
    csv.field_size_limit(2**31 - 1)
    with source.open(newline="") as handle, target.open("w", newline="") as out:
        reader, writer = csv.reader(handle), csv.writer(out)
        for index, row in enumerate(reader):
            if index > rows:
                break
            writer.writerow(row)


def prepare_airs(spec: dict[str, Any], stage: Path) -> dict[str, Any]:
    task = spec["task"]
    task_src = AIRS_ROOT / "airsbench/tasks/rad" / task
    meta = airs_meta(task)
    info = meta["logging_info"]
    raw_dir = DATA_ROOT / "airs_raw" / info["dataset"] / str(info["config"])
    if not (raw_dir / "dataset_dict.json").exists():
        raise RuntimeError(f"raw dataset missing: {raw_dir}")
    (stage / "prep.py").write_text(AIRS_PREP)
    (stage / "sitecustomize.py").write_text(SITECUSTOMIZE)
    code, out = docker_run(CPU_IMAGE, [(DATA_ROOT / "airs_raw", "/raw", "ro"), (task_src, "/task", "ro"),
                                       (stage, "/stage", "ro")],
                           f"TASK={task} PYTHONPATH=/stage python /stage/prep.py", timeout=5400)
    log = (out / "run.log").read_text(errors="replace") if (out / "run.log").exists() else ""
    if code != 0 or "PREP_OK" not in log:
        raise RuntimeError(f"AIRS prepare failed rc={code}: {log[-2500:]}")
    final = stage / "task"
    (final / "evaluation_assets" / "upstream").mkdir(parents=True)
    shutil.move(str(out / "seed"), str(final / "workspace_seed"))
    shutil.move(str(out / "eval_data"), str(final / "evaluation_assets" / "data"))
    for name in ("evaluate.py", "utils.py", "testing_util.py"):
        if (task_src / name).exists():
            shutil.copy2(task_src / name, final / "evaluation_assets" / "upstream" / name)
    for name in ("metadata.yaml", "project_description.md"):
        shutil.copy2(task_src / name, final / "evaluation_assets" / "upstream" / name)
    fixtures = final / "fixtures"
    for sub in ("known_correct", "known_broken", "sanity_gold", "sanity_missing"):
        (fixtures / sub).mkdir(parents=True)
    gold = out / "gold"
    shutil.copy2(gold / "gold_submission.csv", fixtures / "sanity_gold" / "submission.csv")
    # known_correct doubles as the shuffled-gold band anchor (upstream permuted_2 convention)
    shutil.copy2(gold / "gold_submission_permuted_2.csv", fixtures / "known_correct" / "submission.csv")
    _write_truncated(gold / "gold_submission.csv", fixtures / "known_broken" / "submission.csv")
    shutil.rmtree(out, ignore_errors=True)
    norm = normalization_for(spec)
    cfg = _base_task_config(spec, norm, "submission.csv", info["metric"], info["metric"], 3600)
    write_json(final / "evaluation_assets" / "task_config.json", cfg)
    prompt = (task_src / "project_description.md").read_text(encoding="utf-8")
    labels = {"metric_lower_is_better": meta["metric_lower_is_better"],
              "file_export_globs": meta.get("file_export_globs"),
              "training_timeout": airs_timeout(task),
              "evaluate_container_python_requirements": meta.get("evaluate_container_python_requirements"),
              **{k: info.get(k) for k in ("name", "category", "research_problem", "output_type", "dataset",
                                          "config", "metric", "train_split", "test_split", "input_columns",
                                          "scoring_column", "shape", "custom_gold_labels", "sota",
                                          "estimated_worst_score", "optimal_score", "dataset_paper_url")}}
    return {
        "prompt": prompt, "labels": labels, "norm": norm,
        "source_path": f"airsbench/tasks/rad/{task}",
        "source_hash": tree_sha256([task_src, AIRS_ROOT / "airsbench/tasks/mlgym" / task], AIRS_ROOT),
        "submission": {"mode": "workspace_artifact", "payload_kind": "file", "relative_path": "submission.csv"},
        "timeout": airs_timeout(task), "license": AIRS_LICENSE,
        "notes": [
            "prompt = upstream rad project_description.md verbatim; ./data maps to the Turn task directory",
            "workspace_seed/data = output of upstream prepare.py; evaluation_assets/data = output of upstream "
            "evaluate_prepare.py (test_with_labels); both executed in the task image with network disabled",
            "known_correct = heavily row-switched gold submission (upstream test_task_folder.py "
            "gold_submission_permuted_2 convention, seeded); it is also the band anchor; known_broken = gold "
            "header + first 10 rows (row count mismatch)",
            "turn_timeout_seconds = training_timeout from upstream airsbench/tasks/mlgym config (unchanged)",
        ] + (["upstream prepare.py leaves the scoring label `no_answer` in the agent test split (its prompt says "
              "it is not available); the column is dropped from workspace_seed/data/test",
              "upstream custom_labels.py writes header has_answer,answers; gold/known_correct fixtures use the "
              "prompt/evaluator header has_answer,answer"] if task == "QuestionAnsweringDuoRCAccuracy" else []),
    }


def prepare_mlgym(spec: dict[str, Any], stage: Path) -> dict[str, Any]:
    task = spec["task"]
    cfg = mlgym_cfg(task)
    data_dir = MLGYM_ROOT / "data" / spec["data_dir"]
    starter = [str(Path(p)) for p in (cfg.get("starter_code") or [])]
    env = {"TASK": task, "DATA_DIR": spec["data_dir"], "STARTER": json.dumps(starter), "HF_NAME": spec["hf"] or ""}
    env_flags = " ".join(f"{k}={json.dumps(v)}" for k, v in env.items())
    final = stage / "task"
    (final / "evaluation_assets" / "upstream").mkdir(parents=True)
    fixtures = final / "fixtures"
    for sub in ("known_correct", "known_broken", "sanity_missing"):
        (fixtures / sub).mkdir(parents=True)
    notes = [f"prompt = MLGym configs/tasks/{task}.yaml description rendered with dataset_docs as in "
             "mlgym/environment/tasks.py; upstream workspace paths map to the Turn task directory",
             f"turn_timeout_seconds = training_timeout from configs/tasks/{task}.yaml (unchanged)"]
    prompt = mlgym_prompt(cfg)
    norm = normalization_for(spec)
    kind = spec["kind"]
    (stage / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env_flags = "PYTHONPATH=/stage " + env_flags
    mounts = [(MLGYM_ROOT, "/src", "ro"), (stage, "/stage", "ro")]
    if kind == "mlgym_csv":
        if spec["hf"]:
            raw = DATA_ROOT / "mlgym_raw" / spec["hf"] / "default"
            if not (raw / "dataset_dict.json").exists():
                raise RuntimeError(f"raw dataset missing: {raw}")
            mounts.append((DATA_ROOT / "mlgym_raw", "/raw", "ro"))
        (stage / "prep.py").write_text(MLGYM_CSV_PREP)
        code, out = docker_run(CPU_IMAGE, mounts, f"{env_flags} python /stage/prep.py", timeout=3600)
        log = (out / "run.log").read_text(errors="replace") if (out / "run.log").exists() else ""
        if code != 0 or "PREP_OK" not in log:
            raise RuntimeError(f"MLGym prepare failed rc={code}: {log[-2500:]}")
        shutil.move(str(out / "seed"), str(final / "workspace_seed"))
        shutil.move(str(out / "eval_data"), str(final / "evaluation_assets" / "data"))
        (fixtures / "sanity_gold").mkdir()
        shutil.copy2(out / "fx" / "known_correct.csv", fixtures / "known_correct" / "submission.csv")
        shutil.copy2(out / "fx" / "gold.csv", fixtures / "sanity_gold" / "submission.csv")
        _write_truncated(out / "fx" / "gold.csv", fixtures / "known_broken" / "submission.csv")
        shutil.rmtree(out, ignore_errors=True)
        subs = []
        if task == "titanic":
            subs = [["'/home/agent/workspace'", "'{work}'"]]
            notes.append("upstream ships data/ground_truth.csv inside the agent workspace; it is moved to "
                         "evaluation_assets/data (hidden test labels stay private)")
            notes.append("known_correct = gender-rule predictions (female -> 1) built from test.csv; upstream "
                         "sample_submission.csv is identical to ground_truth.csv (not agent-visible here: it is not "
                         "in starter_code) and is therefore not used")
        elif task == "regressionKaggleHousePrice":
            subs = [["'/home/agent/workspace/data/answer.csv'", "'{assets}/data/answer.csv'"]]
            notes.append("data generated by upstream _helpers/_generate_split.py (random_state=42); "
                         "data/answer.csv (test labels) moved to evaluation_assets/data")
            notes.append("known_correct = submission.csv produced by upstream baseline.py in the task image")
            notes.append("agent-visible evaluate.py still points at /home/agent/workspace/data/answer.csv, "
                         "which is absent; only the verifier has the labels")
        else:
            hf = spec["hf"]
            subs = [[f'load_dataset("{hf}", split="test")', 'load_from_disk("{assets}/data/test_with_labels")'],
                    ["from datasets import load_dataset", "from datasets import load_dataset, load_from_disk"]]
            prompt += (f"\n\nOffline data note: the Hugging Face dataset `{hf}` is materialized in ./data "
                       "(`datasets.load_from_disk('./data')` returns train and test splits); the test split has "
                       "no `label` column.")
            notes.append(f"upstream loads {hf} from the Hugging Face hub at run time; the train split and the "
                         "label-free test split are materialized in workspace_seed/data, test labels in "
                         "evaluation_assets/data/test_with_labels")
            notes.append("prompt edit: one appended offline-data sentence (network is disabled)")
            notes.append("starter baseline.py edit: load_dataset(<hub id>) -> load_from_disk('./data') (path "
                         "mapping only). Upstream baseline evaluates on test labels, which are now hidden, so the "
                         "starter needs adaptation before it runs end to end")
            notes.append("known_correct = train-majority-class constant prediction for all test rows (upstream "
                         "sample_submission.csv has only 9 rows and is rejected by the upstream evaluator)")
        task_cfg = _base_task_config(spec, norm, "submission.csv", spec["metric_key"], spec["metric_key"],
                                     spec["verify_timeout"])
        task_cfg["eval_substitutions"] = subs
        submission = {"mode": "workspace_artifact", "payload_kind": "file", "relative_path": "submission.csv"}
        shutil.copy2(data_dir / "evaluate.py", final / "evaluation_assets" / "upstream" / "evaluate.py")
    elif kind == "mlgym_python":
        seed = final / "workspace_seed"
        seed.mkdir()
        for rel in starter:
            shutil.copy2(MLGYM_ROOT / rel, seed / Path(rel).name)
        for name in ("evaluate.py", "target.py"):
            shutil.copy2(data_dir / name, final / "evaluation_assets" / "upstream" / name)
        shutil.copy2(data_dir / "strategy.py", fixtures / "known_correct" / "strategy.py")
        broken = (data_dir / "strategy.py").read_text().replace("def row_strategy(", "def renamed_strategy(")
        (fixtures / "known_broken" / "strategy.py").write_text(broken)
        (fixtures / "sanity_strong").mkdir()
        (fixtures / "sanity_strong" / "strategy.py").write_text(STRONG_STRATEGIES[task])
        task_cfg = _base_task_config(spec, norm, "strategy.py", spec["metric_key"], spec["metric_key"],
                                     spec["verify_timeout"])
        task_cfg["pristine_files"] = ["evaluate.py", "target.py"]
        submission = {"mode": "workspace_artifact", "payload_kind": "python_source", "relative_path": "strategy.py"}
        notes += ["workspace_seed = upstream starter_code (strategy.py, target.py, evaluate.py), as in MLGym",
                  "verifier copies the submitted workspace and overwrites evaluate.py/target.py with pristine "
                  "upstream copies before running `python evaluate.py`",
                  "native metric is a Monte-Carlo mean over 10000 simulated games with an unseeded opponent; "
                  "repeated evaluations differ slightly",
                  "known_correct = upstream baseline strategy.py; known_broken = row_strategy renamed (upstream "
                  "prompt: renaming fails evaluation); sanity_strong = builder-written strategy for direction check"]
    elif kind == "mlgym_model":
        (stage / "prep.py").write_text(MLGYM_RL_PREP)
        code, out = docker_run(CPU_IMAGE, mounts, f"{env_flags} python /stage/prep.py", timeout=1800)
        log = (out / "run.log").read_text(errors="replace") if (out / "run.log").exists() else ""
        if code != 0 or "PREP_OK" not in log:
            raise RuntimeError(f"MLGym RL prepare failed rc={code}: {log[-2500:]}")
        shutil.move(str(out / "seed"), str(final / "workspace_seed"))
        shutil.rmtree(fixtures / "known_correct")
        shutil.move(str(out / "fx" / "known_correct"), str(fixtures / "known_correct"))
        shutil.copytree(final / "workspace_seed", fixtures / "known_broken", dirs_exist_ok=True)
        shutil.rmtree(out, ignore_errors=True)
        shutil.copy2(data_dir / "evaluate.py", final / "evaluation_assets" / "upstream" / "evaluate.py")
        task_cfg = _base_task_config(spec, norm, "src/config.yaml", spec["metric_key"], spec["metric_key"],
                                     spec["verify_timeout"])
        task_cfg["pristine_files"] = ["evaluate.py"]
        submission = {"mode": "workspace_artifact", "payload_kind": "directory", "relative_path": "checkpoints"}
        notes += ["submission = the Turn task directory: checkpoints/<config stem>*.pkl (5 files) plus the "
                  "config yaml and src/ networks, evaluated with `python evaluate.py --config_fname <yaml>` as in "
                  "MLGym ModelSubmissionTasks; evaluate.py is replaced by the pristine upstream copy",
                  "known_correct = starter workspace + 5 randomly initialised checkpoints in the upstream pickle "
                  "format (valid, low reward); known_broken = starter workspace without checkpoints",
                  "normalization is linear (AIRS notebook normalize_score) when the return has no tight optimum"
                  if spec["method"] == "airs_linear" else
                  "normalization airs_log with optimal = 100 (max episode return of MountainCarContinuous)"]
    elif kind == "mlgym_mnli":
        mounts += [(DATA_ROOT / "mlgym_raw", "/raw", "ro"), (DATA_ROOT / "models", "/models", "ro")]
        (stage / "prep.py").write_text(MLGYM_MNLI_PREP)
        code, out = docker_run(CPU_IMAGE, mounts, f"{env_flags} python /stage/prep.py", timeout=3600)
        log = (out / "run.log").read_text(errors="replace") if (out / "run.log").exists() else ""
        if code != 0 or "PREP_OK" not in log:
            raise RuntimeError(f"MLGym MNLI prepare failed rc={code}: {log[-2500:]}")
        shutil.move(str(out / "seed"), str(final / "workspace_seed"))
        shutil.move(str(out / "eval_data"), str(final / "evaluation_assets" / "data"))
        for sub in ("known_correct", "known_broken"):
            shutil.rmtree(fixtures / sub)
            shutil.move(str(out / "fx" / sub), str(fixtures / sub))
        shutil.rmtree(out, ignore_errors=True)
        shutil.copy2(data_dir / "evaluate.py", final / "evaluation_assets" / "upstream" / "evaluate.py")
        task_cfg = _base_task_config(spec, norm, "bert_mnli_model", spec["metric_key"], spec["metric_key"],
                                     spec["verify_timeout"])
        task_cfg["kind"] = "mlgym_script"
        task_cfg["eval_substitutions"] = [
            ['load_dataset("multi_nli")', 'load_from_disk("{assets}/data/mnli_eval")'],
            ["from datasets import load_dataset", "from datasets import load_dataset, load_from_disk"],
            ["BertTokenizer.from_pretrained('bert-base-uncased')",
             "BertTokenizer.from_pretrained('{assets}/data/tokenizer')"]]
        submission = {"mode": "workspace_artifact", "payload_kind": "directory", "relative_path": "bert_mnli_model"}
        prompt += ("\n\nOffline data note: MNLI is materialized in ./data (`datasets.load_from_disk('./data')` "
                   "returns `train` and `validation`; columns text1/text2/label as in SetFit/mnli; `validation` is "
                   "the mismatched split, the matched split used for evaluation is hidden) and the pretrained "
                   "bert-base-uncased weights are in ./bert-base-uncased.")
        notes += ["upstream starter loads SetFit/mnli and bert-base-uncased from the Hugging Face hub; both are "
                  "materialized (multi_nli train + validation_mismatched as SetFit-style columns; weights "
                  "google-bert/bert-base-uncased, 421 MB) because the network is disabled",
                  "upstream validation split (= validation_matched) is the evaluation set itself; here it is hidden "
                  "and validation_mismatched is given instead",
                  "starter baseline.py edits: hub ids -> ./data and ./bert-base-uncased (path mapping), and "
                  "`from transformers import AdamW` -> `from torch.optim import AdamW` (removed in transformers>=4.45)",
                  "verifier runs upstream evaluate.py with hub loads mapped to evaluation_assets/data (mnli_eval, "
                  "tokenizer); BERT inference on 9815 pairs runs on CPU",
                  "prompt edit: one appended offline-data sentence",
                  "known_correct = tiny randomly initialised BertForSequenceClassification (2 layers, hidden 64, "
                  "bert-base-uncased vocab, seed 0) saved with save_pretrained (valid, near-chance accuracy, ~8 MB); "
                  "known_broken = bert_mnli_model with config.json "
                  "but no weights"]
    else:
        raise RuntimeError(f"unsupported kind {kind}")
    write_json(final / "evaluation_assets" / "task_config.json", task_cfg)
    labels = {"id": cfg.get("id"), "name": cfg.get("name"), "task_entrypoint": cfg.get("task_entrypoint"),
              "training_timeout": cfg.get("training_timeout"), "baseline_scores": cfg.get("baseline_scores"),
              "priority_metric": spec["metric_key"], "dataset_configs": cfg.get("dataset_configs"),
              "starter_code": cfg.get("starter_code"), "evaluation_paths": cfg.get("evaluation_paths"),
              "dataset": spec["hf"] or (cfg.get("dataset_configs") or [None])[0]}
    hashed = [MLGYM_ROOT / "configs/tasks" / f"{task}.yaml", data_dir]
    hashed += [MLGYM_ROOT / "configs" / rel for rel in cfg.get("dataset_configs") or []]
    return {"prompt": prompt, "labels": labels, "norm": norm, "source_path": f"configs/tasks/{task}.yaml",
            "source_hash": tree_sha256(hashed, MLGYM_ROOT), "submission": submission,
            "timeout": int(cfg["training_timeout"]), "license": MLGYM_LICENSE, "notes": notes}


def prepare_one(spec: dict[str, Any]) -> dict[str, Any]:
    sid = source_id(spec)
    if spec.get("exclude"):
        return {"source_id": sid, "status": "excluded", "reason": spec["exclude"]}
    stage = fresh_dir("ar-stage-")
    try:
        built = prepare_airs(spec, stage) if spec["source"] == "airs" else prepare_mlgym(spec, stage)
        final = stage / "task"
        shutil.copy2(VERIFIER_TEMPLATE, final / "evaluation_assets" / "source_verifier.py")
        (final / "evaluation_assets" / "shim").mkdir()
        (final / "evaluation_assets" / "shim" / "sitecustomize.py").write_text(SITECUSTOMIZE)
        cap_id, cap_desc = CAP[spec["cap"]]
        gpu = spec["requires_gpu"]
        record = {
            "schema_version": SCHEMA,
            "source_id": sid,
            "source": {"benchmark_id": benchmark_id(spec),
                       "benchmark_revision": AIRS_REV if spec["source"] == "airs" else MLGYM_REV,
                       "source_task_id": spec["task"], "source_path": built["source_path"],
                       "source_hash": built["source_hash"], "license": built["license"]},
            "prompt": built["prompt"],
            "benchmark_native_labels": built["labels"],
            "capability_hint": {"capability_id": cap_id, "domain_id": "auto_research",
                                "task_type_ids": spec["task_types"],
                                "rationale": f"{cap_desc}. {spec['rationale']}".strip()},
            "runtime": {"runtime_profile_id": "l2v5_autoresearch_gpu" if gpu else "l2v5_autoresearch_cpu",
                        "requires_gpu": gpu, "turn_timeout_seconds": built["timeout"],
                        "turn_timeout_source": "upstream training_timeout (unchanged)",
                        "network": "disabled", "task_image": GPU_IMAGE if gpu else CPU_IMAGE,
                        "task_image_digest": image_id(GPU_IMAGE if gpu else CPU_IMAGE),
                        "verifier_image": CPU_IMAGE, "verifier_image_digest": image_id(CPU_IMAGE)},
            "submission": built["submission"],
            "evaluation_profile": "auto_research",
            "metric_profile": None,
            "qualification": {"admitted": False, "notes": built["notes"]},
        }
        record["_pending_norm"] = built["norm"]
        write_json(final / "pool_task.json", record)
        POOL_ROOT.mkdir(parents=True, exist_ok=True)
        dest = POOL_ROOT / task_dir_name(spec)
        if dest.exists():
            trash = POOL_ROOT.parent / f".trash-{dest.name}-{int(time.time())}"
            dest.rename(trash)
            shutil.rmtree(trash, ignore_errors=True)
        shutil.move(str(final), str(dest))
        sizes = {"workspace_seed_bytes": du_bytes(dest / "workspace_seed"),
                 "evaluation_assets_bytes": du_bytes(dest / "evaluation_assets"),
                 "fixtures_bytes": du_bytes(dest / "fixtures")}
        return {"source_id": sid, "status": "prepared", **sizes}
    except Exception as exc:  # recorded per candidate; the run continues
        return {"source_id": sid, "status": "prepare_failed", "reason": str(exc)[-3000:]}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ---------------------------------------------------------------------------
# Qualify
# ---------------------------------------------------------------------------

QUALIFY_RUNNER = r'''
import importlib.util, json, sys, time
from pathlib import Path
turn = Path("/q/turn")
spec = importlib.util.spec_from_file_location("source_verifier", turn / "evaluation_assets" / "source_verifier.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
results = {}
for fixture in sorted(p for p in (turn / "fixtures").iterdir() if p.is_dir()):
    start = time.time()
    try:
        res = mod.verify(fixture, turn)
    except Exception as exc:
        res = {"status": "error", "score": None, "raw_metric": None, "details": {"exception": repr(exc)}}
    res["seconds"] = round(time.time() - start, 2)
    for key in ("stdout_tail", "stderr_tail"):
        res.get("details", {}).pop(key, None) if res.get("status") == "pass" else None
    results[fixture.name] = res
    print(fixture.name, res["status"], res.get("score"), res.get("raw_metric"), flush=True)
Path("/out/qualification.json").write_text(json.dumps(results, indent=2, default=str))
print("QUALIFY_DONE")
'''


def derive_bands(norm: dict[str, Any], shuffled_score: float | None) -> tuple[dict[str, float], str]:
    anchor = 0.0
    if shuffled_score is not None and math.isfinite(shuffled_score):
        anchor = max(0.0, min(1.0, shuffled_score))
    partial = anchor + PARTIAL_FRACTION * (1.0 - anchor)
    full = anchor + FULL_FRACTION * (1.0 - anchor)
    if norm["method"] == "airs_log":
        scale = "AIRS log-normalized score (fraction of the log-error gap from the worst anchor to the reference)"
    else:
        scale = "AIRS linear normalized score"
    text = (f"Bands on the {scale}: NS=0 at worst={norm['worst']}, NS=1 at reference={norm['reference']}. "
            f"V5 band style (partial/full thresholds on the normalized score) with partial = a + "
            f"{PARTIAL_FRACTION}*(1-a) and full = a + {FULL_FRACTION}*(1-a), where a = "
            f"{anchor:.4f} is the normalized score of the shuffled-gold known_correct fixture (upstream permuted_2 "
            "convention) "
            "for AIRS, or 0 for MLGym (worst anchor = upstream baseline, so the starter solution scores 0).")
    return {"partial_min_inclusive": round(partial, 6), "full_min_inclusive": round(full, 6)}, text


def raw_threshold(norm: dict[str, Any], ns: float) -> float | None:
    """Raw metric value whose normalized score equals ``ns`` (for reporting)."""
    worst, ref = norm["worst"], norm["reference"]
    if norm["method"] == "airs_linear":
        return worst + ns * (ref - worst)
    opt = norm["optimal"]
    phi_w, phi_r = -math.log10(abs(worst - opt)), -math.log10(abs(ref - opt))
    err = 10 ** (-(phi_w + ns * (phi_r - phi_w)))
    return opt - err if ref < opt else opt + err


def qualify_one(spec: dict[str, Any]) -> dict[str, Any]:
    sid = source_id(spec)
    if spec.get("exclude"):
        return {"source_id": sid, "status": "excluded", "reason": spec["exclude"]}
    dest = POOL_ROOT / task_dir_name(spec)
    if not (dest / "pool_task.json").exists():
        return {"source_id": sid, "status": "not_prepared"}
    record = json.loads((dest / "pool_task.json").read_text())
    work = fresh_dir("ar-qual-")
    try:
        turn = work / "turn"
        turn.mkdir()
        shutil.copytree(dest / "evaluation_assets", turn / "evaluation_assets", symlinks=True)
        shutil.copytree(dest / "fixtures", turn / "fixtures", symlinks=True)
        (work / "runner.py").write_text(QUALIFY_RUNNER)
        started = time.time()
        code, out = docker_run(CPU_IMAGE, [(work, "/q", "ro")], "python /q/runner.py",
                               timeout=4 * 3600, cpus=16, memory="64g")
        results_file = out / "qualification.json"
        log = (out / "run.log").read_text(errors="replace") if (out / "run.log").exists() else ""
        results = json.loads(results_file.read_text()) if results_file.exists() else {}
        shutil.rmtree(out, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    norm = record.pop("_pending_norm", None) or (record.get("metric_profile") or {}).get("normalization")
    shuffled = results.get("known_correct", {}) if spec["source"] == "airs" else {}
    shuffled_score = shuffled.get("score") if shuffled.get("status") == "pass" else None
    bands, band_source = derive_bands(norm, shuffled_score if spec["source"] == "airs" else None)
    checks: dict[str, bool] = {}
    kc, kb = results.get("known_correct", {}), results.get("known_broken", {})
    checks["known_correct_pass"] = kc.get("status") == "pass" and kc.get("score") is not None and \
        0.0 <= float(kc["score"]) <= 1.0
    checks["known_broken_fail"] = kb.get("status") == "fail"
    checks["missing_artifact_fail"] = results.get("sanity_missing", {}).get("status") == "fail"
    if "sanity_gold" in results:
        gold = results["sanity_gold"]
        checks["gold_reaches_full_band"] = gold.get("status") == "pass" and \
            float(gold.get("score") or 0) >= bands["full_min_inclusive"]
        if shuffled_score is not None:
            checks["gold_above_shuffled"] = float(gold.get("score") or 0) > shuffled_score or \
                float(gold.get("score") or 0) == 1.0 and shuffled_score < 1.0
    if "sanity_strong" in results:
        strong = results["sanity_strong"]
        checks["strong_beats_baseline"] = strong.get("status") == "pass" and \
            float(strong.get("raw_metric") or -1e18) > float(kc.get("raw_metric") or 1e18)
    admitted = bool(results) and code == 0 and all(checks.values())
    metric_profile = {
        "profile_id": f"{task_dir_name(spec)}_v1",
        "metric_name": json.loads((dest / "evaluation_assets/task_config.json").read_text())["metric_name"],
        "raw_direction": _direction(spec, norm),
        "raw_metric_required": True,
        "judge_value_source": "native_verifier_result.score",
        "judge_value_semantics": "normalized_higher_is_better_0_1",
        "normalization": norm,
        "performance_bands": bands,
        "band_source": band_source,
        "raw_band_thresholds": {"partial": raw_threshold(norm, bands["partial_min_inclusive"]),
                                "full": raw_threshold(norm, bands["full_min_inclusive"])},
    }
    image = image_id(CPU_IMAGE)
    record["metric_profile"] = metric_profile
    qual = record.get("qualification", {})
    notes = [n for n in qual.get("notes", []) if not n.startswith("qualified in ")]
    notes.append(f"qualified in {CPU_IMAGE} ({image}) with --network none; verifier runs on CPU"
                 + ("; runtime image for this GPU task is " + GPU_IMAGE if spec["requires_gpu"] else ""))
    def brief(res: dict[str, Any]) -> dict[str, Any]:
        out = {k: res.get(k) for k in ("status", "score", "raw_metric", "seconds")}
        if res.get("status") != "pass":
            out["reason"] = (res.get("details") or {}).get("reason") or (res.get("details") or {}).get("exception")
        return out
    record["qualification"] = {
        "image": CPU_IMAGE, "image_digest": image,
        "known_correct": brief(kc), "known_broken": brief(kb),
        "sanity": {k: brief(v) for k, v in results.items() if k.startswith("sanity_")},
        "checks": checks, "admitted": admitted, "notes": notes,
        "qualified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(time.time() - started, 1),
    }
    if not admitted:
        record["qualification"]["reason"] = ("docker run failed: " + log[-1500:]) if not results else \
            "failed checks: " + ", ".join(k for k, v in checks.items() if not v)
    write_json(dest / "pool_task.json", record)
    return {"source_id": sid, "status": "admitted" if admitted else "rejected", "checks": checks,
            "known_correct": brief(kc), "bands": bands}


def _direction(spec: dict[str, Any], norm: dict[str, Any]) -> str:
    if spec["source"] == "airs":
        return "minimize" if airs_meta(spec["task"])["metric_lower_is_better"] else "maximize"
    return "maximize" if norm["reference"] > norm["worst"] else "minimize"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def write_index() -> Path:
    lines = []
    for spec in CANDIDATES:
        sid = source_id(spec)
        cap_id = CAP[spec["cap"]][0]
        row: dict[str, Any] = {"source_id": sid, "task_dir": task_dir_name(spec), "requires_gpu": spec["requires_gpu"],
                               "capability_id": cap_id, "admitted": False}
        path = POOL_ROOT / task_dir_name(spec) / "pool_task.json"
        if spec.get("exclude"):
            row["reason"] = spec["exclude"]
            row["timeout"] = _declared_timeout(spec)
        elif path.exists():
            rec = json.loads(path.read_text())
            qual = rec.get("qualification", {})
            row.update({"admitted": bool(qual.get("admitted")), "timeout": rec["runtime"]["turn_timeout_seconds"],
                        "bands": (rec.get("metric_profile") or {}).get("performance_bands"),
                        "known_correct": qual.get("known_correct"),
                        "size_bytes": du_bytes(path.parent)})
            if not qual.get("admitted"):
                row["reason"] = qual.get("reason", "not qualified")
        else:
            row["reason"] = "prepare failed or not run (see prepare log)"
            row["timeout"] = _declared_timeout(spec)
        lines.append(json.dumps(row, sort_keys=True))
    POOL_ROOT.mkdir(parents=True, exist_ok=True)
    index = POOL_ROOT / "INDEX.jsonl"
    index.write_text("\n".join(lines) + "\n")
    return index


def _declared_timeout(spec: dict[str, Any]) -> int | None:
    try:
        return airs_timeout(spec["task"]) if spec["source"] == "airs" else mlgym_cfg(spec["task"]).get("training_timeout")
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["prepare", "qualify", "index", "all", "list"])
    parser.add_argument("--only", nargs="*", help="task names (upstream ids) to process")
    parser.add_argument("--log", type=Path, help="append JSON lines of per-task results here")
    args = parser.parse_args()
    specs = [s for s in CANDIDATES if not args.only or s["task"] in args.only]
    if args.command == "list":
        for spec in specs:
            print(source_id(spec), "GPU" if spec["requires_gpu"] else "CPU", CAP[spec["cap"]][0],
                  "EXCLUDED: " + spec["exclude"] if spec.get("exclude") else "")
        return 0
    steps = {"prepare": [prepare_one], "qualify": [qualify_one], "all": [prepare_one, qualify_one], "index": []}
    for spec in specs:
        for step in steps[args.command]:
            result = step(spec)
            line = json.dumps({"step": step.__name__, **result}, default=str)
            print(line, flush=True)
            if args.log:
                with args.log.open("a") as handle:
                    handle.write(line + "\n")
    print("index:", write_index())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
