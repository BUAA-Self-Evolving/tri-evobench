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
import time
from pathlib import Path

import l2_v5_sra as legacy
from l2_v5_native_sources import tree_sha256
from trievo_bench.container_resources import (ContainerResourceError, docker_resource_capability_preflight,
                                             require_container_resources, resource_enforcement_mode, evaluate_resource_enforcement)

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
    ledger = []
    index_rows = []
    for line in (pool / "INDEX.jsonl").read_text().splitlines():
        row = json.loads(line)
        row.update(resource_enforcement="pending", resource_readback={}, resource_observation_provenance={"reason": "qualification not run"})
        index_rows.append(row)
        task = pool / row["task_dir"]
        record = json.loads((task / "pool_task.json").read_text())
        helper = ROOT / "tools/l2_v5_sra_strip_think.py"
        shutil.copy2(helper, task / "evaluation_assets/sra_native/strip_think.py")
        (task / "evaluation_assets/source_verifier.py").write_text(r3_verifier_text())
        record.update(resource_enforcement="pending", resource_readback={}, resource_observation_provenance={"reason": "qualification not run"})
        record["runtime"]["environment_version"] = "l2_environment/1.0"
        record["qualification"]["notes"].extend([
            "dedicated SR-Agents NumPy>=2 environment; unchanged native evaluator",
            "fresh container per authoritative fixture and per private diagnostic shadow",
            "r3 carrier uses source-exact upstream think-tag helper; legacy r2 omitted post-tag whitespace regex",
            "red admission requires collected tests and an assertion failure without test/infrastructure errors",
        ])
        record["license_note"] = "SR-Agents repository: MIT; BigCodeBench dataset: Apache-2.0."
        row["license_note"] = record["license_note"]
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
        record["adaptations"] = ledger[-1]["adaptations"]
        write_json(task / "pool_task.json", record)
    (pool / "ADAPTATIONS.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index_rows))


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


def upstream_red_valid(authority: dict) -> bool:
    """Use upstream FAIL semantics, including candidate execution/test errors."""
    for report in (authority,):
        if not report.get("transport_verified") or report.get("transport_error"):
            return False
        try:
            require_container_resources(report.get("resource_readback") or {})
        except ContainerResourceError:
            return False
        result = report.get("verifier", {})
        if (result.get("status") != "fail" or result.get("score") != 0
                or result.get("details", {}).get("result") != "fail"):
            return False
    return True


def placement_sources(package: Path) -> list[str]:
    return sorted({"sr_agents:" + task["source_basis"]["source_task_id"]
                   for path in package.glob("streams/*/*/task.json")
                   if (task := json.loads(path.read_text())).get("source_basis", {}).get("benchmark_id") == "sr_agents"})


def prepare_recipes(pool: Path, previous: Path) -> None:
    """Materialize one shared locked recipe; unrun sources remain recipe-ready."""
    protect_output(pool, previous)
    context = pool / "environment"
    context.mkdir(parents=True, exist_ok=True)
    files = ["Dockerfile.sra-r3", "sra-build-requirements.in", "sra-build-requirements.lock",
             "sra-requirements.in", "sra-requirements.lock", "sra-nltk-assets.json",
             "sra-install-assets.py", "sra-BIGCODEBENCH-LICENSE", "sra-NLTK-DATA-LICENSE"]
    for name in files:
        shutil.copy2(ROOT / "sandbox/l2-v5" / name, context / ("Dockerfile" if name == "Dockerfile.sra-r3" else name))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(context.iterdir()) if p.is_file()}
    environment = {"kind": "shared_dockerfile", "recipe_path": "../environment/Dockerfile",
                   "context_path": "../environment", "recipe_sha256": hashes["Dockerfile"],
                   "context_sha256": tree_sha256(context),
                   "context_file_hashes": hashes, "shared_environment_id": VERSION}
    rows = [json.loads(line) for line in (pool / "INDEX.jsonl").read_text().splitlines()]
    for row in rows:
        task = pool / row["task_dir"]
        old_red = previous / row["task_dir"] / "fixtures/known_broken/response.txt"
        shutil.copy2(old_red, task / "fixtures/known_broken/response.txt")
        path = task / "pool_task.json"
        record = json.loads(path.read_text())
        record["environment"] = environment
        fields = {"resource_enforcement": "unobserved", "resource_readback": {},
                  "resource_observation_provenance": {"reason": "recipe only; no source qualification in current scope"}}
        record.update(fields)
        record["runtime"].update(fields, task_image=None, task_image_digest=None)
        record["runtime"].pop("image_archive", None)
        record["qualification"].update(fields, admitted=False, stage="recipe_ready", reason=None,
                                        image_digest=None, known_correct=None, known_broken=None)
        record["qualification"].pop("private_diagnostic_path", None)
        record["qualification"]["notes"] = [note for note in record["qualification"].get("notes", [])
                                               if not note.startswith("red admission requires")]
        record["qualification"]["notes"].append("fixed old-pool red uses upstream FAIL semantics; no candidate search")
        record["adaptations"] = [a for a in record.get("adaptations", []) if a.get("kind") != "resource_enforcement_waiver"]
        row.update(fields, environment=environment, admitted=False, stage="recipe_ready", reason=None)
        row.pop("image_archive", None)
        write_json(path, record)
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    ledger = [json.loads(line) for line in (pool / "ADAPTATIONS.jsonl").read_text().splitlines()]
    for entry in ledger:
        entry.update(qualification="recipe_ready", **fields)
        entry["adaptations"] = [a for a in entry["adaptations"] if a.get("kind") != "resource_enforcement_waiver"]
        entry.pop("red_candidates", None)
    (pool / "ADAPTATIONS.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger))


def resource_header(stdout: str, *, nonce: str, response_sha256: str, shadow: bool,
                    enforcement_mode: str) -> dict:
    """Authenticate only the first stdout line; never scan candidate output."""
    lines = stdout.splitlines()
    if not lines:
        raise ValueError("missing first stdout resource header")
    header = json.loads(lines[0])
    if (not isinstance(header, dict)
            or header.get("schema_version") != "sra_resource_pre_execution/1.0"
            or header.get("execution_nonce") != nonce
            or header.get("response_sha256") != response_sha256
            or header.get("shadow") is not shadow):
        raise ValueError("first stdout resource header identity mismatch")
    snapshot = header.get("resource_readback")
    # Ignore serialized assessments/waivers: the host chooses current policy.
    assessment = evaluate_resource_enforcement(snapshot, enforcement_mode=enforcement_mode)
    if not isinstance(snapshot, dict):
        raise ContainerResourceError("missing resource snapshot in first stdout header")
    return dict(snapshot, **assessment)


def fixture_run(task: Path, fixture: Path, image: str, out: Path, *, shadow: bool,
                deadline: float | None = None) -> dict:
    """Transfer private fixtures through docker cp, never daemon-host binds.

    A fresh nonce, container-state/image/network readback, and copied result
    establish actual execution even where the default proxy no-ops docker exec.
    """
    out.mkdir(parents=True, exist_ok=True)
    selected_mode = resource_enforcement_mode()
    capability = docker_resource_capability_preflight(enforcement_mode=selected_mode)
    write_json(out / "resource-capability.json", capability)
    if not capability.get("allowed", capability["passed"]):
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
               "--entrypoint", "python", image, "/runner.py", "--resource-enforcement", selected_mode]
    if shadow:
        command.append("--shadow")
    commands = []
    report = {"transport_verified": False}
    trusted_resources = None
    expected_hash = hashlib.sha256((fixture / "response.txt").read_bytes()).hexdigest()
    provenance = {"kind": "first_stdout_pre_execution_header", "container_name": container_name,
                  "image_id": image, "execution_nonce": nonce, "role": "shadow" if shadow else "authoritative"}

    def call(arguments, timeout=60):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("source exceeded 45-minute qualification time box")
            timeout = min(timeout, remaining)
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
        start_error = None
        try:
            result = call(["docker", "start", "-a", container_name], timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            result = exc
            start_error = exc
        stdout = result.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if not stdout:
            stdout = call(["docker", "logs", container_name]).stdout
        (out / "stdout.log").write_text(stdout)
        stderr = result.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        (out / "stderr.log").write_text(stderr)
        trusted_resources = resource_header(stdout, nonce=nonce, response_sha256=expected_hash,
                                            shadow=shadow, enforcement_mode=selected_mode)
        require_container_resources(trusted_resources, enforcement_mode=selected_mode)
        if start_error is not None:
            raise start_error
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
            if (payload.get("execution_nonce") != nonce or payload.get("shadow") is not shadow
                    or payload.get("response_sha256") != expected_hash):
                raise ValueError("fixture execution readback identity mismatch")
            payload["resource_readback"] = trusted_resources
            report = payload
            report.update(transport_verified=True, container_exit_code=state["ExitCode"],
                          actual_image_id=actual_image, network_readback=network, host_config_readback=host_config)
    except (subprocess.SubprocessError, OSError, ValueError, ContainerResourceError) as exc:
        report = {"transport_verified": False, "transport_error": type(exc).__name__ + ": " + str(exc)}
        if isinstance(exc, subprocess.CalledProcessError):
            report["stderr"] = (exc.stderr or "")[-12000:]
    finally:
        try:
            cleanup = subprocess.run(["docker", "rm", "-f", container_name],
                                     capture_output=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            report["cleanup_warning"] = "cleanup warning: docker rm timed out after 30s: " + container_name
        else:
            if cleanup.returncode:
                report["cleanup_warning"] = ("cleanup warning: docker rm rc=" +
                                             str(cleanup.returncode) + ": " + container_name)
    if trusted_resources is not None:
        report.update(resource_readback=trusted_resources,
                      resource_enforcement=trusted_resources["resource_enforcement"],
                      requested_resources=trusted_resources["requested_resources"],
                      observed_resources=trusted_resources["observed_resources"],
                      resource_enforcement_reason=trusted_resources["reason"],
                      resource_waiver=trusted_resources["waiver"],
                      resource_observation_provenance=provenance)
    report["resource_enforcement_mode"] = selected_mode
    report.update(command=command, transport_commands=commands, network="none", image_id=image,
                  expected_nonce=nonce, workspace_transport="docker_cp_private_container")
    if report["transport_verified"]:
        write_json(out / "result.json", report)
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


def qualify_one(task: Path, image: str, artifacts: Path, old_red_pool: Path | None = None) -> dict:
    deadline = time.monotonic() + 45 * 60
    try:
        return _qualify_one(task, image, artifacts, deadline, old_red_pool)
    except TimeoutError:
        observed = next((report for path in sorted((artifacts / task.name).glob("**/report.json"))
                         if (report := json.loads(path.read_text())).get("resource_readback")), {})
        return {"task_dir": task.name, "fixtures": {}, "admitted": False,
                "resource_report": observed,
                "stage": "deferred", "reason": "source exceeded 45-minute qualification time box"}


def _qualify_one(task: Path, image: str, artifacts: Path, deadline: float,
                 old_red_pool: Path | None = None) -> dict:
    def run(fixture: Path, out: Path, *, shadow: bool) -> dict:
        if time.monotonic() >= deadline:
            raise TimeoutError("source time box expired")
        report = fixture_run(task, fixture, image, out, shadow=shadow, deadline=deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("source time box expired")
        return report

    result = {"task_dir": task.name, "fixtures": {}}
    fixture = task / "fixtures/known_correct"
    out = artifacts / task.name / "known_correct"
    authority = run(fixture, out / "authoritative", shadow=False)
    diagnostic = run(fixture, out / "diagnostic", shadow=True)
    result["fixtures"]["known_correct"] = {
        "authoritative": authority, "diagnostic": diagnostic,
        "valid": diagnostic_valid(authority, diagnostic, red=False)}
    # A gold infrastructure failure is not evidence about a red submission.
    if not result["fixtures"]["known_correct"]["valid"]:
        result["admitted"] = False
        result["reason"] = "gold_not_native_green_with_collected_tests"
        return result
    attempts = []
    candidates = ([("old_pool_known_broken", (old_red_pool / task.name / "fixtures/known_broken/response.txt").read_text())]
                  if old_red_pool is not None else red_candidates(task))
    for label, response in candidates:
        out = artifacts / task.name / "known_broken" / label
        fixture = out / "fixture"
        fixture.mkdir(parents=True, exist_ok=True)
        if old_red_pool is not None:
            shutil.copy2(old_red_pool / task.name / "fixtures/known_broken/response.txt", fixture / "response.txt")
        else:
            (fixture / "response.txt").write_text(response)
        authority = run(fixture, out / "authoritative", shadow=False)
        diagnostic = {} if old_red_pool is not None else run(fixture, out / "diagnostic", shadow=True)
        valid = (upstream_red_valid(authority) if old_red_pool is not None
                 else diagnostic_valid(authority, diagnostic, red=True))
        current = {"authoritative": authority, "diagnostic": diagnostic,
                   "valid": valid, "candidate": label}
        attempts.append({"candidate": label, "valid": valid})
        result["fixtures"]["known_broken"] = current
        if valid:
            shutil.copy2(fixture / "response.txt", task / "fixtures/known_broken/response.txt")
            current["response_sha256"] = hashlib.sha256((fixture / "response.txt").read_bytes()).hexdigest()
            break
    result["red_candidates"] = attempts
    result["admitted"] = all(f["valid"] for f in result["fixtures"].values())
    if not result["admitted"] and old_red_pool is not None:
        result["reason"] = ("old_known_broken_passed" if authority.get("verifier", {}).get("status") == "pass"
                            else "old_known_broken_not_native_fail_or_infrastructure_error")
    return result


def qualify(pool: Path, image: str, artifacts: Path, jobs: int, only: list[str],
            image_archives: Path | None = None, old_red_pool: Path | None = None,
            r2_package: Path | None = None) -> None:
    protect_output(pool)
    artifacts.mkdir(parents=True, exist_ok=True)
    capability = docker_resource_capability_preflight()
    write_json(artifacts / "RESOURCE_PREFLIGHT.json", capability)
    if not capability.get("allowed", capability["passed"]):
        raise ContainerResourceError("SRA qualification blocked before image/fixture work: " + json.dumps(capability))
    image_id = legacy.image_digest(image)
    rows = [json.loads(line) for line in (pool / "INDEX.jsonl").read_text().splitlines()]
    if r2_package is not None:
        only = placement_sources(r2_package)
    selected = [r for r in rows if not only or r["task_dir"] in only or r["source_id"] in only]
    artifacts.mkdir(parents=True, exist_ok=True)
    environment_files = ["Dockerfile.sra-r3", "sra-requirements.in", "sra-requirements.lock",
                         "sra-build-requirements.lock", "sra-nltk-assets.json", "sra-install-assets.py"]
    identity = {"resource_enforcement_mode": resource_enforcement_mode(), "protocol": VERSION, "image_tag": image, "image_id": image_id,
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
    if (pool / "ENVIRONMENT.json").exists():
        previous_identity = json.loads((pool / "ENVIRONMENT.json").read_text())
        for key in ("build_adaptations", "actual_build_recipe", "resolved_dpkg_packages", "resolved_python_packages"):
            if key in previous_identity:
                identity[key] = previous_identity[key]
    identity["qualification_scope"] = [row["source_id"] for row in selected]
    identity["red_policy"] = "old_pool_upstream_fail" if old_red_pool is not None else "assertion_candidate_search"
    write_json(artifacts / "ENVIRONMENT.json", identity)
    write_json(pool / "ENVIRONMENT.json", identity)
    with ThreadPoolExecutor(max_workers=jobs) as executor, (artifacts / "results.jsonl").open("w") as log:
        results = {}
        for result in executor.map(lambda r: qualify_one(pool / r["task_dir"], image_id, artifacts, old_red_pool), selected):
            results[result["task_dir"]] = result
            log.write(json.dumps(result) + "\n")
            log.flush()
    for row in rows:
        result = results.get(row["task_dir"])
        if result is None:
            continue
        path = pool / row["task_dir"] / "pool_task.json"
        record = json.loads(path.read_text())
        record.pop("license", None)
        record["license_note"] = "SR-Agents repository: MIT; BigCodeBench dataset: Apache-2.0."
        row["license_note"] = record["license_note"]
        record["runtime"].update(task_image=image, task_image_digest=image_id)
        resource_report = next((data[side] for data in result["fixtures"].values()
            for side in ("authoritative", "diagnostic") if data[side].get("resource_readback")), result.get("resource_report", {}))
        resource_fields = {"resource_enforcement": resource_report.get("resource_enforcement", "unobserved"),
            "resource_readback": resource_report.get("resource_readback", {}),
            "resource_observation_provenance": resource_report.get("resource_observation_provenance", {"reason": "fixture transport failed before actual readback"})}
        record.update(resource_fields); record["runtime"].update(resource_fields); row.update(resource_fields)
        record["adaptations"] = [item for item in record.get("adaptations", []) if item.get("kind") != "resource_enforcement_waiver"]
        if record["resource_enforcement"] == "declared_not_enforced":
            record["adaptations"].append({"kind": "resource_enforcement_waiver", "mode": "declared_only",
                "reason": "explicit user approval W2-phase2-resume.md", **resource_fields})
        q = record["qualification"]
        q.update(resource_fields)
        q.update(image_digest=image_id, admitted=result["admitted"],
                 known_correct=None, known_broken=None,
                 stage=result.get("stage", "qualified"), reason=result.get("reason"),
                 qualification_protocol=VERSION,
                 red_policy=identity["red_policy"],
                 private_diagnostic_path=str((artifacts / row["task_dir"]).resolve()))
        for name, value in result["fixtures"].items():
            authoritative = value["authoritative"].get("verifier", {})
            q[name] = {"status": authoritative.get("status", "error"),
                       "score": authoritative.get("score"), "meaningful": value["valid"]}
        row.update(admitted=result["admitted"], stage=result.get("stage", "qualified"),
                   reason=None if result["admitted"] else result.get("reason") or {
                       name: {"authoritative": value["authoritative"].get("verifier", {}),
                              "diagnostic_valid": value["valid"]}
                       for name, value in result["fixtures"].items() if not value["valid"]})
        write_json(path, record)
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ledger = [json.loads(line) for line in (pool / "ADAPTATIONS.jsonl").read_text().splitlines()]
    for entry in ledger:
        task_dir = entry["source_id"].replace(":", "__")
        if task_dir in results:
            entry["qualification"] = ("admitted" if results[task_dir]["admitted"] else
                                      results[task_dir].get("stage", "rejected"))
            record = json.loads((pool / task_dir / "pool_task.json").read_text())
            entry.update({key: record[key] for key in ("resource_enforcement", "resource_readback", "resource_observation_provenance")})
            entry["adaptations"] = [item for item in entry["adaptations"] if item.get("kind") != "resource_enforcement_waiver"]
            if record["resource_enforcement"] == "declared_not_enforced":
                entry["adaptations"].append({"kind": "resource_enforcement_waiver", "mode": "declared_only",
                    "reason": "explicit user approval W2-phase2-resume.md", "resource_readback": record["resource_readback"],
                    "resource_observation_provenance": record["resource_observation_provenance"]})
            entry["red_candidates"] = results[task_dir].get("red_candidates", [])
    (pool / "ADAPTATIONS.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))
    if image_archives is not None:
        archive_image(pool, image, image_archives)


def archive_image(pool: Path, image: str, archive_dir: Path) -> None:
    """Save the shared qualified image once; retain its path/hash before rmi."""
    protect_output(pool)
    if not image.startswith("trievo-r3-"):
        raise ValueError("only task-owned trievo-r3-* image tags may be offloaded")
    image_id = legacy.image_digest(image)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir.resolve() / (image_id.removeprefix("sha256:") + ".tar")
    partial = archive.with_suffix(".tar.partial")
    subprocess.run(["docker", "save", "--output", str(partial), image_id], check=True)
    with partial.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    partial.replace(archive)
    saved = {"image_id": image_id, "archive_path": str(archive), "archive_sha256": digest}
    rows = [json.loads(line) for line in (pool / "INDEX.jsonl").read_text().splitlines()]
    for row in rows:
        path = pool / row["task_dir"] / "pool_task.json"
        record = json.loads(path.read_text())
        if record["runtime"].get("task_image_digest") == image_id:
            record["runtime"]["image_archive"] = saved
            row["image_archive"] = saved
            write_json(path, record)
    (pool / "INDEX.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    environment = json.loads((pool / "ENVIRONMENT.json").read_text())
    environment["image_archive"] = saved
    write_json(pool / "ENVIRONMENT.json", environment)
    if legacy.image_digest(image) != image_id:
        raise ValueError("image tag changed during archival; retaining the loaded image")
    subprocess.run(["docker", "rmi", image], check=True)


def compare(pool: Path, previous: Path, package: Path, out: Path) -> None:
    protect_output(pool, previous)
    def records(root):
        return {r["source_id"]: (r, json.loads((root / r["task_dir"] / "pool_task.json").read_text()))
                for r in (json.loads(s) for s in (root / "INDEX.jsonl").read_text().splitlines())}
    old, new = records(previous), records(pool)
    diffs = []
    placements = []
    for path in sorted(package.glob("streams/*/*/task.json")):
        task = json.loads(path.read_text())
        source = task.get("source_basis", {})
        if source.get("benchmark_id") != "sr_agents":
            continue
        sid = "sr_agents:" + source["source_task_id"]
        placements.append({"stream_id": task["stream_id"], "turn_id": task["turn_id"], "source_id": sid})
    for sid in sorted(old.keys() | new.keys()):
        a = old.get(sid, ({}, {}))[1]; b = new.get(sid, ({}, {}))[1]
        diffs.append({"source_id": sid, "old_admitted": a.get("qualification", {}).get("admitted"),
                      "new_admitted": b.get("qualification", {}).get("admitted"),
                      "old_stage": a.get("qualification", {}).get("stage"),
                      "new_stage": b.get("qualification", {}).get("stage"),
                      "new_reason": b.get("qualification", {}).get("reason") or new.get(sid, ({}, {}))[0].get("reason"),
                      "new_green": b.get("qualification", {}).get("known_correct"),
                      "new_red": b.get("qualification", {}).get("known_broken"),
                      "old_image": a.get("runtime", {}).get("task_image_digest"),
                      "new_image": b.get("runtime", {}).get("task_image_digest")})
    by_id = {r["source_id"]: r for r in diffs}
    placement_diff = [dict(p, **{k: v for k, v in by_id[p["source_id"]].items() if k != "source_id"}) for p in placements]
    qualified = [r for r in diffs if r["new_stage"] != "recipe_ready"]
    summary = {"sources": len(new), "old_admitted": sum(bool(r[1].get("qualification", {}).get("admitted")) for r in old.values()),
               "recipe_ready": sum(r["new_stage"] == "recipe_ready" for r in diffs),
               "requalified": len(qualified), "admitted": sum(bool(r["new_admitted"]) for r in qualified),
               "deferred": sum(r["new_stage"] == "deferred" for r in qualified),
               "retained": sum(bool(r["old_admitted"] and r["new_admitted"]) for r in qualified),
               "gained": sum(bool(not r["old_admitted"] and r["new_admitted"]) for r in qualified),
               "lost": sum(bool(r["old_admitted"] and not r["new_admitted"]) for r in qualified),
               "scope": "existing r2 SRA placements only; recipe_ready is not a failed qualification"}
    summary["rejected"] = summary["requalified"] - summary["admitted"] - summary["deferred"]
    write_json(out / "source-diff.json", diffs)
    write_json(out / "r2-placement-diff.json", placement_diff)
    write_json(out / "r2-intra-placement-diff.json", [r for r in placement_diff if r["stream_id"].startswith("intra_")])
    write_json(out / "summary.json", summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build"); b.add_argument("--pool", type=Path, required=True); b.add_argument("--upstream", type=Path, required=True)
    q = sub.add_parser("qualify"); q.add_argument("--pool", type=Path, required=True); q.add_argument("--image", required=True); q.add_argument("--artifacts", type=Path, required=True); q.add_argument("--jobs", type=int, default=4); q.add_argument("--only", nargs="*", default=[])
    q.add_argument("--image-archives", type=Path, help="Save shared image here after qualification, record SHA, then rmi its task-owned tag")
    q.add_argument("--old-red-pool", type=Path, help="Use exact previous known_broken once with upstream FAIL semantics")
    q.add_argument("--r2-package", type=Path, help="Qualify only SR-Agents sources placed in this existing package")
    p = sub.add_parser("prepare-recipes"); p.add_argument("--pool", type=Path, required=True); p.add_argument("--previous", type=Path, required=True)
    d = sub.add_parser("compare"); d.add_argument("--pool", type=Path, required=True); d.add_argument("--previous", type=Path, required=True); d.add_argument("--package", type=Path, required=True); d.add_argument("--out", type=Path, required=True)
    args = vars(parser.parse_args()); command = args.pop("command")
    {"build": build, "qualify": qualify, "compare": compare, "prepare-recipes": prepare_recipes}[command](**args)


if __name__ == "__main__":
    main()
