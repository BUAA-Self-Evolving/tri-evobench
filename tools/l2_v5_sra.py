#!/usr/bin/env python3
"""Build and qualify SR-Agents (SRA-Bench) BigCodeBench pool tasks for L2 V5.

Every upstream BigCodeBench instance becomes one pool task. The agent sees the
native question (SR-Agents sends it as the only user message, no system
prompt) and answers in its final response. The verifier is the upstream
SR-Agents evaluator: tree-sitter sanitize + ``untrusted_check`` against the
native unittest suite, copied verbatim from the pinned checkout.

Usage:
  l2_v5_sra.py build   --pool <pool>/sra
  l2_v5_sra.py qualify --pool <pool>/sra --image <tag> [--jobs 16] [--only ID ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "third_party/src/sr-agents"
REVISION = "277fd8d2bbd7d3b81a5cf4ffa6e87e18c7906e4f"
INSTANCES = "data/bench/instances/bigcodebench.json"
NATIVE_FILES = {
    "sanitize.py": "src/sragents/evaluate/datasets/bigcodebench/sanitize.py",
    "execution.py": "src/sragents/evaluate/datasets/bigcodebench/execution.py",
}
RUNTIME_PROFILE = "sra_python_v1"
TURN_TIMEOUT = 900

# Coarse capability groups keyed by upstream gold skill ids (corpus names in
# comments). The first matching group in this order wins, so a task that plots
# a DataFrame is a visualization task, and one that fetches a URL is a web task.
CAPABILITY_ORDER = [
    ("sra.web_and_network", {"bigcodebench_077", "bigcodebench_082", "bigcodebench_007"}),  # requests, urllib, bs4
    ("sra.data_visualization", {"bigcodebench_138", "bigcodebench_013", "bigcodebench_022", "bigcodebench_011"}),  # matplotlib, seaborn, wordcloud, cv2
    ("sra.dataframe_analysis", {"bigcodebench_094"}),  # pandas
    ("sra.numerical_and_ml", {"bigcodebench_137", "bigcodebench_024", "bigcodebench_062", "bigcodebench_084", "bigcodebench_079"}),  # numpy, sklearn, scipy, statistics, math
    ("sra.filesystem_and_io", {"bigcodebench_130", "bigcodebench_133", "bigcodebench_134", "bigcodebench_135", "bigcodebench_119",
                               "bigcodebench_116", "bigcodebench_042", "bigcodebench_047", "bigcodebench_025", "bigcodebench_061"}),  # os, shutil, glob, pathlib, csv, json, zipfile, io, sqlite3, subprocess
    ("sra.text_processing", {"bigcodebench_036", "bigcodebench_045", "bigcodebench_041", "bigcodebench_105", "bigcodebench_115"}),  # re, regex, string, collections, nltk
    ("sra.encoding_and_hashing", {"bigcodebench_078", "bigcodebench_067", "bigcodebench_086", "bigcodebench_117"}),  # base64, hashlib, binascii, codecs
    ("sra.datetime_and_time", {"bigcodebench_043", "bigcodebench_056", "bigcodebench_064", "bigcodebench_050"}),  # datetime, pytz, dateutil, time
]

VERIFIER = '''"""SR-Agents native BigCodeBench evaluator (upstream sragents.evaluate.datasets.bigcodebench)."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "sra_native"))
from execution import PASS, untrusted_check  # noqa: E402
from sanitize import sanitize  # noqa: E402

_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)


def strip_think_tags(text):
    if "<think>" not in text:
        return text
    text = _THINK_CLOSED_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text.lstrip()


def verify(submission, turn_dir):
    if not isinstance(submission, str) or not submission.strip():
        return {"status": "fail", "score": 0.0, "raw_metric": None, "details": {"reason": "empty_submission"}}
    item = json.loads((Path(turn_dir) / "evaluation_assets/source_item.json").read_text(encoding="utf-8"))
    eval_data = item["eval_data"]
    extracted = sanitize(strip_think_tags(submission), entrypoint=eval_data.get("entry_point", ""))
    code_prompt = eval_data.get("code_prompt", "")
    solution = code_prompt + "\\n    pass\\n" + extracted if code_prompt else extracted
    stat, details = untrusted_check(solution, eval_data["test"], eval_data.get("entry_point", ""),
                                    max_as_limit=30 * 1024, max_data_limit=30 * 1024, max_stack_limit=10)
    passed = stat == PASS
    return {"status": "pass" if passed else "fail", "score": 1.0 if passed else 0.0, "raw_metric": None,
            "details": {"carrier": "sragents_bigcodebench_native/1.0", "result": stat,
                        "details": json.loads(json.dumps(details, default=str))[:20] if isinstance(details, list) else str(details)[:4000]}}
'''

QUALIFY_IN_CONTAINER = r'''
import json, sys, importlib.util
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
pool = Path("/pool"); jobs = int(sys.argv[1]); only = set(sys.argv[2:])

def run(task_dir):
    spec = importlib.util.spec_from_file_location("v_" + task_dir.name, task_dir / "evaluation_assets/source_verifier.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    out = {"task_dir": task_dir.name}
    for name in ("known_correct", "known_broken"):
        try:
            res = mod.verify((task_dir / "fixtures" / name / "response.txt").read_text(), task_dir)
            out[name] = {"status": res["status"], "score": res["score"], "result": res["details"].get("result")}
        except Exception as exc:
            out[name] = {"status": "error", "score": None, "result": f"{type(exc).__name__}: {exc}"[:500]}
    return out

dirs = sorted(d for d in pool.iterdir() if (d / "pool_task.json").exists() and (not only or d.name in only))
with ProcessPoolExecutor(jobs) as ex, open("/out/results.jsonl", "w") as fh:
    for res in ex.map(run, dirs):
        fh.write(json.dumps(res) + "\n"); fh.flush()
'''


def capability(skills: list[str]) -> str:
    for cap, ids in CAPABILITY_ORDER:
        if ids & set(skills):
            return cap
    return "sra.general_python"


def task_dir_name(instance_id: str) -> str:
    return f"sr_agents__{instance_id}"


def build(pool: Path) -> None:
    items = json.loads((UPSTREAM / INSTANCES).read_text(encoding="utf-8"))
    pool.mkdir(parents=True, exist_ok=True)
    index = []
    for item in items:
        skills = item["skill_annotations"]
        if isinstance(skills, str):
            skills = json.loads(skills.replace("'", '"'))
        eval_data = item["eval_data"]
        canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
        dest = pool / task_dir_name(item["instance_id"])
        if dest.exists():
            shutil.rmtree(dest)
        assets = dest / "evaluation_assets"
        (assets / "sra_native").mkdir(parents=True)
        (dest / "workspace_seed").mkdir()
        for name, rel in NATIVE_FILES.items():
            shutil.copy2(UPSTREAM / rel, assets / "sra_native" / name)
        (assets / "source_item.json").write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (assets / "source_verifier.py").write_text(VERIFIER, encoding="utf-8")
        for name, body in (("known_correct", eval_data["code_prompt"] + eval_data["answer"]),
                           ("known_broken", eval_data["code_prompt"] + "    return None\n")):
            (dest / "fixtures" / name).mkdir(parents=True)
            (dest / "fixtures" / name / "response.txt").write_text(body, encoding="utf-8")
        cap = capability(skills)
        record = {
            "schema_version": "l2_v5_pool_task/1.0",
            "source_id": f"sr_agents:{item['instance_id']}",
            "source": {"benchmark_id": "sr_agents", "benchmark_revision": REVISION,
                       "source_task_id": item["instance_id"], "source_path": f"{INSTANCES}#{item['instance_id']}",
                       "source_hash": hashlib.sha256(canonical.encode()).hexdigest()},
            "prompt": item["question"],
            "benchmark_native_labels": {"dataset": item["dataset"], "skill_annotations": skills},
            "capability_hint": {"capability_id": cap, "domain_id": "software_engineering",
                                "task_type_ids": ["code_generation"],
                                "rationale": "first matching coarse group over upstream gold skill_annotations"},
            "runtime": {"runtime_profile_id": RUNTIME_PROFILE, "requires_gpu": False,
                        "turn_timeout_seconds": TURN_TIMEOUT, "network": "disabled",
                        "task_image": None, "task_image_digest": None},
            "submission": {"mode": "final_response", "payload_kind": "python_source", "relative_path": None},
            "evaluation_profile": "general",
            "metric_profile": None,
            "qualification": {"image_digest": None, "known_correct": None, "known_broken": None,
                              "admitted": False, "notes": ["prompt is the native SR-Agents user message; no system prompt"]},
        }
        (dest / "pool_task.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        index.append({"source_id": record["source_id"], "task_dir": dest.name, "capability_id": cap})
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index), encoding="utf-8")
    print(f"built {len(index)} tasks into {pool}")


def image_digest(image: str) -> str:
    return subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image],
                          check=True, text=True, capture_output=True).stdout.strip()


def qualify(pool: Path, image: str, jobs: int, only: list[str]) -> None:
    digest = image_digest(image)
    with tempfile.TemporaryDirectory(prefix="l2v5-sra-", dir="/data_storage/yl_test/lgx/data-1/tmp/l2v5") as raw:
        out = Path(raw)
        (out / "qualify.py").write_text(QUALIFY_IN_CONTAINER, encoding="utf-8")
        cmd = ["docker", "run", "--rm", "--network", "none", "--cpus", str(jobs), "--memory", "32g",
               "-v", f"{pool}:/pool:ro", "-v", f"{out}:/out", "--entrypoint", "python", image,
               "/out/qualify.py", str(jobs), *only]
        subprocess.run(cmd, check=True, timeout=7200)
        results = {r["task_dir"]: r for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}
    admitted = 0
    index = []
    for line in (pool / "INDEX.jsonl").read_text().splitlines():
        row = json.loads(line)
        res = results.get(row["task_dir"])
        if res is not None:
            path = pool / row["task_dir"] / "pool_task.json"
            record = json.loads(path.read_text())
            ok = res["known_correct"]["status"] == "pass" and res["known_broken"]["status"] == "fail"
            record["runtime"].update(task_image=image, task_image_digest=digest)
            record["qualification"].update(image_digest=digest, known_correct=res["known_correct"],
                                           known_broken=res["known_broken"], admitted=ok)
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            row.update(admitted=ok, reason=None if ok else f"correct={res['known_correct']} broken={res['known_broken']}")
        admitted += bool(row.get("admitted"))
        index.append(row)
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index), encoding="utf-8")
    print(f"qualified {len(results)}; admitted total {admitted}/{len(index)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--pool", type=Path, required=True)
    q = sub.add_parser("qualify"); q.add_argument("--pool", type=Path, required=True)
    q.add_argument("--image", required=True); q.add_argument("--jobs", type=int, default=16)
    q.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    if args.cmd == "build":
        build(args.pool)
    else:
        qualify(args.pool.resolve(), args.image, args.jobs, args.only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
