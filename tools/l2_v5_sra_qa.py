#!/usr/bin/env python3
"""Materialize and independently qualify native SR-Agents QA tasks.

Build into a staging pool, qualify with one fresh offline container per fixture,
then publish only admitted task directories. INDEX.jsonl retains every candidate.
No retrieval corpus or skill text is copied into tasks.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

REVISION = "277fd8d2bbd7d3b81a5cf4ffa6e87e18c7906e4f"
FAMILIES = ("medcalcbench", "logicbench", "theoremqa", "champ")
PREFIX = {"medcalcbench": "medcalc", "logicbench": "logic", "theoremqa": "theoremqa", "champ": "champ"}
NATIVE_FILES = ["llm.py", "prompts.py", "evaluate/base.py", "evaluate/common.py"]
PACKAGE_SHIMS = ("__init__.py", "evaluate/__init__.py", "evaluate/datasets/__init__.py")
SHIM_CONTENT = "# L2 packaging shim; native evaluator files are unchanged.\n"
VERIFIER = '''"""Carrier for verbatim SR-Agents QA evaluators; see PROVENANCE.json."""
import importlib
import json
from pathlib import Path
import sys


def verify(submission, turn_dir):
    assets = Path(turn_dir) / "evaluation_assets"
    sys.path.insert(0, str(assets / "native"))
    item = json.loads((assets / "source_item.json").read_text())
    evaluator = importlib.import_module("sragents.evaluate.datasets." + item["dataset"])
    result = evaluator.evaluate(submission if isinstance(submission, str) else "", item)
    passed = bool(result["correct"])
    return {"status": "pass" if passed else "fail", "score": float(passed),
            "raw_metric": None, "details": result}
'''
RUNNER = '''import importlib.util, json, sys
from pathlib import Path
root = Path("/task")
spec = importlib.util.spec_from_file_location("verifier", root / "evaluation_assets/source_verifier.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    result = module.verify((root / "fixtures" / sys.argv[1] / "response.txt").read_text(), root)
except Exception as exc:
    result = {"status": "error", "score": None, "details": str(exc)}
Path("/out/result.json").write_text(json.dumps(result))
'''


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skills_of(item: dict) -> list[str]:
    skills = item["skill_annotations"]
    if isinstance(skills, str):
        skills = ast.literal_eval(skills)
    if not isinstance(skills, list) or not skills or not all(isinstance(s, str) for s in skills):
        raise ValueError(f"Invalid skill annotations: {item['instance_id']}")
    return skills


def capability(item: dict) -> str:
    return f"sra_qa.{PREFIX[item['dataset']]}.{skills_of(item)[0]}"


def fixture_answer(item: dict) -> str:
    answer = str(item["eval_data"]["answer"])
    if item["dataset"] in ("medcalcbench", "champ"):
        return "ANSWER: " + answer
    return "Therefore, the answer is " + answer


def materialize(item: dict, upstream: Path, pool: Path, license_info: dict) -> dict:
    license_info = dict(license_info)
    license_path = license_info.pop("license_text_path", None)
    family = item["dataset"]
    instance_id = item["instance_id"]
    if family not in FAMILIES or Path(instance_id).name != instance_id:
        raise ValueError("Unsupported family or unsafe instance ID")
    dest = pool / ("sr_agents__" + instance_id)
    dest.mkdir()  # Never overwrite an existing task or its qualification.
    assets = dest / "evaluation_assets"
    native = assets / "native/sragents"
    native.mkdir(parents=True)
    (dest / "workspace_seed").mkdir()
    provenance = []
    for rel in [*NATIVE_FILES, f"evaluate/datasets/{family}.py"]:
        source = upstream / "src/sragents" / rel
        target = native / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        provenance.append({"path": f"src/sragents/{rel}", "revision": REVISION, "sha256": sha256(source)})
    # Namespace shims avoid importing the upstream dispatcher's unrelated datasets.
    for rel in PACKAGE_SHIMS:
        (native / rel).write_text(SHIM_CONTENT)
    shutil.copyfile(upstream / "LICENSE", assets / "SR_AGENTS_LICENSE")
    if license_path:
        shutil.copyfile(license_path, assets / "DATASET_LICENSE")
        license_info["license_asset"] = "evaluation_assets/DATASET_LICENSE"
        license_info["license_sha256"] = sha256(assets / "DATASET_LICENSE")
    write_json(assets / "PROVENANCE.json", {"revision": REVISION, "files": provenance,
               "dataset_license": license_info, "packaging": "Empty package initializers only; evaluator and helpers verbatim."})
    write_json(assets / "source_item.json", item)
    (assets / "source_verifier.py").write_text(VERIFIER)
    for name, value in (("known_correct", fixture_answer(item)), ("known_broken", "")):
        folder = dest / "fixtures" / name
        folder.mkdir(parents=True)
        (folder / "response.txt").write_text(value, encoding="utf-8")
    prompt_path = upstream / "src/sragents/prompts.py"
    namespace = {"__name__": "sra_qa_prompts"}
    # Compile in memory: SourceFileLoader would write __pycache__ upstream.
    exec(compile(prompt_path.read_text(), str(prompt_path), "exec"), namespace)
    system, user = namespace["build_prompt"](item)
    record = {
        "schema_version": "l2_v5_pool_task/1.0", "source_id": f"sr_agents:{instance_id}",
        "source": {"benchmark_id": "sr_agents", "benchmark_revision": REVISION,
                   "source_task_id": instance_id, "source_path": f"data/bench/instances/{family}.json#{instance_id}",
                   "source_hash": hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()},
        "prompt": system + "\n\n" + user if system else user,
        "benchmark_native_labels": {"dataset": family, "skill_annotations": item["skill_annotations"]},
        "capability_hint": {"capability_id": capability(item), "domain_id": "reasoning",
                            "task_type_ids": ["question_answering"],
                            "rationale": "One upstream skill per capability; first annotation assigns multi-skill CHAMP instances."},
        "license": license_info,
        "runtime": {"runtime_profile_id": "sra_qa_python_v1", "requires_gpu": False,
                    "turn_timeout_seconds": 300, "network": "disabled", "task_image": None, "task_image_digest": None},
        "submission": {"mode": "final_response", "payload_kind": "text", "relative_path": None},
        "evaluation_profile": "general", "metric_profile": None,
        "qualification": {"image_digest": None, "known_correct": None, "known_broken": None,
                          "admitted": False, "notes": ["Native system and user prompts concatenated; no skills supplied.",
                                                        "Empty final response is the known-broken fixture."]},
    }
    write_json(dest / "pool_task.json", record)
    return {"source_id": record["source_id"], "task_dir": dest.name, "family": family,
            "capability_id": capability(item), "source": record["source"], "admitted": False, "reason": "not_qualified"}


def build(upstream: Path, pool: Path, licenses: dict) -> list[dict]:
    revision = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if revision != REVISION:
        raise ValueError(f"Expected upstream {REVISION}, got {revision}")
    if pool.exists() and any(pool.iterdir()):
        raise ValueError("Build requires an empty staging directory")
    for family in FAMILIES:
        info = licenses[family]
        if (not info.get("redistributable") or not info.get("license") or not info.get("source")
                or not info.get("license_text_path") or not Path(info["license_text_path"]).is_file()):
            raise ValueError(f"Dataset redistribution or license notice not established: {family}")
    pool.mkdir(parents=True, exist_ok=True)
    rows = []
    for family in FAMILIES:
        info = licenses[family]
        items = json.loads((upstream / f"data/bench/instances/{family}.json").read_text())
        rows.extend(materialize(item, upstream, pool, info) for item in items)
    write_index(pool, rows)
    return rows


def write_index(pool: Path, rows: list[dict]) -> None:
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def fixture_command(task: Path, fixture: str, digest: str, folder: Path) -> list[str]:
    """Return the exact fresh-container command, also used for cache validation."""
    name = "sra-qa-" + hashlib.sha256(str(folder).encode()).hexdigest()[:24]
    return ["docker", "run", "--name", name, "--rm", "--network", "none", "--cpus", "1", "--memory", "1g",
           "-v", f"{task.resolve()}:/task:ro", "-v", f"{folder.resolve()}:/out",
           "--entrypoint", "python", digest, "/out/run.py", fixture]


def qualify_fixture(task: Path, fixture: str, digest: str, logs: Path) -> dict:
    folder = logs / task.name / fixture
    folder.mkdir(parents=True, exist_ok=True)
    runner = folder / "run.py"
    runner.write_text(RUNNER)
    result_path = folder / "result.json"
    result_path.unlink(missing_ok=True)
    cmd = fixture_command(task, fixture, digest, folder)
    name = cmd[cmd.index("--name") + 1]
    write_json(folder / "command.json", cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        (folder / "docker.log").write_text(result.stdout + result.stderr)
        if result.returncode or not result_path.exists():
            return {"status": "error", "score": None, "details": f"docker exit={result.returncode}; result exists={result_path.exists()}"}
        return json.loads(result_path.read_text())
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        return {"status": "error", "score": None, "details": "container timeout"}



def cached_qualification(task: Path, digest: str, logs: Path) -> tuple[dict, dict] | None:
    """Validate a checkpoint; incomplete or changed inputs require a fresh run."""
    try:
        return _cached_qualification(task, digest, logs)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        # A truncated/missing checkpoint is a cache miss, never an admission.
        return None


def _cached_qualification(task: Path, digest: str, logs: Path) -> tuple[dict, dict] | None:
    """Reuse only complete results bound to unchanged native assets and fixtures."""
    record = json.loads((task / "pool_task.json").read_text())
    qualification = record["qualification"]
    if qualification.get("image_digest") != digest:
        return None
    assets = task / "evaluation_assets"
    item = json.loads((assets / "source_item.json").read_text())
    item_hash = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if item_hash != record["source"]["source_hash"] or (assets / "source_verifier.py").read_text() != VERIFIER:
        return None
    provenance = json.loads((assets / "PROVENANCE.json").read_text())
    if provenance.get("revision") != REVISION or record["source"].get("benchmark_revision") != REVISION:
        return None
    expected_paths = {f"src/sragents/{rel}" for rel in [*NATIVE_FILES, f"evaluate/datasets/{item['dataset']}.py"]}
    if len(provenance["files"]) != len(expected_paths) or {e["path"] for e in provenance["files"]} != expected_paths:
        return None
    for rel in PACKAGE_SHIMS:
        if (assets / "native/sragents" / rel).read_text() != SHIM_CONTENT:
            return None
    for entry in provenance["files"]:
        if sha256(assets / "native" / entry["path"].removeprefix("src/")) != entry["sha256"]:
            return None
    results = []
    for fixture, expected_body in (("known_correct", fixture_answer(item)), ("known_broken", "")):
        result = qualification.get(fixture)
        folder = logs / task.name / fixture
        if not result or result.get("status") not in ("pass", "fail") or result.get("score") not in (0.0, 1.0):
            return None
        if (task / "fixtures" / fixture / "response.txt").read_text() != expected_body:
            return None
        if not (folder / "result.json").exists() or json.loads((folder / "result.json").read_text()) != result:
            return None
        if not (folder / "run.py").exists() or (folder / "run.py").read_text() != RUNNER:
            return None
        command = json.loads((folder / "command.json").read_text())
        if command != fixture_command(task, fixture, digest, folder):
            return None
        results.append(result)
    return results[0], results[1]


def qualify(pool: Path, image: str, logs: Path, jobs: int = 8, resume: bool = False) -> list[dict]:
    digest = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image], check=True, capture_output=True, text=True).stdout.strip()
    if not digest.startswith("sha256:"):
        raise ValueError("Missing immutable image ID")
    rows = [json.loads(line) for line in (pool / "INDEX.jsonl").read_text().splitlines()]

    def run(row: dict) -> dict:
        task = pool / row["task_dir"]
        cached = cached_qualification(task, digest, logs) if resume else None
        if cached is None:
            green = qualify_fixture(task, "known_correct", digest, logs)
            red = qualify_fixture(task, "known_broken", digest, logs)
        else:
            green, red = cached
        admitted = green.get("status") == "pass" and green.get("score") == 1.0 and red.get("status") == "fail" and red.get("score") == 0.0
        record = json.loads((task / "pool_task.json").read_text())
        record["runtime"].update(task_image=image, task_image_digest=digest)
        if not isinstance(record.get("qualification"), dict):
            record["qualification"] = {}
        record["qualification"].update(image_digest=digest, known_correct=green, known_broken=red, admitted=admitted)
        write_json(task / "pool_task.json", record)
        return {**row, "admitted": admitted, "reason": "native_red_green_pass" if admitted else "native_red_green_failed",
                "known_correct": green, "known_broken": red, "image_digest": digest}

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        results = []
        for row in executor.map(run, rows):
            results.append(row)
            if len(results) % 100 == 0:
                print(f"Qualified {len(results)}/{len(rows)}", flush=True)
    write_index(pool, results)
    return results


def publish(staging: Path, output: Path, selection: dict | None = None) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError("Publish requires an empty output directory")
    rows = [json.loads(line) for line in (staging / "INDEX.jsonl").read_text().splitlines()]
    if selection is not None:
        indexed = {row["source_id"].split(":", 1)[1]: row for row in rows}
        for group in selection["capabilities"]:
            for instance_id in group["representative_ids"] + group["backup_ids"]:
                row = indexed[instance_id]
                if not row["admitted"] or row["capability_id"] != group["capability_id"]:
                    raise ValueError(f"Selected instance not admitted to its capability: {instance_id}")
                row["curation_role"] = "representative" if instance_id in group["representative_ids"] else "backup"
        for instance_id, reason in selection.get("rejected_instances", {}).items():
            row = indexed[instance_id]
            if row.get("curation_role"):
                raise ValueError(f"Selected and rejected instance: {instance_id}")
            row.update(admitted=False, reason="curation_rejected: " + reason)
    output.mkdir(parents=True, exist_ok=True)
    if selection is not None:
        write_json(output / "SELECTION.json", selection)
    for row in rows:
        if row["admitted"]:
            shutil.copytree(staging / row["task_dir"], output / row["task_dir"])
    write_index(output, rows)
    counts = {}
    for family in FAMILIES:
        relevant = [r for r in rows if r["family"] == family]
        skills = Counter(r["capability_id"] for r in relevant if r["admitted"])
        counts[family] = {"considered": len(relevant), "admitted": sum(skills.values()),
                          "rejected": sum(not r["admitted"] for r in relevant),
                          "skills_at_least_5": sum(n >= 5 for n in skills.values()),
                          "skills_at_least_3": sum(n >= 3 for n in skills.values()),
                          "top_10": skills.most_common(10), "all_capabilities": dict(skills),
                          "per_capability": {
                              cap: {"admitted": skills[cap],
                                    "rejected": sum(not r["admitted"] for r in relevant if r["capability_id"] == cap),
                                    "rejection_reasons": dict(Counter(r["reason"] for r in relevant
                                                                   if r["capability_id"] == cap and not r["admitted"]))}
                              for cap in sorted({r["capability_id"] for r in relevant})}}
    write_json(output / "COUNTS.json", counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--upstream", type=Path, required=True)
    b.add_argument("--pool", type=Path, required=True)
    b.add_argument("--licenses", type=Path, required=True)
    q = sub.add_parser("qualify")
    q.add_argument("--pool", type=Path, required=True)
    q.add_argument("--image", required=True)
    q.add_argument("--logs", type=Path, required=True)
    q.add_argument("--jobs", type=int, default=8)
    q.add_argument("--resume", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--selection", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        licenses = json.loads(args.licenses.read_text())
        for info in licenses.values():
            info["license_text_path"] = str((args.licenses.parent / info["license_text_path"]).resolve())
        build(args.upstream, args.pool, licenses)
    elif args.command == "qualify":
        qualify(args.pool, args.image, args.logs, args.jobs, args.resume)
    else:
        publish(args.staging, args.output, json.loads(args.selection.read_text()) if args.selection else None)


if __name__ == "__main__":
    main()
