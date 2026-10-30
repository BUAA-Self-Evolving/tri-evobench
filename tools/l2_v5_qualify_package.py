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

FIXTURES = {"known_correct": "pass", "known_broken": "fail"}
DRIVER = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
w = Path("/w"); turn = w / "turn"; spec = json.loads((w / "spec.json").read_text())
fixture = sys.argv[1]
src = turn / "evaluation_assets" / "fixtures" / fixture
ws = w / f"ws-{fixture}"
task_dir = ws / "tasks" / spec["turn_id"]
task_dir.mkdir(parents=True)
if spec["source"] == "interaction_record":
    binding = {"kind": "inline", "value": (src / "response.txt").read_text()}
else:
    shutil.copytree(src, task_dir, dirs_exist_ok=True)
    binding = {"kind": "path", "path": spec["path"]}
request = {"schema_version": "l2_native_verifier_request/1.0", "bindings": {"submission": binding}}
env = {**os.environ, "L2_VERIFIER_WORKSPACE": str(ws)}
try:
    proc = subprocess.run([sys.executable, str(turn / "evaluation_assets" / "verifier_cli.py")],
                          input=json.dumps(request), text=True, capture_output=True, env=env,
                          timeout=spec["timeout"])
    lines = proc.stdout.strip().splitlines()
    result = json.loads(lines[-1]) if lines else {"status": "error", "error": proc.stderr[-1500:]}
except Exception as exc:
    result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
(w / f"result-{fixture}.json").write_text(json.dumps(
    {"status": result.get("status"), "score": result.get("score"), "error": result.get("error")}))
'''


def qualify_turn(turn_dir: Path, image: str, scratch: Path, *, volume_transport: bool = False,
                 declared_image: str | None = None, cpus: float = 2) -> dict:
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0 < cpus <= 8:
        raise ValueError("qualification CPU limit must be in (0,8]")
    task = json.loads((turn_dir / "task.json").read_text())
    native = task["evaluation"]["native_verifier"]
    binding = next(b for b in native["input_bindings"] if b["name"] == "submission")
    spec = {"turn_id": task["turn_id"], "source": binding["source"], "path": binding["path"],
            "timeout": int(native["timeout_seconds"])}
    record = {"stream_id": task["stream_id"], "turn_id": task["turn_id"],
              "source_id": task["source_basis"].get("source_task_id"), "image": image,
              "declared_image": declared_image or image, "volume_transport": volume_transport,
              "cpus": cpus, "fixtures": {}}
    work = Path(tempfile.mkdtemp(prefix="l2v5-qual-", dir=scratch))
    mirror = None
    try:
        shutil.copytree(turn_dir, work / "turn", symlinks=True)
        (work / "spec.json").write_text(json.dumps(spec))
        (work / "driver.py").write_text(DRIVER)
        if volume_transport:
            from trievo_bench.docker_volume import DockerVolumeMirror
            mirror = DockerVolumeMirror.create(work, role="l2-v5-qualification")
        for fixture, expected in FIXTURES.items():
            cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "4g", "--cpus", str(cpus),
                   "-v", mirror.mount("/w") if mirror else f"{work}:/w",
                   "--entrypoint", "python", image, "/w/driver.py", fixture]
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True, timeout=spec["timeout"] + 300)
                detail = (proc.stderr or proc.stdout)[-1500:]
            except subprocess.TimeoutExpired:
                detail = "docker run timed out"
            if mirror:
                mirror.sync_to_host()
            result_file = work / f"result-{fixture}.json"
            outcome = (json.loads(result_file.read_text()) if result_file.exists()
                       else {"status": "docker_error", "score": None, "error": detail})
            record["fixtures"][fixture] = {"expected": expected, **outcome}
    finally:
        if mirror:
            mirror.close()
        shutil.rmtree(work, ignore_errors=True)
    record["ok"] = all(v["status"] == v["expected"] for v in record["fixtures"].values())
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--cpus", type=float, default=2, help="fixed CPU limit for every fixture container")
    parser.add_argument("--stream", action="append", default=[])
    parser.add_argument("--image-map", type=Path, help="declared digest to independently rebuilt digest JSON")
    parser.add_argument("--volume-transport", action="store_true", help="copy fixtures through daemon-owned volumes")
    args = parser.parse_args()
    if not 0 < args.cpus <= 8:
        parser.error("--cpus must be in (0,8]")
    image_map = json.loads(args.image_map.read_text()) if args.image_map else {}
    if not isinstance(image_map, dict) or any(
        not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:")
        or any(c not in "0123456789abcdef" for c in value[7:])
        for pair in image_map.items() for value in pair
    ):
        parser.error("--image-map must map pinned SHA-256 image digests")
    profiles = {p["runtime_profile_id"]: p for p in
                json.loads((args.package / "runtime_profiles.json").read_text())["profiles"]}
    turns = []
    for stream in sorted((args.package / "streams").iterdir()):
        if args.stream and stream.name not in args.stream:
            continue
        for task_file in sorted(stream.glob("turn*/task.json")):
            task = json.loads(task_file.read_text())
            turns.append((task_file.parent, profiles[task["runtime_profile_id"]]["verifier_image_digest"]))
    args.scratch.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.jobs) as pool, args.output.open("w") as sink:
        for record in pool.map(lambda t: qualify_turn(t[0], image_map.get(t[1], t[1]), args.scratch,
                                                     volume_transport=args.volume_transport, declared_image=t[1], cpus=args.cpus), turns):
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
