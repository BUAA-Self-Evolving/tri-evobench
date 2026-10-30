"""Launch new Friday matrices with the tested, guarded throughput preset.

--dry-run only reads plans. A real launch first checks Docker/source readiness,
then ensures the independent local gateway is ready, before admitting workers.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_friday_gateway import load_pool
from trievo_bench.baselines.manifest import find_manifest, load_manifest
from trievo_bench.friday_gateway import check_gateway


def ensure_gateway(configuration, credentials, environment, state_path=None):
    url = configuration["host_base_url"]
    from urllib.parse import urlsplit
    port = urlsplit(url).port
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError:
        state = state_path or REPO / ".tmp/friday-shared-budget.sqlite"
        state.parent.mkdir(parents=True, exist_ok=True)
        with (state.parent / "friday-gateway.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, str(REPO / "tools/run_friday_gateway.py"),
                "--state", str(state), "--port", str(port),
                "--model", configuration.get("model", "deepseek-v4-flash"),
                "--policy-json", json.dumps(configuration["policy"])], env=environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                check_gateway(url, credentials, configuration["policy"], configuration.get("model"))
                return child
            except ValueError:
                if child.poll() is not None:
                    break
                time.sleep(0.1)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        raise ValueError("Friday gateway did not start; inspect its state directory's friday-gateway.log")
    check_gateway(url, credentials, configuration["policy"], configuration.get("model"))


def check_readiness(config_dir, baselines, environment):
    docker = shutil.which("docker", path=environment.get("PATH"))
    if not docker:
        raise ValueError("Docker executable is required for a real matrix")
    for baseline in baselines:
        name = f"tmp_hermes_cf_l1.override.json" if baseline == "hermes" else f"tmp_{baseline}_docker.override.json"
        names = [name]
        if baseline == "hermes" and (config_dir / "tmp_hermes_cf_l2.override.json").is_file():
            names.append("tmp_hermes_cf_l2.override.json")
        for name in names:
            path = config_dir / name
            if not path.is_file():
                raise ValueError(f"missing prepared baseline config: {name}")
            manifest = load_manifest(find_manifest(baseline, REPO)).with_overrides(json.loads(path.read_text()))
            command_image = manifest.adapter["command"][-1]
            image = manifest.options.get("image") if command_image == "{image}" else command_image
            result = subprocess.run([docker, "image", "inspect", str(image)], env=environment,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if result.returncode:
                raise ValueError(f"baseline image not available: {baseline}; build/verify it before launch")
            # Same strict, hash-bound source identity check as each worker.
            from trievo_bench.baselines.process import ProcessBaselineRuntime
            from trievo_bench.model import TokenBudget
            ProcessBaselineRuntime(manifest, budget=TokenBudget(), repo_root=REPO)._verify_source_revision()



def check_container_gateway(configuration, credential, environment):
    """Test Docker -> gateway authentication without making a model request."""
    docker = shutil.which("docker", path=environment.get("PATH"))
    image = "trievo-ubuntu:22.04"
    inspect = subprocess.run([docker, "image", "inspect", image], env=environment,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if inspect.returncode:
        raise ValueError("gateway connectivity preflight requires the existing L1 sandbox image")
    route = configuration["container_base_url"].removesuffix("/v1/openai/native") + "/health"
    code = ("import json,os,urllib.request; "
            f"r=urllib.request.Request({route!r},headers={{'Authorization':'Bearer '+os.environ['TRIEVO_API_KEY']}}); "
            "d=json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=5)); "
            f"assert d['policy']=={configuration['policy']!r}; "
            f"assert d['slot_name']=={credential.slot!r}")
    child_environment = dict(environment, TRIEVO_API_KEY=credential.value)
    result = subprocess.run([docker, "run", "--rm", "--entrypoint", "python3", "-e", "TRIEVO_API_KEY",
                             image, "-c", code], env=child_environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if result.returncode:
        raise ValueError("Docker cannot reach/authenticate the configured gateway; check --container-host before launch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id-file", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    local_default = REPO / "tmp/l2-local-fast-current"
    parser.add_argument("--config-dir", type=Path, default=local_default if local_default.is_dir() else REPO / "tmp/friday-fast-20261017")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--job-error-policy", choices=("stop", "continue"), default="continue",
                        help="skip failed cells and drain the queue by default")
    args, remaining = parser.parse_known_args()
    forbidden = {"--provider-mode", "--concurrency", "--friday-capacities",
                 "--friday-slot-order", "--adopt-state", "--retry-interrupted-run", "--retry-authorization"}
    if any(value.startswith("--") and any(flag.startswith(value.split("=")[0]) for flag in forbidden)
           for value in remaining):
        parser.error("fast preset fixes provider/capacity and supports new batches, not recovery/adoption overrides")
    config_dir = args.config_dir.resolve()
    preset = json.loads((config_dir / "execution-preset.json").read_text())
    def supplied(flag):
        return any(value == flag or value.startswith(flag + "=") for value in remaining)
    if preset.get("baselines") and not supplied("--baseline"):
        for baseline in preset["baselines"]:
            remaining += ["--baseline", baseline]
    for flag, key in [("--level", "level"), ("--l1-package", "l1_package"), ("--l2-package", "l2_package"), ("--l2-panel", "l2_panel")]:
        if preset.get(key) and not supplied(flag):
            remaining += [flag, str((REPO / preset[key]).resolve()) if key in {"l1_package", "l2_package"} else preset[key]]
    if preset.get("l1_conditions") and not supplied("--l1-condition"):
        for condition in preset["l1_conditions"]:
            remaining += ["--l1-condition", condition]
    if preset.get("l2_conditions") and not supplied("--l2-condition"):
        for condition in preset["l2_conditions"]:
            remaining += ["--l2-condition", condition]
    configuration = json.loads((config_dir / "provider-profile.json").read_text())["friday_gateway"]
    root = (REPO / args.root).resolve()
    status = root / "matrix_status.json"
    if status.exists() and json.loads(status.read_text()).get("provider_gateway") != configuration:
        parser.error("output root belongs to another provider plan; use a new --root")
    credentials = load_pool(args.app_id_file)
    if {credential.slot for credential in credentials} != set(preset["worker_capacities"]):
        parser.error("fast preset requires the configured six indexed slots; regenerate for a different pool")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("FRIDAY_APP_ID")}
    environment.update({credential.slot: credential.value for credential in credentials})
    environment.pop("TRIEVO_FRIDAY_GATEWAY_URL", None)
    environment["TRIEVO_PROVIDER_MODEL"] = configuration.get("model", "deepseek-v4-flash")
    docker = shutil.which("docker")
    bundled = Path.home() / ".local/share/trievo-container-tools/bin/docker"
    if not docker and bundled.is_file():
        environment["PATH"] = str(bundled.parent) + os.pathsep + environment.get("PATH", "")
    environment.setdefault("TRIEVO_DOCKER_VOLUME_TRANSPORT", "required")
    environment.setdefault("TRIEVO_STORAGE_GUARD", "1")
    environment.setdefault("TRIEVO_STORAGE_PATH", str(REPO.parent))
    environment.setdefault("TRIEVO_STORAGE_STATE_ROOT", str(root / "storage-guard"))
    environment.setdefault("TRIEVO_STORAGE_MIN_FREE_GIB", str(preset.get("storage_min_free_gib", 60)))
    environment.setdefault("TRIEVO_STORAGE_RESUME_FREE_GIB", str(preset.get("storage_resume_free_gib", 75)))
    environment.setdefault("TRIEVO_STORAGE_REMOTE_DOCKER_ROOT", "1")
    command = [sys.executable, str(REPO / "tools/run_new_package_matrix.py"),
        "--provider-mode", "friday", "--root", str(root), "--config-dir", str(config_dir),
        "--friday-capacities", str(config_dir / "friday-capacities.json"),
        "--concurrency", str(preset["global_workers"]), "--job-error-policy", args.job_error_policy,
        "--storage-guard", *remaining]
    if preset.get("local_resource_policy"):
        command += ["--local-resource-policy", str(config_dir / preset["local_resource_policy"])]
    if preset.get("fast_checkpoints"):
        environment["TRIEVO_FAST_CHECKPOINTS"] = "1"
        environment["TRIEVO_CHECKPOINT_STORE"] = str(root / "_snapshot_store")
    if preset.get("performance_timings"):
        environment["TRIEVO_TIMING_LOG"] = str(root / "performance.jsonl")
    if args.dry_run:
        command.append("--dry-run")
    else:
        from tools.run_new_package_matrix import BASELINES
        baselines = [remaining[index + 1] for index, value in enumerate(remaining[:-1]) if value == "--baseline"]
        baselines += [value.split("=", 1)[1] for value in remaining if value.startswith("--baseline=")]
        check_readiness(config_dir, baselines or list(BASELINES), environment)
        ensure_gateway(configuration, credentials, environment)
        check_container_gateway(configuration, credentials[0], environment)
    lock = None
    try:
        if preset.get("local_resource_policy") and not args.dry_run:
            if os.name == "nt":
                parser.error("the local Colima resource preset requires macOS; use an explicit generic config on Windows")
            import fcntl
            lock_path = REPO / ".tmp/local-l2-admission.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("a+")
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("another local resource-managed matrix owns admission; use one --l2-panel all driver")
        return subprocess.run(command, env=environment, cwd=REPO,
                              **({"pass_fds": (lock.fileno(),)} if lock else {})).returncode
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
