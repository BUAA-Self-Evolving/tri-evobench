#!/usr/bin/env python3
"""Red/green qualification of every Turn in a composed L2 V5 package.

For each Turn this runs the package-local ``evaluation_assets/verifier_cli.py``
inside the Turn's declared verifier image (``--network none``) twice: once with
the ``known_correct`` fixture as the submission (must return ``pass``) and once
with ``known_broken`` (must return ``fail``). Fixtures mirror either the Turn's
task directory (workspace bindings) or ``response.txt`` (inline final-response
bindings). Every fixture runs in a fresh container: native verifiers copy the
submission into fixed container paths such as /root and /app, so a shared
container would let the first submission leak into the second.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

FIXTURES = {"known_correct": "pass", "known_broken": "fail"}
DRIVER = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
from uuid import uuid4
w = Path("/w"); turn = w / "turn"; spec = json.loads((w / "spec.json").read_text())
fixture = sys.argv[1]
resource_readback = None
if spec.get("environment_version") or spec.get("native_environment"):
    from container_resources import require_container_resources
    resource_readback = require_container_resources(enforcement_mode=spec["resource_enforcement_mode"])
    print(json.dumps({"event": "trievo_resource_header", "nonce": spec["resource_nonce"],
                      "resource_readback": resource_readback}), flush=True)
src = turn / "evaluation_assets" / "fixtures" / fixture
ws = Path("/workspace") if spec.get("environment_version") else w / f"ws-{fixture}"
task_dir = ws / "tasks" / spec["turn_id"]
task_dir.mkdir(parents=True)
if spec["source"] == "interaction_record":
    binding = {"kind": "inline", "value": (src / "response.txt").read_text()}
else:
    shutil.copytree(src, task_dir, dirs_exist_ok=True)
    binding = {"kind": "path", "path": spec["path"]}
request = {"schema_version": "l2_native_verifier_request/1.0", "bindings": {"submission": binding}}
env = {**os.environ, "L2_VERIFIER_WORKSPACE": str(ws)}
if resource_readback is not None:
    env["TRIEVO_RESOURCE_ENFORCEMENT"] = spec["resource_enforcement_mode"]
if spec.get("native_environment"):
    from native_environment import mapping_script
    exec(mapping_script(spec["native_environment"], str(task_dir)))
    os.chdir(spec["native_environment"]["cwd"])
try:
    proc = subprocess.run([sys.executable, str(turn / "evaluation_assets" / "verifier_cli.py")],
                          input=json.dumps(request), text=True, capture_output=True, env=env,
                          timeout=spec["timeout"])
    lines = proc.stdout.strip().splitlines()
    result = json.loads(lines[-1]) if lines else {"status": "error", "error": proc.stderr[-1500:]}
except Exception as exc:
    result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
(w / f"result-{fixture}.json").write_text(json.dumps(
    {"status": result.get("status"), "score": result.get("score"), "error": result.get("error"),
     "private_diagnostics": result.get("private_diagnostics"), "resource_readback": resource_readback,
     "resource_enforcement": (resource_readback or {}).get("resource_enforcement"),
     "resource_observation_provenance": "qualification_container"}))
'''


ORACLE_DRIVER = r'''
import hashlib, json, os, pwd, grp, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from container_resources import require_container_resources, read_container_resources, evaluate_resource_enforcement
from native_environment import mapping_script
w = Path('/w'); spec = json.loads((w / 'oracle-spec.json').read_text())
result = {'nonce': spec['nonce'], 'status': 'error'}
try:
    snapshot = read_container_resources()
    assessment = evaluate_resource_enforcement(snapshot, enforcement_mode=spec['resource_enforcement_mode'])
    result.update(resource_readback={**snapshot, **assessment}, resource_enforcement=assessment['resource_enforcement'])
    resources = require_container_resources(snapshot, enforcement_mode=spec['resource_enforcement_mode'])
    result.update(resource_readback=resources, resource_enforcement=resources['resource_enforcement'])
    task_root = Path('/workspace/tasks') / spec['turn_id']
    shutil.copytree(w / 'seed', task_root, symlinks=True)
    shutil.copytree(w / 'oracle', spec['oracle_mount'], symlinks=True)
    exec(mapping_script(spec['native_environment'], str(task_root)))
    canonical = task_root.resolve()
    runner = spec['oracle']
    user = spec['native_user'] or '0'
    username, _, group = user.partition(':')
    try:
        account = pwd.getpwuid(int(username)) if username.isdigit() else pwd.getpwnam(username)
    except KeyError:
        account = None
    uid = int(username) if username.isdigit() else account.pw_uid
    gid = (int(group) if group.isdigit() else grp.getgrnam(group).gr_gid) if group else (account.pw_gid if account else 0)
    # Mapping setup chowns canonical public files without changing oracle bytes.
    for node in [task_root, *task_root.rglob('*')]:
        os.chown(node, uid, gid, follow_symlinks=False)
    def drop_user():
        if uid:
            if account: os.initgroups(account.pw_name, gid)
            else: os.setgroups([])
            os.setgid(gid); os.setuid(uid)
    env = {**os.environ, **runner.get('env', {}), 'ORACLE_DIR': spec['oracle_mount'],
           'UV_OFFLINE': '1', 'PIP_NO_INDEX': '1'}
    with (w / 'oracle.log').open('w') as log:
        proc = subprocess.run(runner['command'], cwd=runner.get('cwd', spec['native_environment']['cwd']),
            env=env, stdout=log, stderr=subprocess.STDOUT, timeout=runner['timeout_seconds'], preexec_fn=drop_user)
    result['returncode'] = proc.returncode
    if proc.returncode:
        raise RuntimeError('unchanged oracle failed with exit ' + str(proc.returncode))
    if task_root.is_symlink() or task_root.resolve() != canonical:
        raise ValueError('oracle replaced canonical public root')
    for node in task_root.rglob('*'):
        target = node.resolve(strict=True)
        if target != canonical and canonical not in target.parents:
            raise ValueError('oracle public output symlink escapes canonical tree: ' + str(node.relative_to(task_root)))
        if not (target.is_file() or target.is_dir()):
            raise ValueError('oracle output contains a special file')
    # Materialize only canonical public output. Private oracle/driver/tests never
    # become fixtures; safe internal symlinks are flattened after containment checks.
    shutil.copytree(task_root, w / 'export', symlinks=False)
    digest = hashlib.sha256()
    for node in sorted((w / 'export').rglob('*')):
        if node.is_file():
            digest.update(node.relative_to(w / 'export').as_posix().encode() + b'\0' + node.read_bytes())
    result.update(status='pass', regenerated_at_utc=datetime.now(timezone.utc).isoformat(),
                  regenerated_fixture_sha256=digest.hexdigest())
except Exception as exc:
    result['error'] = type(exc).__name__ + ': ' + str(exc)
result['output_log'] = (w / 'oracle.log').read_text(errors='replace')[-2000000:] if (w / 'oracle.log').exists() else ''
(w / 'oracle-result.json').write_text(json.dumps(result))
raise SystemExit(0 if result['status'] == 'pass' else 1)
'''


def _tree_digest(root: Path) -> str:
    """Hash bytes and relative names, rejecting non-public export symlinks."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError('oracle transport root must be an actual directory')
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('unexpected symlink in oracle transport tree: ' + str(path.relative_to(root)))
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes())
        elif not path.is_dir():
            raise ValueError('unexpected special file in oracle transport tree')
    return digest.hexdigest()


def regenerate_date_gold(turn: Path, image: str, work: Path, spec: dict, meta: dict) -> dict:
    """Run unchanged private oracle in a fresh container; never use stale gold."""
    from trievo_bench import container_resources, native_environment
    from trievo_bench.container_resources import require_container_resources
    environment = spec['native_environment']
    if not environment or spec['source'] != 'workspace':
        raise ValueError('date-dependent oracle requires native workspace submission')
    relative = meta.get('oracle_assets_relative_path')
    if relative != 'native_oracle' or meta.get('oracle_mount') not in ('/oracle', '/solution'):
        raise ValueError('unsafe date-dependent private oracle location')
    oracle = meta.get('oracle')
    if (not isinstance(oracle, dict) or not isinstance(oracle.get('command'), list) or not oracle['command']
            or not all(isinstance(part, str) for part in oracle['command'])
            or not isinstance(oracle.get('timeout_seconds'), (int, float)) or oracle['timeout_seconds'] <= 0):
        raise ValueError('invalid date-dependent oracle command/timeout')
    seed = turn / 'workspace_seed'
    assets = turn / 'evaluation_assets' / relative
    oracle_hash = _tree_digest(assets)
    seed_hash = _tree_digest(seed)
    stage = work / 'date-oracle'
    stage.mkdir()
    shutil.copytree(seed, stage / 'seed')
    shutil.copytree(assets, stage / 'oracle')
    shutil.copy2(container_resources.__file__, stage / 'container_resources.py')
    shutil.copy2(native_environment.__file__, stage / 'native_environment.py')
    (stage / 'driver.py').write_text(ORACLE_DRIVER)
    nonce = uuid4().hex
    oracle_spec = {**spec, 'nonce': nonce, 'oracle': oracle, 'oracle_mount': meta['oracle_mount'],
                   'native_user': meta.get('native_user', environment.get('user', ''))}
    (stage / 'oracle-spec.json').write_text(json.dumps(oracle_spec))
    name = 'trievo-l2-date-oracle-' + uuid4().hex[:12]
    observed = {'status': 'error', 'error': 'oracle execution did not produce verified output'}
    cmd = ['docker', 'create', '--name', name, '--network', 'none', '--memory', '4g', '--cpus', '2',
           '--pids-limit', '256', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
           '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--security-opt', 'no-new-privileges', '--user', '0',
           '--entrypoint', environment.get('python_executable', 'python'), image, '/w/driver.py']
    limit = oracle['timeout_seconds'] + 120
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=60)
        subprocess.run(['docker', 'cp', str(stage), name + ':/w'], check=True, capture_output=True, timeout=300)
        proc = subprocess.run(['docker', 'start', '-a', name], text=True, capture_output=True, timeout=limit)
        result = work / 'date-oracle-result.json'
        subprocess.run(['docker', 'cp', name + ':/w/oracle-result.json', str(result)],
                       check=True, capture_output=True, timeout=60)
        observed = json.loads(result.read_text())
        if observed.get('nonce') != nonce:
            raise ValueError('date oracle transport nonce mismatch')
        checked = require_container_resources(observed.get('resource_readback') or {},
                                              enforcement_mode=spec['resource_enforcement_mode'])
        observed.update(resource_readback=checked, resource_enforcement=checked['resource_enforcement'])
        inspected = subprocess.run(['docker', 'inspect', name, '--format',
            '{{.Image}} {{.HostConfig.NetworkMode}} {{.State.ExitCode}}'], check=True, text=True, capture_output=True, timeout=30)
        if inspected.stdout.strip().split() != [image, 'none', str(proc.returncode)]:
            raise ValueError('date oracle image/network/exit readback mismatch')
        if proc.returncode or observed.get('status') != 'pass':
            raise ValueError(observed.get('error', 'unchanged oracle failed'))
        exported = work / 'regenerated-gold'
        subprocess.run(['docker', 'cp', name + ':/w/export', str(exported)], check=True, capture_output=True, timeout=300)
        if _tree_digest(exported) != observed.get('regenerated_fixture_sha256'):
            raise ValueError('date oracle public output hash mismatch')
        archived = turn / 'evaluation_assets/fixtures/known_correct'
        if archived.is_symlink() or turn.resolve() not in archived.resolve().parents:
            raise ValueError('temporary gold fixture path escapes copied turn')
        shutil.rmtree(archived)
        shutil.copytree(exported, archived)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        observed.update(status='error', error=type(exc).__name__ + ': ' + str(exc))
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=60)
    return {**observed, 'date_dependent': True, 'oracle_sha256': oracle_hash, 'public_seed_sha256': seed_hash,
            'oracle_command': oracle['command'], 'oracle_cwd': oracle.get('cwd', environment['cwd']),
            'native_user': oracle_spec['native_user'],
            'container_image_digest': image, 'fresh_oracle_container': True,
            'provenance': 'unchanged private upstream oracle from public workspace_seed; fresh public output only',
            'resource_observation_provenance': 'date_oracle_container'}


def parse_resource_header(output: str, nonce: str, mode: str) -> dict:
    """Accept only the first trusted stdout line, never a later candidate echo."""
    from trievo_bench.container_resources import require_container_resources
    lines = output.splitlines()
    try:
        header = json.loads(lines[0]) if lines else None
    except ValueError as exc:
        raise ValueError("invalid first resource header line") from exc
    if (not isinstance(header, dict) or header.get("event") != "trievo_resource_header"
            or header.get("nonce") != nonce or not isinstance(header.get("resource_readback"), dict)):
        raise ValueError("missing or mismatched first resource header")
    return require_container_resources(header["resource_readback"], enforcement_mode=mode)


def qualify_turn(turn_dir: Path, image: str, scratch: Path, profile: dict | None = None) -> dict:
    if (profile or {}).get("image_archive"):
        from trievo_bench.image_archive import ensure_image_available
        ensure_image_available(image, profile["image_archive"])
    environment = (profile or {}).get("native_environment")
    isolated = bool((profile or {}).get("environment_version") or environment)
    if isolated and (profile or {}).get("task_sandbox_image_digest") != image:
        raise ValueError("native task and verifier must use the same image")
    if environment:
        from trievo_bench.native_environment import validate_native_environment
        validate_native_environment(environment)
    if isolated:
        from trievo_bench.container_resources import docker_resource_capability_preflight, resource_enforcement_mode
        capability = docker_resource_capability_preflight()
        if not capability.get("allowed", capability["passed"]):
            raise RuntimeError("native resource enforcement unavailable: " + "; ".join(capability["errors"]))
    interpreter = environment.get("python_executable", "python") if environment else "python"
    task = json.loads((turn_dir / "task.json").read_text())
    native = task["evaluation"]["native_verifier"]
    binding = next(b for b in native["input_bindings"] if b["name"] == "submission")
    spec = {"turn_id": task["turn_id"], "source": binding["source"], "path": binding["path"],
            "timeout": int(native["timeout_seconds"]), "native_environment": environment,
            "environment_version": (profile or {}).get("environment_version"),
            "resource_enforcement_mode": resource_enforcement_mode() if isolated else "strict"}
    record = {"stream_id": task["stream_id"], "turn_id": task["turn_id"],
              "source_id": task["source_basis"].get("source_task_id"), "image": image, "fixtures": {}}
    work = Path(tempfile.mkdtemp(prefix="l2v5-qual-", dir=scratch))
    try:
        shutil.copytree(turn_dir, work / "turn", symlinks=True)
        (work / "spec.json").write_text(json.dumps(spec))
        (work / "driver.py").write_text(DRIVER)
        if isolated:
            import trievo_bench.container_resources as resource_module
            shutil.copy2(resource_module.__file__, work / "container_resources.py")
        if environment:
            import trievo_bench.native_environment as native_module
            shutil.copy2(native_module.__file__, work / "native_environment.py")
        metadata_file = work / "turn/evaluation_assets/native_task.json"
        metadata = json.loads(metadata_file.read_text()) if metadata_file.is_file() else {}
        needs_refresh = metadata.get("date_dependent") or metadata.get("package_qualification_requires_oracle_regeneration")
        if needs_refresh:
            try:
                if not isolated or not metadata.get("date_dependent") or not metadata.get("package_qualification_requires_oracle_regeneration"):
                    raise ValueError("date-dependent metadata must require explicit native oracle regeneration")
                regenerated = regenerate_date_gold(work / "turn", image, work, spec, metadata)
            except (OSError, ValueError, RuntimeError) as exc:
                regenerated = {"status": "error", "error": str(exc), "date_dependent": True}
            record["date_dependent_gold_regeneration"] = regenerated
            if regenerated["status"] != "pass":
                record["fixtures"] = {fixture: {"expected": expected, "status": "error", "score": None,
                    "error": "date-dependent oracle regeneration failed; stale gold fallback forbidden"}
                    for fixture, expected in FIXTURES.items()}
                record["ok"] = False
                return record
        for fixture, expected in FIXTURES.items():
            container_name = "trievo-l2-qual-" + uuid4().hex[:12]
            spec["resource_nonce"] = uuid4().hex
            (work / "spec.json").write_text(json.dumps(spec))
            resource_header = None
            resource_header_source = None
            cmd = ["docker", *(["create", "--name", container_name] if isolated else ["run", "--rm"]),
                   "--network", "none", "--memory", "4g", "--cpus", "2",
                   *(["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256"]
                     if (profile or {}).get("environment_version") or environment else []),
                   *(["--user", "0", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                      "--cap-add", "SETUID", "--cap-add", "SETGID"] if environment else []),
                   *([] if isolated else ["-v", f"{work}:/w"]),
                   "--entrypoint", interpreter, image, "/w/driver.py", fixture]
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True, timeout=spec["timeout"] + 300)
                if isolated:
                    proc.check_returncode()
                    subprocess.run(["docker", "cp", str(work), f"{container_name}:/w"],
                                   check=True, text=True, capture_output=True, timeout=300)
                    proc = subprocess.run(["docker", "start", "-a", container_name],
                                          text=True, capture_output=True, timeout=spec["timeout"] + 300)
                    header_output = proc.stdout
                    resource_header_source = "attached_stdout"
                    if not header_output:
                        logs = subprocess.run(["docker", "logs", container_name], text=True,
                                              capture_output=True, check=True, timeout=60)
                        header_output = logs.stdout
                        resource_header_source = "docker_logs"
                    resource_header = parse_resource_header(
                        header_output, spec["resource_nonce"], spec["resource_enforcement_mode"])
                    subprocess.run(["docker", "cp", f"{container_name}:/w/result-{fixture}.json",
                                    str(work / f"result-{fixture}.json")],
                                   check=True, text=True, capture_output=True, timeout=60)
                detail = (proc.stderr or proc.stdout)[-1500:]
            except subprocess.TimeoutExpired:
                detail = "docker qualification timed out"
            except subprocess.CalledProcessError as exc:
                detail = str(exc) + " " + str(exc.stderr or "")[-1500:]
            except (ValueError, RuntimeError) as exc:
                detail = "qualification resource header rejected: " + str(exc)
            finally:
                if isolated:
                    subprocess.run(["docker", "rm", "-f", container_name],
                                   capture_output=True, timeout=60)
            result_file = work / f"result-{fixture}.json"
            outcome = (json.loads(result_file.read_text()) if result_file.exists()
                       else {"status": "docker_error", "score": None, "error": detail})
            if isolated:
                if resource_header is None:
                    outcome = {**outcome, "status": "error", "score": None,
                               "error": "trusted pre-task resource header missing or rejected: " + detail,
                               "resource_readback": None, "resource_enforcement": "unobserved",
                               "observed_resources": None,
                               "requested_resources": {"cpu_limit": 2.0, "memory_limit_bytes": 4294967296, "pids_limit": 256}}
                else:
                    matches = (outcome.get("resource_readback") == resource_header
                               and outcome.get("resource_enforcement") == resource_header["resource_enforcement"])
                    if not matches:
                        outcome = {**outcome, "status": "error", "score": None,
                                   "error": "post-task resource evidence disagrees with trusted pre-task header"}
                    outcome.update(resource_readback=resource_header,
                        resource_enforcement=resource_header["resource_enforcement"],
                        requested_resources=resource_header["requested_resources"],
                        observed_resources=resource_header["observed_resources"],
                        resource_observation_provenance={"kind": "pre_task_stdout_header",
                            "nonce": spec["resource_nonce"], "captured_from": resource_header_source,
                            "container": container_name})
            record["fixtures"][fixture] = {"expected": expected, **outcome}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    record["ok"] = all(v["status"] == v["expected"] for v in record["fixtures"].values())
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--stream", action="append", default=[])
    args = parser.parse_args()
    profiles = {p["runtime_profile_id"]: p for p in
                json.loads((args.package / "runtime_profiles.json").read_text())["profiles"]}
    turns = []
    for stream in sorted((args.package / "streams").iterdir()):
        if args.stream and stream.name not in args.stream:
            continue
        for task_file in sorted(stream.glob("turn*/task.json")):
            task = json.loads(task_file.read_text())
            turns.append((task_file.parent, profiles[task["runtime_profile_id"]]))
    args.scratch.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.jobs) as pool, args.output.open("w") as sink:
        for record in pool.map(lambda t: qualify_turn(t[0], t[1]["verifier_image_digest"], args.scratch, t[1]), turns):
            sink.write(json.dumps(record) + "\n")
            sink.flush()
            if not record["ok"]:
                print("FAIL", record["stream_id"], record["turn_id"], record["source_id"],
                      json.dumps(record["fixtures"])[:600], flush=True)
    records = [json.loads(line) for line in args.output.read_text().splitlines()]
    print(json.dumps({"turns": len(records), "ok": sum(r["ok"] for r in records)}))
    return 0 if records and all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
