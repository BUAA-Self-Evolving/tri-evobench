"""Start the migrated matrix; accept one provider credential on stdin."""
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
    parser.add_argument("--provider-mode", choices=sorted(PROVIDER_PROFILES), default="packycode")
    args = parser.parse_args()
    root = REPO_ROOT
    # Authentication is supplied through SSH stdin, not command-line arguments.
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
    with (root / "migration/server-driver.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "tools/run_new_package_matrix.py", "--provider-mode", args.provider_mode,
             "--packages", "benchmarks/current",
             "--root", "runs/mx24/full"], cwd=root, env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps({"pid": process.pid, "log": "migration/server-driver.log"}))


if __name__ == "__main__":
    main()
