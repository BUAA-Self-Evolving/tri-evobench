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
    hermes_image: str = "trievo-hermes:matrix-current",
    skillopt_image: str = "trievo-skillopt:matrix-current",
    trace2skill_image: str = "trievo-trace2skill:matrix-current",
    inherit_proxy: bool = True,
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
    # The repaired SkillsBench package declares a 40-call ceiling. Other
    # streams keep their package-local 20-call ceiling through
    # _effective_work_limits(), so this does not silently widen them.
    l2_limits = {**limits, "max_tool_calls": 40}

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    hermes = json.loads((repo / "trievo_bench/baselines/manifests/hermes.json").read_text(encoding="utf-8"))
    original = hermes["adapter"]["command"]
    command = []
    index = 0
    while index < len(original):
        # Do not assume this workstation's proxy exists on collaborators' hosts.
        if original[index] == "-e" and any(
            original[index + 1] == name or original[index + 1].startswith(name + "=")
            for name in PROXY_ENV_NAMES
        ):
            index += 2
        else:
            command.append(original[index])
            index += 1
    command = _add_passthrough_env(command, PROXY_ENV_NAMES if inherit_proxy else ())
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
            "    extra_body:\n      thinking:\n        type: disabled\n"
            "      reasoning:\n        enabled: false\n"
            f"    request_timeout_seconds: {api_timeout_seconds}\n",
            encoding="utf-8",
        )
        save(f"tmp_hermes_cf_{level}.override.json", {
            "adapter": {"command": command},
            "state": {"seed_from": str(seed), "seed_files": ["config.yaml"]},
            "options": {"image": hermes_image, "approval_policy": "session",
                        "thinking": "disabled",
                        "l2_agent_limits": l2_limits if level == "l2" else limits},
        })

    for agent, image, env_names, options in (
        ("skillopt", skillopt_image,
         ["OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_MODEL",
          "TARGET_OPENAI_COMPATIBLE_BASE_URL", "TARGET_OPENAI_COMPATIBLE_API_KEY", "TARGET_OPENAI_COMPATIBLE_MODEL"],
         {"target_model": model, "optimizer_model": model, "target_backend": "openai_compatible",
          "optimizer_backend": "openai_compatible", "openai_compatible_base_url": base_url,
          "openai_compatible_model": model, "max_completion_tokens": 4096,
          "native_tool_rounds": 16, "exec_timeout": 90, "retries": 1}),
        ("trace2skill", trace2skill_image, ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
         {"model": model, "max_turns": 24, "request_timeout": 120, "tool_timeout": 90,
          "generation_config": {"thinking": {"type": "disabled"}}}),
        ("prime-agent", "trievo-prime-agent:matrix-current", ["OPENAI_API_KEY"],
         {"provider": "trievo", "model": model, "base_url": base_url,
          "api": "openai-completions", "api_key_env": "TRIEVO_API_KEY",
          "thinking": "off", "reasoning": False, "max_output_tokens": 4096}),
        ("adaptive-auto-harness", "trievo-adaptive-auto-harness:matrix-current",
         ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
         {"model": model, "base_url": base_url, "api_key_env": "TRIEVO_API_KEY",
          "max_output_tokens": 4096, "max_steps": 20, "tool_timeout": 90,
          "evolver_model": model, "evolver_base_url": base_url,
          "evolver_api_key_env": "TRIEVO_API_KEY", "evolver_max_tokens": 4096,
          "evolve_prompts": True, "evolve_skills": True,
          "evolve_memory": True, "evolve_tools": False}),
    ):
        container = f"trievo-{agent}-{{instance_id}}"
        command = ["docker", "run", "--rm", "-i", "--name", container,
                   "--user", "{uid}:{gid}",
                   "-v", "{workspace_mount}:/workspace", "-v", "{state_mount}:/state"]
        for name in env_names + [
            "TRIEVO_BASE_URL",
            "TRIEVO_API_KEY",
            "TRIEVO_MODEL",
            "TRIEVO_THINKING",
            "TRIEVO_API_TIMEOUT_SECONDS",
            "TRIEVO_API_MAX_RETRIES",
            *(PROXY_ENV_NAMES if inherit_proxy else ()),
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
                    "l2_agent_limits": l2_limits,
                    "max_completion_tokens": l2_max_output_tokens,
                    "openai_compatible_max_tokens": l2_max_output_tokens,
                },
            })
        elif agent == "trace2skill":
            save("tmp_trace2skill_docker_l2.override.json", {
                **config,
                "options": {
                    **config["options"],
                    "l2_agent_limits": l2_limits,
                    "request_timeout": api_timeout_seconds,
                    "max_tokens": l2_max_output_tokens,
                    "evolution_max_tokens": l2_max_output_tokens,
                    "generation_config": {
                        **config["options"]["generation_config"],
                        "max_tokens": l2_max_output_tokens,
                    },
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
    parser.add_argument("--hermes-image", default="trievo-hermes:matrix-current")
    parser.add_argument("--skillopt-image", default="trievo-skillopt:matrix-current")
    parser.add_argument("--trace2skill-image", default="trievo-trace2skill:matrix-current")
    parser.add_argument(
        "--direct-api",
        action="store_true",
        help="do not inherit host proxy variables into baseline containers",
    )
    args = parser.parse_args()
    print(generate(
        args.output,
        args.model,
        args.base_url,
        l2_max_output_tokens=args.l2_max_output_tokens,
        api_timeout_seconds=args.api_timeout_seconds,
        api_max_retries=args.api_max_retries,
        hermes_image=args.hermes_image,
        skillopt_image=args.skillopt_image,
        trace2skill_image=args.trace2skill_image,
        inherit_proxy=not args.direct_api,
    ))


if __name__ == "__main__":
    main()
