"""Start the migrated matrix; accept the key on stdin, never persist it."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    # Authentication is supplied through SSH stdin, not command-line arguments.
    secret = json.loads(sys.stdin.readline())
    if not secret.get("api_key"):
        raise SystemExit("Missing API key")
    environment = dict(os.environ)
    environment.update(TRIEVO_API_KEY=secret["api_key"], TRIEVO_BASE_URL="https://cf.api.fan/v1",
                       TRIEVO_MODEL="deepseek-v4-flash", PYTHONUNBUFFERED="1",
                       TMPDIR=str(root / "tmp/runtime"))
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    with (root / "migration/server-driver.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "tools/run_new_package_matrix.py", "--packages", "benchmarks/current",
             "--root", "runs/mx24/full"], cwd=root, env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps({"pid": process.pid, "log": "migration/server-driver.log"}))


if __name__ == "__main__":
    main()
