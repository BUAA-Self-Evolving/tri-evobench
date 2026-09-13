"""Launch the L2 condition driver while accepting the API key only on stdin."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trievo_bench.provider_profiles import PROVIDER_PROFILES, configure_provider_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--provider-mode", choices=sorted(PROVIDER_PROFILES), default="packycode")
    args = parser.parse_args()
    root = REPO_ROOT
    secret = json.loads(sys.stdin.readline())
    environment = dict(os.environ)
    credential_name = PROVIDER_PROFILES[args.provider_mode].credential_env
    credential = secret.get("app_id" if args.provider_mode == "friday" else "api_key")
    if not credential:
        raise SystemExit(f"Missing credential for {credential_name}")
    environment[credential_name] = credential
    configure_provider_environment(args.provider_mode, environment)
    environment.update(PYTHONUNBUFFERED="1", TMPDIR=str(root / "tmp/runtime"))
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "tools/run_l2_condition_matrix.py", "--provider-mode", args.provider_mode,
               "--root", "runs/mx25/l2-controls"]
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
