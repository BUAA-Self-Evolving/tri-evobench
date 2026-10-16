#!/usr/bin/env python3
"""Qualify all SR-Agents BigCodeBench sources in a dedicated locked environment.

Each authoritative fixture and each diagnostic shadow gets a fresh offline
container. Native evaluator files and reward semantics are unchanged.
"""
from __future__ import annotations

import argparse
import ast
import uuid
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import tempfile
import subprocess
from pathlib import Path

import l2_v5_sra as legacy
from trievo_bench.container_resources import (ContainerResourceError, docker_resource_capability_preflight,
                                             require_container_resources)

ROOT = Path(__file__).resolve().parents[1]
VERSION = "sra_r3_environment/1.0"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def protect_output(pool: Path, previous: Path | None = None) -> None:
    resolved = pool.resolve()
    if "l2v5_pool" in resolved.parts or any(x.startswith("L2_Demo_V5") for x in resolved.parts):
        raise ValueError("refusing to modify a released pool/package")
    if previous and resolved == previous.resolve():
        raise ValueError("output pool must differ from previous pool")


def r3_verifier_text() -> str:
    text = legacy.VERIFIER
    start = text.index('_THINK_CLOSED_RE =')
    end = text.index('def verify(', start)
    return text[:start] + 'from strip_think import strip_think_tags\n\n\n' + text[end:]


def build(pool: Path, upstream: Path) -> None:
    protect_output(pool)
    revision = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                              check=True, text=True, capture_output=True).stdout.strip()
    if revision != legacy.REVISION:
        raise ValueError("SR-Agents revision mismatch: " + revision)
    original_upstream = legacy.UPSTREAM
    try:
        legacy.UPSTREAM = upstream.resolve()
        legacy.build(pool)
    finally:
        legacy.UPSTREAM = original_upstream
    license_text = (upstream / "LICENSE").read_text()
    ledger = []
    for line in (pool / "INDEX.jsonl").read_text().splitlines():
        row = json.loads(line)
        task = pool / row["task_dir"]
        record = json.loads((task / "pool_task.json").read_text())
        helper = ROOT / "tools/l2_v5_sra_strip_think.py"
        shutil.copy2(helper, task / "evaluation_assets/sra_native/strip_think.py")
        (task / "evaluation_assets/source_verifier.py").write_text(r3_verifier_text())
        record["runtime"]["environment_version"] = "l2_environment/1.0"
        record["qualification"]["notes"].extend([
            "dedicated SR-Agents NumPy>=2 environment; unchanged native evaluator",
            "fresh container per authoritative fixture and per private diagnostic shadow",
            "r3 carrier uses source-exact upstream think-tag helper; legacy r2 omitted post-tag whitespace regex",
            "red admission requires collected tests and an assertion failure without test/infrastructure errors",
        ])
        record["license"] = {"repository": "MIT", "dataset": "Apache-2.0",
                             "dataset_url": "https://huggingface.co/datasets/bigcode/bigcodebench",
                             "notice_path": "evaluation_assets/SOURCE_LICENSE.txt"}
        (task / "evaluation_assets/SOURCE_LICENSE.txt").write_text(license_text)
        shutil.copy2(ROOT / "sandbox/l2-v5/sra-BIGCODEBENCH-LICENSE", task / "evaluation_assets/BIGCODEBENCH_LICENSE.txt")
        hashes = {str(f.relative_to(task)): hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in (task / "evaluation_assets").rglob("*") if f.is_file()}
        record["source_asset_hashes"] = hashes
        write_json(task / "pool_task.json", record)
        ledger.append({"source_id": row["source_id"], "adaptations": [
            {"kind": "environment", "reason": "replace shared union image with locked source-compatible evaluator closure"},
            {"kind": "diagnostics", "reason": "shadow instrumentation in a separate private container; authoritative reward unchanged"},
            {"kind": "legacy_wrapper_alignment", "source": "src/sragents/llm.py:strip_think_tags",
             "revision": legacy.REVISION, "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
             "reason": "restore exact upstream post-think-tag whitespace handling for r3; legacy r2 untouched"}],
                       "qualification": "pending"})
    (pool / "ADAPTATIONS.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))


def diagnostic_valid(authority: dict, shadow: dict, *, red: bool) -> bool:
    if not authority.get("transport_verified") or not shadow.get("transport_verified"):
        return False
    for evidence in (authority, shadow):
        try:
            require_container_resources(evidence.get("resource_readback") or {})
        except ContainerResourceError:
            return False
    expected = "fail" if red else "pass"
    for evidence in (authority, shadow):
        result = evidence.get("verifier", {})
        # The wrapper also calls a native timeout "fail". Only actual native
        # FAIL is assertion red; TIMEOUT never qualifies as meaningful failure.
        if (result.get("status") != expected or result.get("details", {}).get("result") != expected
                or result.get("score") != (0.0 if red else 1.0)):
            return False
    started = shadow.get("tests_started", 0)
    return (started > 0 and shadow.get("tests_completed", 0) == started
            and shadow.get("test_errors", 0) == 0
            and shadow.get("execution_errors", 0) == 0
            and (shadow.get("assertion_failures", 0) > 0 if red
                 else shadow.get("assertion_failures", 0) == 0 and shadow.get("tests_successful", 0) > 0))


def fixture_run(task: Path, fixture: Path, image: str, out: Path, *, shadow: bool) -> dict:
    """Transfer private fixtures through docker cp, never daemon-host binds.

    A fresh nonce, container-state/image/network readback, and copied result
    establish actual execution even where the default proxy no-ops docker exec.
    """
    out.mkdir(parents=True, exist_ok=True)
    capability = docker_resource_capability_preflight()
    write_json(out / "resource-capability.json", capability)
    if not capability["passed"]:
        report = {"transport_verified": False, "resource_preflight_rejected": True,
                  "resource_capability": capability,
                  "transport_error": "Docker daemon cannot enforce required cgroups; no fixture container launched"}
        write_json(out / "report.json", report)
        return report
    container_name = "l2-sra-r3-" + uuid.uuid4().hex[:16]
    nonce = uuid.uuid4().hex
    command = ["docker", "create", "--name", container_name, "--network", "none",
               "--cpus", "2", "--memory", "4g", "--user", "0",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
               "-e", "MPLBACKEND=Agg", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-e", "L2_QUALIFICATION_NONCE=" + nonce,
               "--entrypoint", "python", image, "/runner.py"]
    if shadow:
        command.append("--shadow")
    commands = []
    report = {"transport_verified": False}

    def call(arguments, timeout=60):
        commands.append(arguments)
        value = subprocess.run(arguments, capture_output=True, text=True,
                               timeout=timeout, check=True)
        return value

    try:
        call(command)
        call(["docker", "cp", str(task.resolve()), container_name + ":/task"])
        call(["docker", "cp", str(fixture.resolve()), container_name + ":/fixture"])
        call(["docker", "cp", str(ROOT / "trievo_bench/container_resources.py"),
              container_name + ":/container_resources.py"])
        call(["docker", "cp", str(ROOT / "tools/l2_v5_sra_diagnostics.py"),
              container_name + ":/runner.py"])
        result = call(["docker", "start", "-a", container_name], timeout=600)
        (out / "stdout.log").write_text(result.stdout)
        (out / "stderr.log").write_text(result.stderr)
        inspection = call(["docker", "inspect", "--format",
                           "{{json .State}}|{{.Image}}|{{.HostConfig.NetworkMode}}|{{json .HostConfig}}", container_name])
        raw_state, actual_image, network, raw_host_config = inspection.stdout.strip().split("|", 3)
        state = json.loads(raw_state)
        host_config = json.loads(raw_host_config)
        if (host_config.get("PidsLimit") != 256
                or "ALL" not in host_config.get("CapDrop", [])
                or "no-new-privileges" not in host_config.get("SecurityOpt", [])
                or host_config.get("Memory") != 4 * 1024 ** 3
                or host_config.get("NanoCpus") != 2 * 10 ** 9):
            raise ValueError("container resource/security policy mismatch")
        if state.get("Running") or state.get("ExitCode") != 0 or actual_image != image or network != "none":
            raise ValueError("unexpected container state/image/network: " + inspection.stdout.strip())
        with tempfile.TemporaryDirectory(prefix="sra-readback-", dir=out) as staged:
            destination = Path(staged) / "result.json"
            call(["docker", "cp", container_name + ":/out/result.json", str(destination)])
            payload = json.loads(destination.read_text())
            expected_hash = hashlib.sha256((fixture / "response.txt").read_bytes()).hexdigest()
            if (payload.get("execution_nonce") != nonce or payload.get("shadow") is not shadow
                    or payload.get("response_sha256") != expected_hash):
                raise ValueError("fixture execution readback identity mismatch")
            require_container_resources(payload.get("resource_readback") or {})
            report = payload
            report.update(transport_verified=True, container_exit_code=state["ExitCode"],
                          actual_image_id=actual_image, network_readback=network, host_config_readback=host_config)
            shutil.copy2(destination, out / "result.json")
    except (subprocess.SubprocessError, OSError, ValueError, ContainerResourceError) as exc:
        report = {"transport_verified": False, "transport_error": type(exc).__name__ + ": " + str(exc)}
        if "payload" in locals():
            report["resource_readback"] = payload.get("resource_readback")
        if isinstance(exc, subprocess.CalledProcessError):
            report["stderr"] = (exc.stderr or "")[-12000:]
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30)
    report.update(command=command, transport_commands=commands, network="none", image_id=image,
                  expected_nonce=nonce, workspace_transport="docker_cp_private_container")
    write_json(out / "report.json", report)
    return report


def red_candidates(task: Path):
    """Deterministic wrong submissions; never modify evaluator/test source."""
    item = json.loads((task / "evaluation_assets/source_item.json").read_text())["eval_data"]
    prompt = item["code_prompt"]
    yield "return_none", prompt + "    return None\n"
    # Small mutations of the real implementation preserve its output types and
    # side effects better than a constant stub. Qualification proves actual red.
    source = prompt + item["answer"]
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or type(node.value) not in (int, float):
            continue
        if not hasattr(node, "end_lineno"):
            continue
        lines = source.splitlines(keepends=True)
        before = sum(len(x) for x in lines[:node.lineno - 1]) + node.col_offset
        after = sum(len(x) for x in lines[:node.end_lineno - 1]) + node.end_col_offset
        value = node.value + 1
        yield "numeric_literal_" + str(count), source[:before] + repr(value) + source[after:]
        count += 1
        if count == 12:
            break
    for value in (0, False, "", [], {}, (0, 0)):
        yield "constant_" + type(value).__name__, prompt + "    return " + repr(value) + "\n"


def qualify_one(task: Path, image: str, artifacts: Path) -> dict:
    result = {"task_dir": task.name, "fixtures": {}}
    fixture = task / "fixtures/known_correct"
    out = artifacts / task.name / "known_correct"
    authority = fixture_run(task, fixture, image, out / "authoritative", shadow=False)
    diagnostic = fixture_run(task, fixture, image, out / "diagnostic", shadow=True)
    result["fixtures"]["known_correct"] = {
        "authoritative": authority, "diagnostic": diagnostic,
        "valid": diagnostic_valid(authority, diagnostic, red=False)}
    # A gold infrastructure failure is not evidence about a red submission.
    if not result["fixtures"]["known_correct"]["valid"]:
        result["admitted"] = False
        result["reason"] = "gold_not_native_green_with_collected_tests"
        return result
    attempts = []
    for label, response in red_candidates(task):
        out = artifacts / task.name / "known_broken" / label
        fixture = out / "fixture"
        fixture.mkdir(parents=True, exist_ok=True)
        (fixture / "response.txt").write_text(response)
        authority = fixture_run(task, fixture, image, out / "authoritative", shadow=False)
        diagnostic = fixture_run(task, fixture, image, out / "diagnostic", shadow=True)
        valid = diagnostic_valid(authority, diagnostic, red=True)
        current = {"authoritative": authority, "diagnostic": diagnostic,
                   "valid": valid, "candidate": label}
        attempts.append({"candidate": label, "valid": valid})
        result["fixtures"]["known_broken"] = current
        if valid:
            shutil.copy2(fixture / "response.txt", task / "fixtures/known_broken/response.txt")
            current["response_sha256"] = hashlib.sha256(response.encode()).hexdigest()
            break
    result["red_candidates"] = attempts
    result["admitted"] = all(f["valid"] for f in result["fixtures"].values())
    return result


def qualify(pool: Path, image: str, artifacts: Path, jobs: int, only: list[str]) -> None:
    protect_output(pool)
    artifacts.mkdir(parents=True, exist_ok=True)
    capability = docker_resource_capability_preflight()
    write_json(artifacts / "RESOURCE_PREFLIGHT.json", capability)
    if not capability["passed"]:
        raise ContainerResourceError("SRA qualification blocked before image/fixture work: " + json.dumps(capability))
    image_id = legacy.image_digest(image)
    rows = [json.loads(line) for line in (pool / "INDEX.jsonl").read_text().splitlines()]
    selected = [r for r in rows if not only or r["task_dir"] in only or r["source_id"] in only]
    artifacts.mkdir(parents=True, exist_ok=True)
    environment_files = ["Dockerfile.sra-r3", "sra-requirements.in", "sra-requirements.lock",
                         "sra-build-requirements.lock", "sra-nltk-assets.json", "sra-install-assets.py"]
    identity = {"protocol": VERSION, "image_tag": image, "image_id": image_id,
                "dependency_hashes": {name: hashlib.sha256((ROOT / "sandbox/l2-v5" / name).read_bytes()).hexdigest()
                                      for name in environment_files},
                "qualifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "diagnostic_sha256": hashlib.sha256((ROOT / "tools/l2_v5_sra_diagnostics.py").read_bytes()).hexdigest(),
                "strip_think_sha256": hashlib.sha256((ROOT / "tools/l2_v5_sra_strip_think.py").read_bytes()).hexdigest(),
                "upstream_revision": legacy.REVISION,
                "network": "none", "cpus": 2, "memory": "4g",
                "pids_limit": 256, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"],
                "fixture_isolation": "fresh_container_per_authoritative_and_shadow",
                "transport": "docker_cp_private_container"}
    write_json(artifacts / "ENVIRONMENT.json", identity)
    write_json(pool / "ENVIRONMENT.json", identity)
    with ThreadPoolExecutor(max_workers=jobs) as executor, (artifacts / "results.jsonl").open("w") as log:
        results = {}
        for result in executor.map(lambda r: qualify_one(pool / r["task_dir"], image_id, artifacts), selected):
            results[result["task_dir"]] = result
            log.write(json.dumps(result) + "\n")
            log.flush()
    for row in rows:
        result = results.get(row["task_dir"])
        if result is None:
            continue
        path = pool / row["task_dir"] / "pool_task.json"
        record = json.loads(path.read_text())
        record["runtime"].update(task_image=image, task_image_digest=image_id)
        q = record["qualification"]
        q.update(image_digest=image_id, admitted=result["admitted"],
                 qualification_protocol=VERSION,
                 private_diagnostic_path=str((artifacts / row["task_dir"]).resolve()))
        for name, value in result["fixtures"].items():
            authoritative = value["authoritative"].get("verifier", {})
            q[name] = {"status": authoritative.get("status", "error"),
                       "score": authoritative.get("score"), "meaningful": value["valid"]}
        row.update(admitted=result["admitted"],
                   reason=None if result["admitted"] else {
                       name: {"authoritative": value["authoritative"].get("verifier", {}),
                              "diagnostic_valid": value["valid"]}
                       for name, value in result["fixtures"].items() if not value["valid"]})
        write_json(path, record)
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ledger = [json.loads(line) for line in (pool / "ADAPTATIONS.jsonl").read_text().splitlines()]
    for entry in ledger:
        task_dir = entry["source_id"].replace(":", "__")
        if task_dir in results:
            entry["qualification"] = "admitted" if results[task_dir]["admitted"] else "rejected"
            entry["red_candidates"] = results[task_dir].get("red_candidates", [])
    (pool / "ADAPTATIONS.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))


def compare(pool: Path, previous: Path, package: Path, out: Path) -> None:
    protect_output(pool, previous)
    def records(root):
        return {r["source_id"]: (r, json.loads((root / r["task_dir"] / "pool_task.json").read_text()))
                for r in (json.loads(s) for s in (root / "INDEX.jsonl").read_text().splitlines())}
    old, new = records(previous), records(pool)
    diffs = []
    used = set()
    placements = []
    for path in sorted(package.glob("streams/*/*/task.json")):
        task = json.loads(path.read_text())
        source = task.get("source_basis", {})
        if source.get("benchmark_id") != "sr_agents":
            continue
        sid = "sr_agents:" + source["source_task_id"]
        if task["stream_id"].startswith("intra_"):
            used.add(sid)
        placements.append({"stream_id": task["stream_id"], "turn_id": task["turn_id"], "source_id": sid})
    for sid in sorted(old.keys() | new.keys()):
        a = old.get(sid, ({}, {}))[1]; b = new.get(sid, ({}, {}))[1]
        diffs.append({"source_id": sid, "old_admitted": a.get("qualification", {}).get("admitted"),
                      "new_admitted": b.get("qualification", {}).get("admitted"),
                      "old_image": a.get("runtime", {}).get("task_image_digest"),
                      "new_image": b.get("runtime", {}).get("task_image_digest")})
    by_id = {r["source_id"]: r for r in diffs}
    counts = {}
    for sid, (row, record) in new.items():
        if record["qualification"]["admitted"] and sid not in used:
            cap = record["capability_hint"]["capability_id"]
            counts[cap] = counts.get(cap, 0) + 1
    write_json(out / "source-diff.json", diffs)
    write_json(out / "r2-placement-diff.json", [dict(p, **{k: v for k, v in by_id[p["source_id"]].items() if k != "source_id"}) for p in placements])
    write_json(out / "unused-intra-capability-counts.json", counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build"); b.add_argument("--pool", type=Path, required=True); b.add_argument("--upstream", type=Path, required=True)
    q = sub.add_parser("qualify"); q.add_argument("--pool", type=Path, required=True); q.add_argument("--image", required=True); q.add_argument("--artifacts", type=Path, required=True); q.add_argument("--jobs", type=int, default=4); q.add_argument("--only", nargs="*", default=[])
    d = sub.add_parser("compare"); d.add_argument("--pool", type=Path, required=True); d.add_argument("--previous", type=Path, required=True); d.add_argument("--package", type=Path, required=True); d.add_argument("--out", type=Path, required=True)
    args = vars(parser.parse_args()); command = args.pop("command")
    {"build": build, "qualify": qualify, "compare": compare}[command](**args)


if __name__ == "__main__":
    main()
