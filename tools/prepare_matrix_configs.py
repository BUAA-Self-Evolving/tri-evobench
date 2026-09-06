"""Generate machine-local, credential-free Docker configs for the current matrix."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

PROXY_ENV_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


def _add_passthrough_env(command, names):
    image = command.pop()
    existing = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-e"
    }
    for name in names:
        if name not in existing:
            command.extend(["-e", name])
    command.append(image)
    return command


def generate(
    output: Path,
    model: str,
    base_url: str,
    *,
    l2_max_output_tokens: int = 12000,
    api_timeout_seconds: int = 150,
    api_max_retries: int = 4,
):
    if not 1 <= l2_max_output_tokens <= 1_000_000:
        raise ValueError("l2_max_output_tokens must be between 1 and 1000000")
    if not 1 <= api_timeout_seconds <= 3600:
        raise ValueError("api_timeout_seconds must be between 1 and 3600")
    if not 0 <= api_max_retries <= 20:
        raise ValueError("api_max_retries must be between 0 and 20")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    limits = {
        "max_model_calls": 24,
        "max_tool_calls": 20,
        "episode_timeout_seconds": 900,
        "max_output_tokens": l2_max_output_tokens,
    }

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    hermes = json.loads((repo / "trievo_bench/baselines/manifests/hermes.json").read_text(encoding="utf-8"))
    original = hermes["adapter"]["command"]
    command = []
    index = 0
    while index < len(original):
        # Do not assume this workstation's proxy exists on collaborators' hosts.
        if original[index] == "-e" and original[index + 1].startswith(("HTTP_PROXY=", "HTTPS_PROXY=", "NO_PROXY=")):
            index += 2
        else:
            command.append(original[index])
            index += 1
    command = _add_passthrough_env(command, PROXY_ENV_NAMES)
    for level, cap in (("l1", 1200), ("l2", l2_max_output_tokens)):
        seed = output / f"hermes-{level}-seed"
        seed.mkdir(exist_ok=True)
        # JSON scalars are valid YAML; no credentials are written to disk.
        # A relay can expose arbitrary model IDs.  Hermes' first-party DeepSeek
        # provider normalizes unknown IDs, so route relay traffic as custom.
        (seed / "config.yaml").write_text(
            f"model:\n  default: {json.dumps(model)}\n  provider: custom\n"
            f"  base_url: {json.dumps(base_url)}\n  max_tokens: {cap}\n"
            "auxiliary:\n  title_generation:\n    enabled: false\n"
            f"agent:\n  reasoning_effort: none\n  api_max_retries: {api_max_retries}\n"
            f"providers:\n  custom:\n    base_url: {json.dumps(base_url)}\n"
            "    key_env: OPENAI_API_KEY\n    api_mode: chat_completions\n"
            f"    request_timeout_seconds: {api_timeout_seconds}\n",
            encoding="utf-8",
        )
        save(f"tmp_hermes_cf_{level}.override.json", {
            "adapter": {"command": command},
            "state": {"seed_from": str(seed), "seed_files": ["config.yaml"]},
            "options": {"image": "trievo-hermes:matrix-current", "approval_policy": "session",
                        "l2_agent_limits": limits},
        })

    for agent, image, env_names, options in (
        ("skillopt", "trievo-skillopt:matrix-current",
         ["OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_MODEL",
          "TARGET_OPENAI_COMPATIBLE_BASE_URL", "TARGET_OPENAI_COMPATIBLE_API_KEY", "TARGET_OPENAI_COMPATIBLE_MODEL"],
         {"target_model": model, "optimizer_model": model, "target_backend": "openai_compatible",
          "optimizer_backend": "openai_compatible", "openai_compatible_base_url": base_url,
          "openai_compatible_model": model, "max_completion_tokens": 4096,
          "native_tool_rounds": 16, "exec_timeout": 90, "retries": 1}),
        ("trace2skill", "trievo-trace2skill:matrix-current", ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
         {"model": model, "max_turns": 24, "request_timeout": 120, "tool_timeout": 90,
          "generation_config": {"thinking": {"type": "disabled"}}}),
    ):
        container = f"trievo-{agent}-{{instance_id}}"
        command = ["docker", "run", "--rm", "-i", "--name", container,
                   "-v", "{workspace_mount}:/workspace", "-v", "{state_mount}:/state"]
        for name in env_names + [
            "TRIEVO_BASE_URL",
            "TRIEVO_API_KEY",
            "TRIEVO_MODEL",
            "TRIEVO_THINKING",
            "TRIEVO_API_TIMEOUT_SECONDS",
            "TRIEVO_API_MAX_RETRIES",
            *PROXY_ENV_NAMES,
        ]:
            command.extend(["-e", name])
        command.append(image)
        config = {
            "adapter": {"command": command, "cwd": "{repo_root}",
                        "cleanup_command": ["docker", "rm", "-f", container]},
            "workspace": {"mode": "shared_volume", "adapter_path": "/workspace"},
            "state": {"adapter_path": "/state"},
            "environment": {"set": {"TRIEVO_THINKING": "disabled"}},
            "options": {**options, "l2_agent_limits": limits},
        }
        save(f"tmp_{agent}_docker.override.json", config)
        if agent == "skillopt":
            save("tmp_skillopt_docker_l2.override.json", {
                **config,
                "options": {
                    **config["options"],
                    "max_completion_tokens": l2_max_output_tokens,
                },
            })
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/matrix-config"))
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--base-url", default="https://cf.api.fan/v1")
    parser.add_argument("--l2-max-output-tokens", type=int, default=12000)
    parser.add_argument("--api-timeout-seconds", type=int, default=150)
    parser.add_argument("--api-max-retries", type=int, default=4)
    args = parser.parse_args()
    print(generate(
        args.output,
        args.model,
        args.base_url,
        l2_max_output_tokens=args.l2_max_output_tokens,
        api_timeout_seconds=args.api_timeout_seconds,
        api_max_retries=args.api_max_retries,
    ))


if __name__ == "__main__":
    main()
