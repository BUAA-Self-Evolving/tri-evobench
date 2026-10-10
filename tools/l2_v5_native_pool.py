#!/usr/bin/env python3
"""Turn prepared + qualified SkillLearnBench/SkillsBench tasks into L2 V5 pool tasks.

Input: the ``--work`` directory of ``l2_v5_native_sources.py`` (prepared.jsonl,
qualification.jsonl, sources/<benchmark>/<task>/{seed.tar,oracle_output.tar}).
Output: ``<pool>/<task_dir>/`` per docs/l2-v5-rebuild/POOL_FORMAT.md.

The agent works in ``/workspace/tasks/<turn_id>/``. The verifier copies that
directory to the upstream WORKDIR (``/root`` and ``/app`` for those roots),
installs the upstream tests at their native mount, and runs
``pytest test_outputs.py`` exactly as upstream ``test.sh`` does, minus the
network installs that the pinned image already provides.

``qualify`` re-runs known_correct (oracle output) and known_broken (untouched
seed) through this verifier inside the task image, network disabled.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKOUTS = {
    "skilllearnbench": REPO_ROOT / "third_party/src/skilllearnbench",
    "skillsbench": REPO_ROOT / "third_party/src/skillsbench",
}
REVISIONS = {
    "skilllearnbench": "a0da045a8bf64b8a8ff20730c4d6ef10dc4e2c5b",
    "skillsbench": "9a1f4dd5f7659f75707435da3ce854b6e48321d1",
}
SCRATCH = Path("/data_storage/yl_test/lgx/data-1/tmp/l2v5")
# Prompts that rely on upstream-provided skill files (which L2 withholds). Generic
# wording such as "use video frame extraction skill" is fine: tools cover it.
SKILL_PROMPT = re.compile(
    r"utili[sz]e (the )?skills|use the (provided |available )?skills|(provided|available|given|installed) skills"
    r"|skills? (directory|folder|library|files?)", re.IGNORECASE)
# Per-task input data is copied into the pool, every Stream placement, and the
# runtime Workspace; ~0.9 GB financial filings tasks would dominate the package.
MAX_SEED_BYTES = 300_000_000

VERIFIER = '''"""Upstream file-artifact verifier: copy the task directory to the native WORKDIR, run upstream tests."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _copy_into(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, destination, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination, follow_symlinks=False)


def verify(submission, turn_dir):
    assets = Path(turn_dir) / "evaluation_assets"
    meta = json.loads((assets / "native_task.json").read_text(encoding="utf-8"))
    submission = Path(submission)
    if not submission.is_dir():
        return {"status": "fail", "score": 0.0, "raw_metric": None, "details": {"reason": "missing_task_directory"}}
    for root in meta["copy_roots"]:
        _copy_into(submission, Path(root))
    tests = Path(meta["tests_mount"])
    if tests.exists():
        shutil.rmtree(tests)
    shutil.copytree(assets / "native_tests", tests)
    Path("/logs/verifier").mkdir(parents=True, exist_ok=True)
    # Upstream ENV lines like PATH=/opt/venv/bin:$PATH are unexpanded text; keep the image PATH.
    upstream = {k: v for k, v in meta.get("env", {}).items() if k != "PATH" and "$" not in v}
    env = {**os.environ, **upstream}
    try:
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", f"{tests}/test_outputs.py"],
            cwd=meta["workdir"], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=meta["verifier_timeout_seconds"])
        code, output = run.returncode, run.stdout
    except subprocess.TimeoutExpired as exc:
        code, output = 124, (exc.stdout or "") if isinstance(exc.stdout, str) else "timeout"
    passed = code == 0
    return {"status": "pass" if passed else "fail", "score": 1.0 if passed else 0.0, "raw_metric": None,
            "details": {"carrier": "upstream_pytest_test_outputs/1.0", "exit_code": code, "output_tail": output[-4000:]}}
'''

QUALIFY_SCRIPT = r'''
import json, shutil, sys, importlib.util
from pathlib import Path
task = Path("/opt/task")
spec = importlib.util.spec_from_file_location("sv", task / "evaluation_assets/source_verifier.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
name = sys.argv[1]
work = Path("/tmp/sub"); shutil.copytree(task / "fixtures" / name, work, symlinks=True)
res = mod.verify(work, task)
Path(f"/out/{name}.json").write_text(json.dumps(res))
'''


def frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    import sys
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from l2_data_pipeline import _frontmatter  # PyYAML when present, else a small fallback parser
    text = path.read_text(encoding="utf-8")
    body = text.split("---\n", 2)[2] if text.startswith("---\n") else text
    return _frontmatter(path), body.strip()


def upstream_info(row: dict[str, Any]) -> dict[str, Any]:
    benchmark, task_id = row["benchmark_id"], row["source_task_id"]
    if benchmark == "skilllearnbench":
        task_dir = CHECKOUTS[benchmark] / "tasks" / row["family"] / task_id
        meta = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        return {
            "task_dir": task_dir, "prompt": (task_dir / "instruction.md").read_text(encoding="utf-8").strip(),
            "labels": {"family": row["family"], **meta.get("metadata", {})},
            "agent_timeout": int(meta.get("agent", {}).get("timeout_sec", 900)),
            "verifier_timeout": int(meta.get("verifier", {}).get("timeout_sec", 600)),
            "tests_dir": task_dir / "tests", "tests_mount": "/tests",
        }
    task_dir = CHECKOUTS[benchmark] / "tasks" / task_id
    meta, body = frontmatter(task_dir / "task.md")
    labels = {key: value for key, value in (meta.get("metadata") or {}).items()
              if key not in {"author_name", "author_email"}}
    return {
        "task_dir": task_dir, "prompt": body, "labels": labels,
        "agent_timeout": int(float((meta.get("agent") or {}).get("timeout_sec", 900))),
        "verifier_timeout": int(float((meta.get("verifier") or {}).get("timeout_sec", 600))),
        "tests_dir": task_dir / "verifier", "tests_mount": "/verifier",
    }


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
    return digest.hexdigest()


def extract(tar_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as archive:
        archive.extractall(target, filter="tar")


def image_digest(image: str) -> str:
    return subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image],
                          check=True, text=True, capture_output=True).stdout.strip()


def build(work: Path, pool: Path, image: str) -> None:
    digest = image_digest(image)
    prepared = {json.loads(l)["source_id"]: json.loads(l) for l in (work / "prepared.jsonl").read_text().splitlines()}
    qualified = {json.loads(l)["source_id"]: json.loads(l) for l in (work / "qualification.jsonl").read_text().splitlines()}
    pool.mkdir(parents=True, exist_ok=True)
    index = []
    for source_id, prep in sorted(prepared.items()):
        row = {"source_id": source_id, "benchmark_id": prep["benchmark_id"]}
        qual = qualified.get(source_id)
        if prep["status"] != "prepared":
            index.append({**row, "admitted": False, "stage": "prepare", "reason": prep.get("reason")})
            continue
        if not qual or not qual.get("admitted"):
            index.append({**row, "admitted": False, "stage": "upstream_red_green",
                          "reason": (qual or {}).get("failure_reasons", ["not_qualified"])})
            continue
        if prep.get("seed_bytes", 0) > MAX_SEED_BYTES:
            index.append({**row, "admitted": False, "stage": "size",
                          "reason": f"seed_bytes={prep['seed_bytes']} exceeds {MAX_SEED_BYTES}"})
            continue
        info = upstream_info(prep)
        if SKILL_PROMPT.search(info["prompt"]):
            index.append({**row, "admitted": False, "stage": "fairness",
                          "reason": "prompt_refers_to_upstream_skills_which_L2_does_not_provide"})
            continue
        oracle_tar = Path(qual["green"]["oracle_output_tar"])
        task_dir_name = f"{prep['benchmark_id']}__{prep['source_task_id']}"
        dest = pool / task_dir_name
        if dest.exists():
            shutil.rmtree(dest)
        assets = dest / "evaluation_assets"
        assets.mkdir(parents=True)
        extract(Path(prep["seed_tar"]), dest / "workspace_seed")
        extract(Path(prep["seed_tar"]), dest / "fixtures" / "known_broken")
        extract(oracle_tar, dest / "fixtures" / "known_correct")
        shutil.copytree(info["tests_dir"], assets / "native_tests",
                        ignore=shutil.ignore_patterns("test.sh", "__pycache__"))
        workdir = prep["workdir"]
        roots = ["/root", "/app"] if workdir in {"/root", "/app"} else [workdir]
        (assets / "native_task.json").write_text(json.dumps({
            "workdir": workdir, "copy_roots": roots, "tests_mount": info["tests_mount"],
            "env": prep.get("dockerfile_env") or {}, "verifier_timeout_seconds": info["verifier_timeout"],
        }, indent=2) + "\n")
        (assets / "source_verifier.py").write_text(VERIFIER)
        source_hash = hashlib.sha256(json.dumps({
            "prompt": info["prompt"], "seed": prep["seed_tree_sha256"],
            "tests": tree_hash(info["tests_dir"]), "dockerfile": prep["upstream_dockerfile_sha256"],
        }, sort_keys=True).encode()).hexdigest()
        rel = info["task_dir"].relative_to(CHECKOUTS[prep["benchmark_id"]]).as_posix()
        record = {
            "schema_version": "l2_v5_pool_task/1.0", "source_id": source_id,
            "source": {"benchmark_id": prep["benchmark_id"], "benchmark_revision": REVISIONS[prep["benchmark_id"]],
                       "source_task_id": prep["source_task_id"], "source_path": rel, "source_hash": source_hash},
            "prompt": info["prompt"], "benchmark_native_labels": info["labels"],
            "capability_hint": {
                "capability_id": (f"skilllearn.{prep['family']}" if prep["benchmark_id"] == "skilllearnbench"
                                  else f"skillsbench.{info['labels'].get('category') or 'uncategorized'}"),
                "rationale": ("upstream task family (native siblings)" if prep["benchmark_id"] == "skilllearnbench"
                              else "upstream metadata.category (coarse group)"),
                "family": prep["family"], "category": info["labels"].get("category")},
            "runtime": {"runtime_profile_id": None, "requires_gpu": False,
                        "turn_timeout_seconds": info["agent_timeout"], "verifier_timeout_seconds": info["verifier_timeout"] + 120,
                        "network": "disabled", "task_image": image, "task_image_digest": digest,
                        "upstream_workdirs": roots},
            "submission": {"mode": "workspace_artifact", "payload_kind": "directory", "relative_path": ""},
            "evaluation_profile": "general", "metric_profile": None,
            "qualification": {"image_digest": digest, "known_correct": None, "known_broken": None, "admitted": False,
                              "notes": [f"skipped_install_segments={len(prep.get('skipped_install_segments', []))}",
                                        "upstream skills are not materialized", *prep.get("notes", [])]},
        }
        (dest / "pool_task.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        index.append({**row, "admitted": None, "stage": "built", "task_dir": task_dir_name, "family": prep["family"],
                      "category": info["labels"].get("category")})
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in index))
    print(json.dumps({"built": sum(1 for r in index if r["stage"] == "built"), "total": len(index)}))


def qualify_one(task_dir: Path, image: str) -> dict[str, Any]:
    results = {}
    with tempfile.TemporaryDirectory(prefix="l2v5-pq-", dir=SCRATCH) as raw:
        temp = Path(raw)
        task_copy = temp / "task"
        shutil.copytree(task_dir, task_copy, symlinks=True)
        out = temp / "out"
        out.mkdir()
        (out / "q.py").write_text(QUALIFY_SCRIPT)
        for name in ("known_correct", "known_broken"):
            cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "4g", "--cpus", "2",
                   "--pids-limit", "512", "-e", "MPLBACKEND=Agg",
                   "-v", f"{task_copy}:/opt/task:ro", "-v", f"{out}:/out", "--entrypoint", "python", image,
                   "/out/q.py", name]
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            except subprocess.TimeoutExpired:
                pass
            path = out / f"{name}.json"
            if path.exists():
                res = json.loads(path.read_text())
                results[name] = {"status": res["status"], "score": res["score"],
                                 "exit_code": res["details"].get("exit_code"),
                                 "tail": res["details"].get("output_tail", "")[-600:]}
            else:
                results[name] = {"status": "error", "score": None}
    return results


def qualify(pool: Path, image: str, jobs: int) -> None:
    index = [json.loads(l) for l in (pool / "INDEX.jsonl").read_text().splitlines()]
    built = [r for r in index if r["stage"] == "built"]
    with concurrent.futures.ThreadPoolExecutor(jobs) as executor:
        futures = {executor.submit(qualify_one, pool / r["task_dir"], image): r for r in built}
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            res = future.result()
            ok = res["known_correct"]["status"] == "pass" and res["known_broken"]["status"] == "fail"
            path = pool / row["task_dir"] / "pool_task.json"
            record = json.loads(path.read_text())
            record["qualification"].update(known_correct=res["known_correct"], known_broken=res["known_broken"],
                                           admitted=ok)
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            row["admitted"] = ok
            if not ok:
                row["reason"] = {k: {kk: v.get(kk) for kk in ("status", "exit_code", "tail")} for k, v in res.items()}
            print(row["source_id"], ok, flush=True)
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in index))
    print(json.dumps({"admitted": sum(1 for r in index if r.get("admitted")), "built": len(built)}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--work", type=Path, required=True)
    b.add_argument("--pool", type=Path, required=True)
    b.add_argument("--image", required=True)
    q = sub.add_parser("qualify")
    q.add_argument("--pool", type=Path, required=True)
    q.add_argument("--image", required=True)
    q.add_argument("--jobs", type=int, default=12)
    args = parser.parse_args()
    if args.command == "build":
        build(args.work, args.pool, args.image)
    else:
        qualify(args.pool, args.image, args.jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
