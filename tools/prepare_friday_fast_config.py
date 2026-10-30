"""Prepare a new, credential-free 6-account Friday preset (8 workers locally)."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.prepare_matrix_configs import generate
from trievo_bench.baselines.manifest import find_manifest, load_manifest
from trievo_bench.friday_gateway import DEFAULT_POLICY, GATEWAY_SCHEMA, validate_host_gateway_url
from trievo_bench.provider_profiles import FRIDAY_MODELS, _BASE_URL_ALIASES, _KEY_ALIASES


def prepare(output: Path, *, port=18765, container_host="host.lima.internal", dsh_source: Path | None = None, global_workers=8, model: str | None = None):
    output = output.resolve()
    if output.exists():
        raise ValueError("fast config output already exists; select a new directory")
    if type(global_workers) is not int or not 1 <= global_workers <= 12:
        raise ValueError("global workers must be within the six-slot capacity, 1..12")
    container_url = f"http://{container_host}:{port}/v1/openai/native"
    parsed = urlsplit(container_url)
    if (parsed.hostname != container_host or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port != port):
        raise ValueError("invalid Docker-reachable gateway host")
    host_url = validate_host_gateway_url(f"http://127.0.0.1:{port}/v1/openai/native")
    from trievo_bench.provider_profiles import get_provider_profile
    request_model = get_provider_profile("friday", model=model).model
    generate(output, provider_mode="friday", model=request_model, base_url=container_url, inherit_proxy=False)
    native_limits = {"max_model_calls": 24, "max_tool_calls": 20,
                     "episode_timeout_seconds": 900, "max_output_tokens": 12000}
    for baseline in ("codex", "zcode"):
        manifest = load_manifest(find_manifest(baseline, REPO))
        override = {"adapter": {"command": manifest.adapter["command"]}, "options": {
            "model": request_model, "base_url": container_url,
            "api_key_env": "TRIEVO_API_KEY", "l2_agent_limits": native_limits,
        }}
        (output / f"tmp_{baseline}_docker.override.json").write_text(json.dumps(override, indent=2) + "\n")
    if dsh_source:
        raw = dsh_source.read_bytes()
        override = json.loads(raw)
        override["options"]["base_url"] = container_url
        if model is not None:
            override["options"]["model"] = request_model
        (output / "tmp_deepseek-harness_docker.override.json").write_text(json.dumps(override, indent=2) + "\n")
        (output / "dsh-source.json").write_text(json.dumps({"sha256": hashlib.sha256(raw).hexdigest(),
            "note": "Source execution/session/budget options preserved; provider endpoint replaced."}, indent=2) + "\n")
    # Host evaluator calls use loopback; adapter containers use their reachable
    # host alias. Explicit options and inherited variables agree on the route.
    for path in output.glob("*.override.json"):
        override = json.loads(path.read_text())
        adapter = override.setdefault("adapter", {})
        command = adapter["command"]
        image = command.pop()
        for name in _BASE_URL_ALIASES:
            if name not in command:
                command.extend(["-e", name])
        command.append(image)
        environment = override.setdefault("environment", {})
        environment["inherit"] = sorted(set(environment.get("inherit", [])) | set(_BASE_URL_ALIASES) | set(_KEY_ALIASES))
        environment.setdefault("set", {}).update({name: container_url for name in _BASE_URL_ALIASES})
        path.write_text(json.dumps(override, indent=2) + "\n")
    profile_path = output / "provider-profile.json"
    profile = json.loads(profile_path.read_text())
    profile["friday_gateway"] = {"schema": GATEWAY_SCHEMA, "host_base_url": host_url,
                                "container_base_url": container_url, "policy": DEFAULT_POLICY}
    profile["model"] = request_model
    if model is not None:
        profile["friday_gateway"]["model"] = request_model
    profile_path.write_text(json.dumps(profile, indent=2) + "\n")
    capacities = {f"FRIDAY_APP_ID_{index}": 2 for index in range(1, 7)}
    (output / "friday-capacities.json").write_text(json.dumps(capacities, indent=2) + "\n")
    preset = {"schema": "friday_fast_preset/1.0", "global_workers": global_workers,
              "worker_capacities": capacities, "request_policy": DEFAULT_POLICY,
              "job_error_policy": "stop", "storage_guard": True,
              "scope": "new output roots only; no changes to benchmark budgets, thinking or scoring",
              "native_readiness": "Codex/ZCode image, revision and Friday protocol support require separate preflight."}
    (output / "execution-preset.json").write_text(json.dumps(preset, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--model", choices=FRIDAY_MODELS)
    parser.add_argument("--global-workers", type=int, default=8, help="default 8 for this 8-CPU/~10-GiB Docker host; maximum 12")
    parser.add_argument("--container-host", default="host.lima.internal", help="Colima default; Docker Desktop uses host.docker.internal")
    parser.add_argument("--dsh-source-config", type=Path, help="copy DSH execution options from a verified config without modifying it")
    args = parser.parse_args()
    print(prepare(args.output, port=args.port, container_host=args.container_host, dsh_source=args.dsh_source_config, global_workers=args.global_workers, model=args.model))


if __name__ == "__main__":
    main()
