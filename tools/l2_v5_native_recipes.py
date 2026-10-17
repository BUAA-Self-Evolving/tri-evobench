#!/usr/bin/env python3
"""Materialize pinned native task recipes without invoking Docker."""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
from pathlib import Path
from typing import Any

try:
    from . import l2_v5_native_environment as native
except ImportError:
    import l2_v5_native_environment as native


def recipe_dockerfile(original: str, tests_mount: str) -> str:
    """Exclude skill copies and mount verifier setup only during build."""
    instructions = re.split(r"(?mi)(?=^(?:FROM|RUN|COPY|ADD|ARG|ENV|USER|WORKDIR|CMD|ENTRYPOINT|LABEL|EXPOSE|VOLUME|SHELL|HEALTHCHECK|STOPSIGNAL|ONBUILD)\s)", original)
    public = []
    for instruction in instructions:
        directive = native.logical_shell_lines(instruction)[0] if instruction else ""
        if re.match(r"(?:COPY|ADD)\s", instruction, re.I) and re.search(
                r"(?:^|[\s/\"'])\.?skills?(?:[\s/\"']|$)", directive, re.I):
            continue
        public.append(instruction)
    text = "".join(public).rstrip() + "\n"
    last_stage = re.split(r"(?m)^FROM\s", original)[-1]
    users = re.findall(r"(?m)^USER\s+([^\n]+)", last_stage)
    user = users[-1].strip() if users else "root"
    # Bind mounts never put private tests or setup scripts into an image layer.
    text += ("\n# Native verifier dependencies; evaluation assets remain private.\n"
             f"ARG PIP_INDEX_URL={native.BUILD_PYPI_INDEX}\n"
             f"ARG UV_INDEX_URL={native.BUILD_PYPI_INDEX}\n"
             "ARG npm_config_registry=https://registry.npmmirror.com\n"
             "ARG PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright\n"
             "ARG UV_INSTALLER_GITHUB_BASE_URL=https://ghproxy.net/https://github.com\n"
             "USER root\n"
             "RUN --mount=type=bind,source=.trievo-verifier-root-setup.sh,target=/tmp/verifier-root-setup.sh,readonly "
             f"--mount=type=bind,from=trievo_verifier,target={tests_mount},readonly "
             "bash /tmp/verifier-root-setup.sh\n"
             f"USER {user}\n"
             "RUN --mount=type=bind,source=.trievo-verifier-user-setup.sh,target=/tmp/verifier-user-setup.sh,readonly "
             f"--mount=type=bind,from=trievo_verifier,target={tests_mount},readonly "
             "bash /tmp/verifier-user-setup.sh\n")
    # Some upstream Dockerfiles lack a final newline; Docker syntax stays explicit.
    # Use Docker's bundled BuildKit frontend. An optional external syntax image
    # added an unrelated registry pull and failed with the daemon mirror's 401.
    return re.sub(r"(?m)^# syntax=.*\n", "", native.build_transport_dockerfile(text))


def materialize(row: dict[str, Any], pool: Path, outcome: dict[str, Any] | None = None) -> dict[str, Any]:
    sid = row["benchmark_id"] + ":" + row["source_task_id"]
    task = pool / sid.replace(":", "__")
    inp = native.task_inputs(row)
    info = native.source_info(row)
    metadata = native.source_metadata(row)
    category = info["labels"].get("category")
    cap = "skilllearn." + row["family"] if row["benchmark_id"] == "skilllearnbench" else "skillsbench." + str(category or "uncategorized")
    base = {"source_id": sid, "benchmark_id": row["benchmark_id"], "family": row["family"],
            "capability_id": cap, "former_admission": bool(row.get("former_admission")),
            "source_hash": native.sources.tree_sha256(row["task_dir"]),
            "license_note": native.asset_inventory(row, inp)["license_note"]}
    if not (inp["oracle"] / "solve.sh").exists():
        return {**base, "admitted": False, "stage": "source", "reason": "missing_upstream_oracle"}
    runtime_runner, setup, adaptations = native.prepare_runner((inp["tests"] / "test.sh").read_text())
    setup = native.plain_download_commands(setup)
    if row["benchmark_id"] == "skilllearnbench" and row["family"] == "stock-data-visualization":
        setup += "\ncd /tmp\nnpm install d3@6.7.0 --registry https://registry.npmmirror.com --cache /root/.npm --no-audit --no-fund\n"
    environment_dir = task / "environment"
    if environment_dir.exists():
        shutil.rmtree(environment_dir)
    shutil.copytree(inp["context"], environment_dir, symlinks=True,
                    ignore=shutil.ignore_patterns("skills", ".skills", "__pycache__"))
    root_setup, user_setup = [], []
    for line in setup.splitlines():
        (root_setup if re.match(r"apt(?:-get)?\s+(?:update|install)", line.strip()) else user_setup).append(line)
    (environment_dir / ".trievo-verifier-root-setup.sh").write_text(
        "#!/bin/bash\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n" + "\n".join(root_setup) + "\n")
    (environment_dir / ".trievo-verifier-user-setup.sh").write_text("\n".join(user_setup) + "\n")
    original = inp["dockerfile"].read_text()
    dockerfile = environment_dir / "Dockerfile"
    dockerfile.write_text(recipe_dockerfile(original, row["tests_mount"]))
    assets = task / "evaluation_assets"
    assets.mkdir(parents=True, exist_ok=True)
    # Preserve complete native assets; only standalone dependency setup changes.
    shutil.copytree(inp["tests"], assets / "native_tests", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(inp["tests"] / "test.sh", assets / "upstream-test.sh")
    (assets / "native_tests/test.sh").write_text(runtime_runner)
    shutil.copytree(inp["oracle"], assets / "native_oracle", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (task / "Dockerfile.upstream").write_text(original)
    (task / "Dockerfile.diff").write_text("".join(difflib.unified_diff(
        original.splitlines(keepends=True), dockerfile.read_text().splitlines(keepends=True),
        fromfile="upstream/environment/Dockerfile", tofile="environment/Dockerfile")))
    verifier_env, _ = native.verifier_cache_environment(native.stage_environment(metadata, "verifier"), adaptations)
    verifier_env.update(UV_OFFLINE="1", PIP_NO_INDEX="1")
    oracle_env = native.stage_environment(metadata, "oracle")
    if row["benchmark_id"] == "skilllearnbench" and row["family"] == "stock-data-visualization":
        oracle_env.update(npm_config_offline="true", npm_config_cache="/root/.npm",
                          npm_config_registry="https://registry.npmmirror.com", npm_config_audit="false", npm_config_fund="false")
    environment = {"kind": "dockerfile", "recipe_path": "environment/Dockerfile",
                   "recipe_sha256": native.digest_file(dockerfile), "context_path": "environment",
                   "context_sha256": native.sources.tree_sha256(environment_dir),
                   "build_contexts": {"trievo_verifier": {
                       "path": "evaluation_assets/native_tests",
                       "sha256": native.sources.tree_sha256(assets / "native_tests")}},
                   "upstream_dockerfile_path": "Dockerfile.upstream", "diff_path": "Dockerfile.diff",
                   "source_environment": metadata.get("environment") or metadata.get("sandbox") or {},
                   "verifier": {"command": ["bash", row["tests_mount"] + "/test.sh"],
                                "assets_path": "evaluation_assets/native_tests", "mount": row["tests_mount"],
                                "env": verifier_env, "timeout_seconds": info["verifier_timeout"],
                                "reward_path": "/logs/verifier/reward.txt"},
                   "oracle": {"command": ["bash", row["oracle_mounts"][0] + "/solve.sh"],
                              "assets_path": "evaluation_assets/native_oracle", "mount": row["oracle_mounts"][0],
                              "env": oracle_env, "timeout_seconds": max(1800, info["agent_timeout"])}}
    if row["benchmark_id"] == "skilllearnbench" and row["family"] == "travel-planning":
        helper = inp["context"] / "skills"
        if not helper.exists():
            helper = native.sources.CHECKOUTS[row["benchmark_id"]] / "skills/human_authored/travel-planning"
        private_helper = assets / "private_oracle_skills"
        if private_helper.exists():shutil.rmtree(private_helper)
        shutil.copytree(helper, private_helper, symlinks=True)
        environment["oracle"]["private_helpers"] = [{
            "assets_path": "evaluation_assets/private_oracle_skills", "mount": "/app/skills",
            "scope": "oracle_only", "sha256": native.sources.tree_sha256(private_helper)}]
    record_path = task / "pool_task.json"
    record = json.loads(record_path.read_text()) if record_path.exists() and (outcome or {}).get("admitted") else {
        "schema_version": "l2_v5_pool_task/1.0", "source_id": sid,
        "source": {"benchmark_id": row["benchmark_id"], "benchmark_revision": native.legacy.REVISIONS[row["benchmark_id"]],
                   "source_task_id": row["source_task_id"],
                   "source_path": str(row["task_dir"].relative_to(native.sources.CHECKOUTS[row["benchmark_id"]])),
                   "source_hash": base["source_hash"]},
        "prompt": info["prompt"], "benchmark_native_labels": info["labels"],
        "capability_hint": {"capability_id": cap, "family": row["family"], "category": category},
        "runtime": {"runtime_profile_id": None, "requires_gpu": False, "network": "disabled",
                    "turn_timeout_seconds": info["agent_timeout"], "verifier_timeout_seconds": info["verifier_timeout"] + 120},
        "submission": {"mode": "workspace_artifact", "payload_kind": "directory", "relative_path": ""},
        "evaluation_profile": "general", "metric_profile": None,
        "qualification": {"admitted": False, "stage": "recipe_ready"}, "license_note": base["license_note"]}
    record["environment"] = environment
    record["recipe_adaptations"] = adaptations + [{"kind": "skills_excluded_from_build_context"},
        {"kind": "ordinary_domestic_mirrors"}, {"kind": "private_verifier_bind_mount_at_build_only"}]
    if "https://astral.sh/uv/0.9.7/install.sh" in setup:
        record["recipe_adaptations"].append({"kind": "ordinary_uv_installer_mirror",
            "variable": "UV_INSTALLER_GITHUB_BASE_URL", "value": "https://ghproxy.net/https://github.com",
            "version": "0.9.7", "reason": "pinned installer release download produced no progress for about 25 minutes; installer and requested version unchanged"})
    attempted = (outcome or {}).get("environment", {})
    failed_current_recipe = (not (outcome or {}).get("admitted")
        and (outcome or {}).get("stage") not in {None, "pending", "recipe_ready", "inventory"}
        and attempted.get("recipe_sha256") == environment["recipe_sha256"]
        and attempted.get("context_sha256") == environment["context_sha256"])
    if failed_current_recipe:
        record["qualification"] = {"admitted": False, "stage": outcome["stage"],
                                   "reason": outcome.get("reason")}
        for key in ("oracle", "known_correct", "known_broken", "recipe_verification"):
            if key in outcome:record["qualification"][key] = outcome[key]
    native.dump(record_path, record)
    result = {**base, **(outcome or {}), "environment": environment}
    if not result.get("admitted") and not failed_current_recipe:
        # Historical build errors are not failures of unbuilt recipes.
        # In particular, a runner preflight is not a recipe resource readback.
        result = {**base, "environment": environment, "admitted": False,
                  "stage": "recipe_ready", "reason": None,
                  "recipe_validation": "not_built"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--old-pool", type=Path, required=True)
    parser.add_argument("--r2-inventory", type=Path, required=True)
    parser.add_argument("--skip", action="append", default=[])
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    previous = {r["source_id"]: r for r in map(json.loads, (args.pool / "INDEX.jsonl").read_text().splitlines())}
    old = {r["source_id"]: r for r in map(json.loads, (args.old_pool / "INDEX.jsonl").read_text().splitlines())}
    results = []
    for row in native.discover(args.upstream):
        sid = row["benchmark_id"] + ":" + row["source_task_id"]
        row["former_admission"] = bool(old.get(sid, {}).get("admitted"))
        if sid in args.skip or (args.only and sid not in args.only):
            results.append(previous[sid])
            continue
        outcome_path = args.work / sid.replace(":", "__") / "outcome.json"
        outcome = json.loads(outcome_path.read_text()) if outcome_path.exists() else previous.get(sid)
        results.append(materialize(row, args.pool, outcome))
        print(sid, results[-1]["stage"], flush=True)
    samples = {}
    for result in results:
        if result.get("admitted"):
            samples.setdefault(result["capability_id"], result["source_id"])
    for result in results:
        sample = samples.get(result.get("capability_id"))
        result["family_sample_verified"] = sample
        task = args.pool / result["source_id"].replace(":", "__") / "pool_task.json"
        if task.exists() and result["source_id"] not in args.skip:
            record = json.loads(task.read_text())
            record["family_sample_verified"] = sample
            native.dump(task, record)
    native.write_summaries(args.pool, results, args.old_pool, args.r2_inventory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
