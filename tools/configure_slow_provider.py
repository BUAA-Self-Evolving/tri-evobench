"""Derive L2 slow-provider configs without changing source configs or task data."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

FILES = {
    "hermes": "tmp_hermes_cf_l2.override.json",
    "skillopt": "tmp_skillopt_docker_l2.override.json",
    "trace2skill": "tmp_trace2skill_docker_l2.override.json",
}


def configure(source: Path, output: Path, *, request_timeout=600, retries=20,
              interaction_timeout=21600, evolution_timeout=14400, images=None):
    if source.resolve() == output.resolve():
        raise ValueError("source and output configs must be separate")
    if not 1 <= request_timeout <= 3600 or not 0 <= retries <= 100:
        raise ValueError("invalid request timeout/retry budget")
    # A full failed request retry series must fit inside either outer budget.
    request_budget = request_timeout * (retries + 1) + 65 * retries
    if not request_budget < min(interaction_timeout, evolution_timeout) <= 86400:
        raise ValueError("outer budgets must exceed the complete request retry budget and be <= 86400")
    if max(interaction_timeout, evolution_timeout) > 86400:
        raise ValueError("outer budgets must be <= 86400")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output config directory must be empty")
    configs = {}
    for agent, name in FILES.items():
        config = copy.deepcopy(json.loads((source / name).read_text()))
        options = config.setdefault("options", {})
        options["slow_provider"] = True
        options.setdefault("l2_agent_limits", {}).update(
            episode_timeout_seconds=interaction_timeout,
            evolution_timeout_seconds=evolution_timeout,
        )
        env = config.setdefault("environment", {}).setdefault("set", {})
        env.update(TRIEVO_SLOW_PROVIDER="1", TRIEVO_API_TIMEOUT_SECONDS=str(request_timeout),
                   TRIEVO_API_MAX_RETRIES=str(retries))
        command = config["adapter"]["command"]
        # Generated Docker manifests keep the image as the final argument.
        if not command or command[0] != "docker" or "run" not in command:
            raise ValueError("slow-provider overlay expects generated Docker configs")
        image = command.pop()
        if images and agent in images:
            image = images[agent]
            options["image"] = image
        existing = {command[i + 1] for i, word in enumerate(command[:-1]) if word == "-e"}
        for key in ("TRIEVO_SLOW_PROVIDER", "TRIEVO_API_TIMEOUT_SECONDS", "TRIEVO_API_MAX_RETRIES"):
            if key not in existing:
                command.extend(["-e", key])
        command.append(image)
        if agent == "skillopt":
            options.update(exec_timeout=request_timeout, retries=retries)
        elif agent == "trace2skill":
            options["request_timeout"] = request_timeout
        configs[name] = config
    output.mkdir(parents=True, exist_ok=True)
    for name, config in configs.items():
        (output / name).write_text(json.dumps(config, indent=2) + "\n")
    profile = {"timing_profile": "slow_provider", "request_timeout_seconds": request_timeout,
               "max_retries": retries, "interaction_timeout_seconds": interaction_timeout,
               "evolution_timeout_seconds": evolution_timeout,
               "dataset_wall_time_overridden": True,
               "source_config_dir": str(source.resolve())}
    (output / "slow-provider-profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--request-timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=20)
    parser.add_argument("--interaction-timeout", type=int, default=21600)
    parser.add_argument("--evolution-timeout", type=int, default=14400)
    for agent in FILES:
        parser.add_argument(f"--{agent}-image")
    args = parser.parse_args()
    images = {agent: getattr(args, agent + "_image") for agent in FILES if getattr(args, agent + "_image")}
    print(json.dumps(configure(args.source, args.output, request_timeout=args.request_timeout,
                               retries=args.retries, interaction_timeout=args.interaction_timeout,
                               evolution_timeout=args.evolution_timeout, images=images), indent=2))


if __name__ == "__main__":
    main()
