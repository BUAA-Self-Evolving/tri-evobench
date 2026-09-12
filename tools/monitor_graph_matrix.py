"""Compact read-only matrix snapshot for the SSH heartbeat."""
import json
import os
from pathlib import Path
import re
import subprocess
from collections import deque


def tail(path, count=8):
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            lines = list(deque(stream, maxlen=count))
        return [re.sub(r"sk-[A-Za-z0-9_.-]{15,}", "[REDACTED]", line.rstrip())[:1000] for line in lines]
    except OSError:
        return []


def main():
    root = Path(__file__).resolve().parents[1]
    state = json.loads((root / "runs/mx24/full/matrix_status.json").read_text())
    current = next((job for job in state["jobs"] if job["id"] == state.get("current_job")), {})
    pid = state.get("pid")
    try:
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, TypeError):
        alive = False
    result = {"status": state["status"], "pid": pid, "alive": alive,
              "current_job": state.get("current_job"),
              "completed_jobs": sum(job.get("status") == "completed" for job in state["jobs"]),
              "driver_tail": tail(root / "migration/server-driver.log")}
    if current:
        output = Path(current["output"])
        result["job_tail"] = tail(current.get("log", ""))
        checkpoints = list((output / "_checkpoints").rglob("checkpoint.json"))
        if checkpoints:
            checkpoint = max(checkpoints, key=lambda p: p.stat().st_mtime)
            result["checkpoint"] = json.loads(checkpoint.read_text())
            result["checkpoint_path"] = str(checkpoint)
        logs = list(output.rglob("baseline.stderr.log"))
        if logs:
            result["baseline_tail"] = tail(max(logs, key=lambda p: p.stat().st_mtime))
    docker = subprocess.run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"], capture_output=True, text=True, timeout=20)
    result["docker"] = docker.stdout.strip()
    result["docker_error"] = docker.stderr.strip()[:500]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
