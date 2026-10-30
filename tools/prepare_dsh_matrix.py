"""Generate a credential-free DSH plan for the existing full benchmark."""
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tools.run_new_package_matrix import jobs, save
from trievo_bench.provider_profiles import get_provider_profile


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    l1 = REPO / "benchmarks/current/l1_arena"
    l2 = REPO.parent / "tri-evobench-offline-opencode-dcbcc00/benchmarks/current/L2_Demo_V2"
    conditions = ["reset", "frozen", "evolving"]
    rows = jobs(REPO, root, l1_package=l1, l2_package=l2,
                l1_conditions=conditions, l2_conditions=conditions, baselines=["deepseek-harness"])
    assert len(rows) == 87 and len({row["id"] for row in rows}) == 87
    config = root / "config"
    profile = get_provider_profile("friday")
    save(config / "provider-profile.json", profile.public_metadata())
    save(config / "tmp_deepseek-harness_docker.override.json", {
        "options": {"image": "trievo-deepseek-harness:friday-20261011-deadline-v3",
                    "model": "deepseek-v4-flash", "base_url": profile.base_url,
                    "provider_transport": "friday_chat", "api_key_env": "TRIEVO_API_KEY",
                    "reasoning_effort": "high", "max_output_tokens": 16384,
                    "upstream_timeout_seconds": 300, "request_timeout_seconds": 600,
                    "l2_agent_limits": {"max_model_calls": 24, "max_tool_calls": 40,
                                        "episode_timeout_seconds": 900, "max_output_tokens": 16384}},
        "evolution": {"policy": "disabled"}})
    save(root / "run_plan.json", {"model": "deepseek-v4-flash", "baseline": "deepseek-harness",
        "concurrency": 6, "credential_slots": [f"FRIDAY_APP_ID_{i}" for i in range(1, 7)],
        "l1_package": str(l1), "l2_package": str(l2), "conditions": conditions,
        "expected_cells": len(rows), "expected_turns": sum(len(row["turns"]) for row in rows),
        "harness_learning": "unsupported; native disabled barrier in all conditions",
        "accounting": "valid probes retain their scores; exceptional probes zero; pending stays pending",
        "jobs": rows})
    print(json.dumps({"cells": len(rows), "turns": sum(len(row["turns"]) for row in rows), "root": str(root)}))


if __name__ == "__main__":
    main()
