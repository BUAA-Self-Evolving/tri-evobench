"""Launch the L2 condition driver while accepting the API key only on stdin."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    secret = json.loads(sys.stdin.readline())
    if not secret.get("api_key"):
        raise SystemExit("Missing API key")
    environment = dict(os.environ)
    environment.update(
        TRIEVO_API_KEY=secret["api_key"], TRIEVO_BASE_URL="https://cf.api.fan/v1",
        TRIEVO_MODEL="deepseek-v4-flash", PYTHONUNBUFFERED="1", TMPDIR=str(root / "tmp/runtime"),
    )
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "tools/run_l2_condition_matrix.py", "--root", "runs/mx25/l2-controls"]
    if args.gate:
        command.append("--gate")
    log_path = root / ("migration/l2-controls-gate.log" if args.gate else "migration/l2-controls-driver.log")
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    print(json.dumps({"pid": process.pid, "log": str(log_path.relative_to(root))}))


if __name__ == "__main__":
    main()
