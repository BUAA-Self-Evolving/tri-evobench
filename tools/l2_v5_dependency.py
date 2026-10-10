#!/usr/bin/env python3
"""Build the explicit-dependency subset of the L2 V5 pool.

Each line splits one native AutoResearch task into two stages:

* stage 1 (``turn_origin: derived_evolution_turn``) prepares an artifact in its
  own task directory: feature tables, cleaned series, or an evaluation harness.
  Its verifier is written here and is labelled derived.
* stage 2 is the unchanged native task with the unchanged native verifier, but
  its workspace seed is empty: the data, code, and the stage-1 artifact exist
  only in the stage-1 directory, which the prompt names through the
  ``{{dependency_dir}}`` placeholder that the composer resolves to the actual
  stage-1 Turn directory. Removing the stage-1 Turn leaves stage 2 without its
  inputs.

Records carry ``pool_scope: explicit_dependency_only`` so the planner only uses
them when a stream pins them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

SCRATCH = Path("/data_storage/yl_test/lgx/data-1/tmp/l2v5")
POOL_ROOT = Path("/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_pool")
IMAGE = "trievo-l2-v5-ar-cpu:0.1"

LINES = {
    "houseprice": {"parent": "mlgym__regressionKaggleHousePrice", "stage": "feature_tables"},
    "rideshare": {"parent": "airs-bench__TimeSeriesForecastingRideshareMAE", "stage": "series_cleaning"},
    "blotto": {"parent": "mlgym__blotto", "stage": "evaluation_harness"},
}
BLOTTO_CANDIDATES = {"uniform": [20, 20, 20, 20, 20, 20], "four_fields": [0, 0, 30, 30, 30, 30],
                     "three_fields": [40, 40, 40, 0, 0, 0], "two_fields": [60, 60, 0, 0, 0, 0]}
BLOTTO_TOLERANCE = 0.03

STAGE1_PROMPT = {
    "houseprice": """Stage 1 of 2 (feature preparation) for the Kaggle house-price regression task described below. Do not train a model or write a submission in this Turn; a later Turn will build the model from the tables you produce here.

Write a script `prepare_features.py` in this directory and run it so that it creates `features/train.csv`, `features/validation.csv`, and `features/test.csv` from `data/train.csv`, `data/validation.csv`, and `data/test.csv`:
- keep every row, in the original order, with the original `Id` column;
- keep the target column `SalePrice` unchanged in the train and validation tables, and leave it out of the test table;
- every other column must be numeric with no missing or infinite values (encode the categorical columns and impute missing values);
- the three tables must have exactly the same feature columns, and there must be at least {min_features} of them (the number of numeric input columns in the raw data), so categorical information is encoded rather than dropped.

Original task statement, for context:

""",
    "rideshare": """Stage 1 of 2 (data cleaning) for the time-series forecasting task described below. Do not fit a forecasting model or write a submission in this Turn; a later Turn will forecast from the series you clean here.

The datasets in `data/train` and `data/test` (load them with `datasets.load_from_disk`) hold one record per series. Each record's `target` is a list of channels, each a list of floats, and contains missing values (NaN). Write `clean_series.py` in this directory and run it so that it creates `clean/train.json` and `clean/test.json`. Each file is a JSON list with one entry per record, in the original order, holding that record's `target` with exactly the same shape, where every missing value is replaced by a finite imputed number and every observed (non-missing) value is left unchanged. Write plain JSON numbers (no NaN or Infinity).

Original task statement, for context:

""",
    "blotto": """Stage 1 of 2 (evaluation harness) for the Colonel Blotto task described below. Do not change `strategy.py` in this Turn; a later Turn will use your harness to design a stronger strategy.

Write `harness.py` in this directory with a function `estimate(allocation, games=20000)` that returns the expected per-game payoff of always playing the fixed `allocation` against `target_column_strategy` from `target.py`, using the payoff rules in `evaluate.py`. Then use it (at least 20000 simulated games per allocation) to write `analysis/candidates.json`: a JSON object mapping each of these names to its estimated payoff as a number:
- "uniform": [20, 20, 20, 20, 20, 20]
- "four_fields": [0, 0, 30, 30, 30, 30]
- "three_fields": [40, 40, 40, 0, 0, 0]
- "two_fields": [60, 60, 0, 0, 0, 0]

Original task statement, for context:

""",
}
STAGE2_PROMPT = {
    "houseprice": """This Turn continues the house-price work from an earlier Turn. The data, the baseline, and the feature tables you prepared (`features/train.csv`, `features/validation.csv`, `features/test.csv`) are in `{{dependency_dir}}`; this Turn's directory `{{turn_dir}}` starts empty. Train a model on those feature tables and save `submission.csv` in `{{turn_dir}}`. In the original task statement below, relative paths such as `data/` and `baseline.py` refer to `{{dependency_dir}}`, and the submission goes to `{{turn_dir}}`.

""",
    "rideshare": """This Turn continues the forecasting work from an earlier Turn. The original datasets (`data/train`, `data/test`) and the cleaned series you prepared (`clean/train.json`, `clean/test.json`) are in `{{dependency_dir}}`; this Turn's directory `{{turn_dir}}` starts empty. Forecast from those cleaned series and save `submission.csv` in `{{turn_dir}}`. In the original task statement below, relative paths such as `data/` refer to `{{dependency_dir}}`, and the submission goes to `{{turn_dir}}`.

""",
    "blotto": """This Turn continues the Colonel Blotto work from an earlier Turn. The game files (`evaluate.py`, `target.py`, `strategy.py`) and the harness you wrote (`harness.py`, `analysis/candidates.json`) are in `{{dependency_dir}}`; this Turn's directory `{{turn_dir}}` starts empty. Use your harness to design a stronger strategy and save the final `strategy.py`, with the same `row_strategy(history)` interface, in `{{turn_dir}}`. In the original task statement below, the game files refer to `{{dependency_dir}}`, and the strategy goes to `{{turn_dir}}`.

""",
}

VERIFIER_COMMON = '''"""Derived stage-1 verifier (explicit-dependency subset, not an upstream check)."""
import json
import math
from pathlib import Path


def _result(issues, details):
    ok = not issues
    return {"status": "pass" if ok else "fail", "score": 1.0 if ok else 0.0, "raw_metric": None,
            "details": {"derived_verifier": True, "issues": issues[:20], **details}}

'''
VERIFIERS = {
    "houseprice": VERIFIER_COMMON + '''
def verify(submission, turn_dir):
    import numpy as np
    import pandas as pd
    ref = Path(turn_dir) / "evaluation_assets" / "stage_reference"
    spec = json.loads((ref / "spec.json").read_text())
    if not isinstance(submission, Path) or not submission.is_dir():
        return _result(["workspace directory required"], {})
    issues, columns = [], {}
    for split in ("train", "validation", "test"):
        path = submission / "features" / f"{split}.csv"
        if not path.is_file():
            issues.append(f"missing features/{split}.csv")
            continue
        try:
            frame = pd.read_csv(path)
        except Exception as exc:
            issues.append(f"features/{split}.csv unreadable: {type(exc).__name__}")
            continue
        raw = pd.read_csv(ref / f"{split}.csv")
        if "Id" not in frame or frame["Id"].tolist() != raw["Id"].tolist():
            issues.append(f"{split}: Id column missing or rows/order differ from the raw table")
        if split == "test":
            if "SalePrice" in frame:
                issues.append("test: SalePrice must not be present")
        elif "SalePrice" not in frame or len(frame) != len(raw) or not np.allclose(
                pd.to_numeric(frame["SalePrice"], errors="coerce"), raw["SalePrice"], equal_nan=False):
            issues.append(f"{split}: SalePrice missing or changed")
        feats = [c for c in frame.columns if c not in ("Id", "SalePrice")]
        columns[split] = feats
        block = frame[feats]
        non_numeric = [c for c in feats if not pd.api.types.is_numeric_dtype(block[c])]
        if non_numeric:
            issues.append(f"{split}: non-numeric feature columns {non_numeric[:5]}")
        elif not np.isfinite(block.to_numpy(dtype=float)).all():
            issues.append(f"{split}: missing or infinite feature values")
    if len(columns) == 3:
        if not (columns["train"] == columns["validation"] == columns["test"]):
            issues.append("feature columns differ between the three tables")
        if len(columns["train"]) < spec["min_features"]:
            issues.append(f"only {len(columns['train'])} feature columns; at least {spec['min_features']} required")
    return _result(issues, {"feature_columns": len(columns.get("train", []))})
''',
    "rideshare": VERIFIER_COMMON + '''
def _reject(token):
    raise ValueError(f"non-finite JSON number {token}")


def verify(submission, turn_dir):
    ref = Path(turn_dir) / "evaluation_assets" / "stage_reference"
    if not isinstance(submission, Path) or not submission.is_dir():
        return _result(["workspace directory required"], {})
    issues, imputed = [], 0
    for split in ("train", "test"):
        path = submission / "clean" / f"{split}.json"
        raw = json.loads((ref / f"{split}.json").read_text())
        if not path.is_file():
            issues.append(f"missing clean/{split}.json")
            continue
        try:
            clean = json.loads(path.read_text(), parse_constant=_reject)
        except Exception as exc:
            issues.append(f"clean/{split}.json invalid: {exc}")
            continue
        if not isinstance(clean, list) or len(clean) != len(raw):
            issues.append(f"{split}: expected {len(raw)} records")
            continue
        for r, (got, want) in enumerate(zip(clean, raw)):
            if not isinstance(got, list) or len(got) != len(want):
                issues.append(f"{split}[{r}]: channel count differs")
                continue
            for c, (gs, ws) in enumerate(zip(got, want)):
                if not isinstance(gs, list) or len(gs) != len(ws):
                    issues.append(f"{split}[{r}][{c}]: length differs")
                    continue
                for g, w in zip(gs, ws):
                    if not isinstance(g, (int, float)) or isinstance(g, bool) or not math.isfinite(g):
                        issues.append(f"{split}[{r}][{c}]: non-finite value")
                        break
                    if w is None:
                        imputed += 1
                    elif abs(g - w) > 1e-6 * max(1.0, abs(w)):
                        issues.append(f"{split}[{r}][{c}]: observed value changed")
                        break
    return _result(issues, {"imputed_values": imputed})
''',
    "blotto": VERIFIER_COMMON + '''
def verify(submission, turn_dir):
    ref = json.loads((Path(turn_dir) / "evaluation_assets" / "stage_reference" / "reference.json").read_text())
    if not isinstance(submission, Path) or not submission.is_dir():
        return _result(["workspace directory required"], {})
    issues = []
    if not (submission / "harness.py").is_file():
        issues.append("missing harness.py")
    path = submission / "analysis" / "candidates.json"
    try:
        got = json.loads(path.read_text())
    except Exception as exc:
        return _result(issues + [f"analysis/candidates.json missing or invalid: {type(exc).__name__}"], {})
    errors = {}
    for name, value in ref["expected_payoff"].items():
        estimate = got.get(name) if isinstance(got, dict) else None
        if not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or not math.isfinite(estimate):
            issues.append(f"{name}: missing or non-numeric estimate")
            continue
        errors[name] = round(abs(estimate - value), 4)
        if errors[name] > ref["tolerance"]:
            issues.append(f"{name}: estimate is off by more than {ref['tolerance']}")
    return _result(issues, {"absolute_errors": errors})
''',
}

# Runs inside the AutoResearch CPU image; writes references and fixtures under /w/out/<line>/.
BUILD_SCRIPT = r'''
import json, math, random, shutil, sys
from pathlib import Path
import numpy as np, pandas as pd
w = Path("/w")

def houseprice():
    seed, out = w / "houseprice/seed", w / "out/houseprice"
    ref = out / "stage_reference"; ref.mkdir(parents=True)
    raw = {s: pd.read_csv(seed / "data" / f"{s}.csv") for s in ("train", "validation", "test")}
    for s, f in raw.items():
        f.to_csv(ref / f"{s}.csv", index=False)
    numeric = [c for c in raw["train"].columns if c not in ("Id", "SalePrice") and pd.api.types.is_numeric_dtype(raw["train"][c])]
    (ref / "spec.json").write_text(json.dumps({"min_features": len(numeric)}))
    allx = pd.concat([raw[s].drop(columns=["SalePrice"], errors="ignore").assign(_split=s) for s in raw])
    feats = allx.drop(columns=["Id", "_split"])
    num = feats.select_dtypes("number"); num = num.fillna(num.median())
    cat = pd.get_dummies(feats.select_dtypes(exclude="number").fillna("NA"), dtype=float)
    full = pd.concat([allx[["Id", "_split"]], num, cat], axis=1)
    for name, broken in (("known_correct", False), ("known_broken", True)):
        d = out / "fixtures" / name / "features"; d.mkdir(parents=True)
        for s in raw:
            part = full[full["_split"] == s].drop(columns=["_split"]).reset_index(drop=True)
            if broken:
                part = raw[s].drop(columns=["SalePrice"], errors="ignore")[["Id"] + numeric].copy()
            if s != "test":
                part.insert(1, "SalePrice", raw[s]["SalePrice"].values)
            part.to_csv(d / f"{s}.csv", index=False)

def rideshare():
    from datasets import load_from_disk
    seed, out = w / "rideshare/seed", w / "out/rideshare"
    ref = out / "stage_reference"; ref.mkdir(parents=True)
    for name in ("known_correct", "known_broken"):
        (out / "fixtures" / name / "clean").mkdir(parents=True)
    for s in ("train", "test"):
        rows = [r["target"] for r in load_from_disk(str(seed / "data" / s))]
        nulls = [[[None if (v is None or math.isnan(v)) else v for v in ch] for ch in r] for r in rows]
        (ref / f"{s}.json").write_text(json.dumps(nulls))
        clean = []
        for r in rows:
            chans = []
            for ch in r:
                ser = pd.Series(ch, dtype=float).interpolate(limit_direction="both")
                ser = ser.fillna(0.0)
                chans.append([float(v) for v in ser])
            clean.append(chans)
        (out / "fixtures/known_correct/clean" / f"{s}.json").write_text(json.dumps(clean))
        (out / "fixtures/known_broken/clean" / f"{s}.json").write_text(json.dumps(nulls))

def blotto():
    seed, out = w / "blotto/seed", w / "out/blotto"
    sys.path.insert(0, str(seed))
    from target import target_column_strategy
    candidates = json.loads(sys.argv[1]); tolerance = float(sys.argv[2])
    def payoff(a, b):
        r = sum(x > y for x, y in zip(a, b)); c = sum(x < y for x, y in zip(a, b))
        return 1 if r > c else -1 if r < c else 0
    def estimate(alloc, games):
        return float(np.mean([payoff(alloc, target_column_strategy([])) for _ in range(games)]))
    random.seed(20261010)
    expected = {k: round(estimate(v, 200000), 5) for k, v in candidates.items()}
    ref = out / "stage_reference"; ref.mkdir(parents=True)
    (ref / "reference.json").write_text(json.dumps({"expected_payoff": expected, "tolerance": tolerance,
        "games": 200000, "candidates": candidates}))
    harness = (seed / "evaluate.py").read_text().split("def evaluate")[0].replace("from strategy import row_strategy\n", "")
    harness += ("\n\ndef estimate(allocation, games=20000):\n"
                "    return float(np.mean([calculate_payoffs(allocation, target_column_strategy([]))[0] for _ in range(games)]))\n")
    for name in ("known_correct", "known_broken"):
        d = out / "fixtures" / name; (d / "analysis").mkdir(parents=True)
        (d / "harness.py").write_text(harness)
    random.seed(7)
    est = {k: estimate(v, 40000) for k, v in candidates.items()}
    (out / "fixtures/known_correct/analysis/candidates.json").write_text(json.dumps(est))
    (out / "fixtures/known_broken/analysis/candidates.json").write_text(json.dumps({k: 0.0 for k in candidates}))

houseprice(); rideshare(); blotto()
(w / "out/done").write_text("ok")
'''

# Runs the stage-1 verifiers on the fixtures inside the image.
QUALIFY_SCRIPT = r'''
import json, sys
from pathlib import Path
results = {}
for line in ("houseprice", "rideshare", "blotto"):
    turn = Path("/w/q") / line
    sys.path.insert(0, str(turn / "evaluation_assets"))
    import importlib
    mod = importlib.import_module(f"verifier_{line}")
    results[line] = {name: mod.verify(turn / "fixtures" / name, turn) for name in ("known_correct", "known_broken", "sanity_missing")}
Path("/w/q/results.json").write_text(json.dumps(results, default=str))
'''


def docker(work: Path, script: str, *args: str) -> None:
    (work / "script.py").write_text(script)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/w", "--entrypoint", "python",
                    IMAGE, "/w/script.py", *args], check=True, capture_output=True, timeout=3600)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-root", type=Path, default=POOL_ROOT)
    args = parser.parse_args()
    ar = args.pool_root / "autoresearch"
    dest_root = args.pool_root / "dependency"
    SCRATCH.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="l2v5-dep-", dir=SCRATCH))
    try:
        for line, info in LINES.items():
            shutil.copytree(ar / info["parent"] / "workspace_seed", work / line / "seed")
        docker(work, BUILD_SCRIPT, json.dumps(BLOTTO_CANDIDATES), str(BLOTTO_TOLERANCE))
        if not (work / "out/done").exists():
            raise SystemExit("stage build did not complete")
        # qualify the stage-1 verifiers on their fixtures
        q = work / "q"
        for line in LINES:
            (q / line / "evaluation_assets").mkdir(parents=True)
            (q / line / "evaluation_assets" / f"verifier_{line}.py").write_text(VERIFIERS[line])
            shutil.copytree(work / "out" / line / "stage_reference", q / line / "evaluation_assets" / "stage_reference")
            shutil.copytree(work / "out" / line / "fixtures", q / line / "fixtures")
            (q / line / "fixtures" / "sanity_missing").mkdir()
        docker(work, QUALIFY_SCRIPT)
        results = json.loads((q / "results.json").read_text())
        print(json.dumps({k: {f: (v["status"], v["details"].get("issues", [])[:2]) for f, v in r.items()}
                          for k, r in results.items()}, indent=1))
        if dest_root.exists():
            shutil.rmtree(dest_root)
        index = []
        for line, info in LINES.items():
            parent_dir = ar / info["parent"]
            parent = json.loads((parent_dir / "pool_task.json").read_text())
            res = results[line]
            admitted = (res["known_correct"]["status"] == "pass" and res["known_broken"]["status"] == "fail"
                        and res["sanity_missing"]["status"] == "fail")
            psrc = parent["source"]
            stage_task = f"{psrc['source_task_id']}#stage1_{info['stage']}"
            prompt1 = STAGE1_PROMPT[line]
            if line == "houseprice":
                spec = json.loads((work / "out/houseprice/stage_reference/spec.json").read_text())
                prompt1 = prompt1.replace("{min_features}", str(spec["min_features"]))
            prompt1 += parent["prompt"]
            # stage 1: derived
            d1 = dest_root / f"{info['parent']}__stage1"
            shutil.copytree(parent_dir / "workspace_seed", d1 / "workspace_seed")
            (d1 / "evaluation_assets").mkdir(parents=True)
            (d1 / "evaluation_assets" / "source_verifier.py").write_text(VERIFIERS[line])
            shutil.copytree(work / "out" / line / "stage_reference", d1 / "evaluation_assets" / "stage_reference")
            shutil.copytree(work / "out" / line / "fixtures", d1 / "fixtures")
            stage1 = {
                "schema_version": "l2_v5_pool_task/1.0",
                # benchmark_id:source_task_id, the identity the source catalog is keyed by
                "source_id": f"{parent['source_id']}#stage1_{info['stage']}",
                "pool_scope": "explicit_dependency_only",
                "turn_origin": "derived_evolution_turn",
                "source": {**psrc, "source_task_id": stage_task,
                           "source_hash": sha(psrc["source_hash"] + VERIFIERS[line] + prompt1),
                           "parent_source_task_ids": [psrc["source_task_id"]],
                           "derivation": {"kind": "explicit_dependency_stage", "stage": 1, "stage_name": info["stage"],
                                          "rule": "Derived preparation stage of the parent native task; the stage verifier is written for L2 V5 and is not an upstream check."}},
                "prompt": prompt1,
                "benchmark_native_labels": parent.get("benchmark_native_labels", {}),
                "capability_hint": parent["capability_hint"],
                "runtime": parent["runtime"],
                "submission": {"mode": "workspace_artifact", "payload_kind": "directory", "relative_path": "."},
                "evaluation_profile": "general", "metric_profile": None,
                "qualification": {"image": IMAGE, "image_digest": parent["runtime"]["task_image_digest"],
                                  "known_correct": res["known_correct"], "known_broken": res["known_broken"],
                                  "sanity_missing": res["sanity_missing"], "admitted": admitted,
                                  "notes": ["derived stage-1 verifier; see source.derivation"]},
            }
            (d1 / "pool_task.json").write_text(json.dumps(stage1, indent=2, ensure_ascii=False) + "\n")
            # stage 2: native task, data only in the stage-1 directory
            d2 = dest_root / f"{info['parent']}__stage2"
            (d2 / "workspace_seed").mkdir(parents=True)
            shutil.copytree(parent_dir / "evaluation_assets", d2 / "evaluation_assets", symlinks=True)
            shutil.copytree(parent_dir / "fixtures", d2 / "fixtures", symlinks=True)
            stage2 = {**{k: v for k, v in parent.items() if k != "_dir"},
                      "source_id": f"{parent['source_id']}#stage2",
                      "pool_scope": "explicit_dependency_only",
                      "turn_origin": "source_task",
                      "prompt": STAGE2_PROMPT[line] + parent["prompt"],
                      "dependency": {"requires_source_id": stage1["source_id"], "placeholder": "{{dependency_dir}}",
                                     "artifact": info["stage"]},
                      "qualification": {**parent["qualification"],
                                        "admitted": admitted and parent["qualification"].get("admitted", False),
                                        "notes": list(parent["qualification"].get("notes", [])) + [
                                            "explicit-dependency stage 2: native verifier unchanged; workspace seed moved to the stage-1 Turn directory"]}}
            (d2 / "pool_task.json").write_text(json.dumps(stage2, indent=2, ensure_ascii=False) + "\n")
            index += [{"source_id": stage1["source_id"], "admitted": admitted, "line": line, "stage": 1},
                      {"source_id": stage2["source_id"], "admitted": stage2["qualification"]["admitted"], "line": line, "stage": 2}]
        (dest_root / "INDEX.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index))
        print(json.dumps(index))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
