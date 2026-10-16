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
    resource_readback = require_container_resources()
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
     "private_diagnostics": result.get("private_diagnostics"), "resource_readback": resource_readback}))
'''


def qualify_turn(turn_dir: Path, image: str, scratch: Path, profile: dict | None = None) -> dict:
    environment = (profile or {}).get("native_environment")
    isolated = bool((profile or {}).get("environment_version") or environment)
    if isolated and (profile or {}).get("task_sandbox_image_digest") != image:
        raise ValueError("native task and verifier must use the same image")
    if environment:
        from trievo_bench.native_environment import validate_native_environment
        validate_native_environment(environment)
    if isolated:
        from trievo_bench.container_resources import docker_resource_capability_preflight
        capability = docker_resource_capability_preflight()
        if not capability["passed"]:
            raise RuntimeError("native resource enforcement unavailable: " + "; ".join(capability["errors"]))
    interpreter = environment.get("python_executable", "python") if environment else "python"
    task = json.loads((turn_dir / "task.json").read_text())
    native = task["evaluation"]["native_verifier"]
    binding = next(b for b in native["input_bindings"] if b["name"] == "submission")
    spec = {"turn_id": task["turn_id"], "source": binding["source"], "path": binding["path"],
            "timeout": int(native["timeout_seconds"]), "native_environment": environment,
            "environment_version": (profile or {}).get("environment_version")}
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
        for fixture, expected in FIXTURES.items():
            container_name = "trievo-l2-qual-" + uuid4().hex[:12]
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
                    subprocess.run(["docker", "cp", f"{container_name}:/w/result-{fixture}.json",
                                    str(work / f"result-{fixture}.json")],
                                   check=True, text=True, capture_output=True, timeout=60)
                detail = (proc.stderr or proc.stdout)[-1500:]
            except subprocess.TimeoutExpired:
                detail = "docker qualification timed out"
            except subprocess.CalledProcessError as exc:
                detail = str(exc) + " " + str(exc.stderr or "")[-1500:]
            finally:
                if isolated:
                    subprocess.run(["docker", "rm", "-f", container_name],
                                   capture_output=True, timeout=60)
            result_file = work / f"result-{fixture}.json"
            outcome = (json.loads(result_file.read_text()) if result_file.exists()
                       else {"status": "docker_error", "score": None, "error": detail})
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
