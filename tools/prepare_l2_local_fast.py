"""Prepare new local L2 configs from audited baseline configs, without model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from trievo_bench.local_resources import DEFAULT_WEIGHTS, validate_policy


def prepare(source: Path, output: Path, *, hermes_source: Path, prime_image: str,
            port: int = 18767, docker: str = "docker", calibration: Path | None = None) -> Path:
    if output.exists():
        raise ValueError("select a new configuration directory")
    info = json.loads(subprocess.check_output([docker, "info", "--format", "{{json .}}"], text=True))
    image = subprocess.check_output([docker, "image", "inspect", prime_image, "--format", "{{.Id}}"], text=True).strip()
    heavy_workers = 2
    if calibration:
        measured = json.loads(calibration.read_text())
        maximum = min(4, info["NCPU"] // 4, int((info["MemTotal"] / 1024**3 - 1.25) // 3))
        candidates = [r for r in measured["results"] if r["workers"] <= maximum]
        best = max(r["jobs_per_hour"] for r in candidates)
        heavy_workers = min(r["workers"] for r in candidates if r["jobs_per_hour"] >= .95 * best)
    policy = validate_policy({"schema": "local_resource_policy/1.0", "docker_cpus": info["NCPU"],
                              "docker_memory_gib": info["MemTotal"] / 1024**3,
                              "max_workers": 4, "tau2_workers": 2,
                              "heavy_workers": heavy_workers, "docker_min_free_gib": 5,
                              "queue_priority": {"tau2": 0, "heavy": 1, "sra": 2},
                              "memory_reserve_gib": 1.25, "minimum_available_gib": .75,
                              "max_cpu_utilization": .85, "sample_interval_seconds": 5,
                              "weights": DEFAULT_WEIGHTS})
    required = ["tmp_prime-agent_docker.override.json", "tmp_adaptive-auto-harness_docker.override.json",
                "tmp_deepseek-harness_docker.override.json", "tmp_opencode_docker.override.json",
                "provider-profile.json", "friday-capacities.json"]
    values = {name: json.loads((source / name).read_text()) for name in required}
    source_paths = [source / name for name in required]
    for name in ["tmp_hermes_cf_l1.override.json", "tmp_hermes_cf_l2.override.json"]:
        path = hermes_source / name
        if not path.is_file() and name == "tmp_hermes_cf_l1.override.json":
            path = source / name
        values[name] = json.loads(path.read_text())
        source_paths.append(path)
    originals = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    profile = values["provider-profile.json"]
    gateway = profile["friday_gateway"]
    old_host, old_container = gateway["host_base_url"], gateway["container_base_url"]
    gateway["host_base_url"] = f"http://127.0.0.1:{port}/v1/openai/native"
    gateway["container_base_url"] = f"http://host.lima.internal:{port}/v1/openai/native"
    output.mkdir(parents=True)
    for name, value in values.items():
        if name.endswith(".override.json"):
            # Rewrite only the existing public endpoint strings. Preserve all
            # source/archive identities, budgets, state and native repair flags.
            value = json.loads(json.dumps(value).replace(old_container, gateway["container_base_url"])
                               .replace(old_host, gateway["host_base_url"]))
            value.setdefault("options", {})["l2_limit_cpu_threads"] = True
            value["options"]["l2_verifier_input_copy"] = True
            seed = value.get("state", {}).get("seed_from")
            if seed:
                seed = Path(seed)
                seed = seed if seed.is_absolute() else REPO / seed
                target = output / (name.removesuffix(".override.json") + "-seed")
                shutil.copytree(seed, target)
                for path in target.rglob("*"):
                    if path.is_file():
                        original_path = seed / path.relative_to(target)
                        originals[str(original_path)] = hashlib.sha256(original_path.read_bytes()).hexdigest()
                        try:
                            text = path.read_text()
                        except UnicodeDecodeError:
                            continue
                        path.write_text(text.replace(old_container, gateway["container_base_url"])
                                        .replace(old_host, gateway["host_base_url"]))
                value["state"]["seed_from"] = str(target)
            if name == "tmp_prime-agent_docker.override.json":
                value["adapter"]["command"][-1] = image
                value["options"]["image"] = image
        (output / name).write_text(json.dumps(value, indent=2) + "\n")
    (output / "local-resource-policy.json").write_text(json.dumps(policy, indent=2) + "\n")
    capacities = values["friday-capacities.json"]
    preset = {"schema": "friday_fast_preset/1.0", "global_workers": 4,
              "worker_capacities": capacities, "job_error_policy": "continue",
              "request_policy": gateway["policy"], "storage_guard": True,
              "local_resource_policy": "local-resource-policy.json",
              "fast_checkpoints": True, "performance_timings": True,
              "storage_min_free_gib": 20, "storage_resume_free_gib": 25,
              "baselines": ["hermes", "prime-agent", "adaptive-auto-harness", "opencode", "deepseek-harness"],
              "level": "l2", "l2_package": "benchmarks/current/L2_Demo_V5_r2_intra10",
              "l2_panel": "all", "l2_conditions": ["reset", "frozen", "evolving"],
              "scope": "new roots only; existing task budgets, model, thinking and state contracts preserved"}
    (output / "execution-preset.json").write_text(json.dumps(preset, indent=2) + "\n")
    (output / "source-configs.json").write_text(json.dumps({"original_sha256": originals,
        "prime_image": image, "environment_difference": "Preinstalled public scientific libraries; default Python uses declared Prime kernel; BLAS threads capped at declared task CPUs.",
        "calibration_sha256": hashlib.sha256(calibration.read_bytes()).hexdigest() if calibration else None,
        "hardware": {"docker_cpus": info["NCPU"], "docker_memory_bytes": info["MemTotal"]}}, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--hermes-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prime-image", default="trievo-prime-agent:local-fast-20261027")
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--calibration", type=Path, help="zero-API sustained local throughput probe JSON")
    args = parser.parse_args()
    docker = shutil.which("docker") or str(Path.home() / ".local/share/trievo-container-tools/bin/docker")
    print(prepare(args.source_config.resolve(), args.output.resolve(), hermes_source=args.hermes_config.resolve(),
                  prime_image=args.prime_image, port=args.port, docker=docker, calibration=args.calibration))


if __name__ == "__main__":
    main()
