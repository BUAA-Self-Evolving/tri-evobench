"""Compatibility and isolated launcher for materialized L2 stream packages.

L2 packages deliberately separate agent-visible episode data from benchmark-private
evaluation assets.  This module preserves that boundary: it validates both views
without copying private files into an agent prompt, and delegates execution to the
package's own runtime inside a disposable Ubuntu container.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class L2SpecError(ValueError):
    """Raised when a materialized L2 package violates its public contract."""


@dataclass(frozen=True)
class L2StreamInfo:
    stream_id: str
    episode_count: int
    mode: str
    workspace_snapshot: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise L2SpecError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise L2SpecError(f"JSON root must be an object: {path}")
    return value


def _under(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise L2SpecError(f"path escapes package root: {relative}")
    return candidate


def inspect_l2_package(package_dir: str | Path) -> list[L2StreamInfo]:
    """Validate public/private L2 boundaries and return an inventory.

    Validation intentionally reads private evaluation metadata only in this
    benchmark-runtime process.  Agent-facing fields are checked for private-path
    leakage but never expanded or returned as model input.
    """
    root = Path(package_dir).resolve()
    streams_root = root / "streams"
    if not streams_root.is_dir():
        raise L2SpecError(f"missing streams directory: {streams_root}")

    result: list[L2StreamInfo] = []
    for stream_dir in sorted(path for path in streams_root.iterdir() if path.is_dir()):
        manifest_path = stream_dir / "stream.json"
        if not manifest_path.is_file():
            continue
        manifest = _load_json(manifest_path)
        stream_id = manifest.get("stream_id")
        if stream_id != stream_dir.name:
            raise L2SpecError(f"{stream_dir.name}: stream_id must match its directory")
        if manifest.get("level") != "L2":
            raise L2SpecError(f"{stream_id}: level must be L2")
        workspace = manifest.get("workspace")
        ordering = manifest.get("ordering")
        episodes = manifest.get("episodes")
        runtime = manifest.get("runtime")
        if not isinstance(workspace, dict) or not isinstance(workspace.get("initial_snapshot"), str):
            raise L2SpecError(f"{stream_id}: workspace.initial_snapshot is required")
        snapshot = _under(stream_dir, str(workspace["initial_snapshot"]))
        if not snapshot.is_dir():
            raise L2SpecError(f"{stream_id}: missing workspace snapshot {snapshot}")
        if not isinstance(ordering, dict) or not isinstance(ordering.get("episode_order"), list):
            raise L2SpecError(f"{stream_id}: ordering.episode_order is required")
        if not isinstance(episodes, list) or len(episodes) != len(ordering["episode_order"]):
            raise L2SpecError(f"{stream_id}: episodes must match episode_order")
        if not isinstance(runtime, dict) or runtime.get("episode_result_policy") != "latest_interaction":
            raise L2SpecError(f"{stream_id}: runtime must declare latest_interaction policy")

        for expected_id, relative in zip(ordering["episode_order"], episodes):
            if not isinstance(relative, str):
                raise L2SpecError(f"{stream_id}: episode path must be text")
            episode_path = _under(stream_dir, relative)
            episode = _load_json(episode_path)
            if episode.get("episode_id") != expected_id or episode.get("stream_id") != stream_id:
                raise L2SpecError(f"{stream_id}/{expected_id}: identity mismatch")
            agent_view = episode.get("agent_view")
            if not isinstance(agent_view, dict):
                raise L2SpecError(f"{stream_id}/{expected_id}: agent_view is required")
            visible = json.dumps(agent_view, ensure_ascii=False)
            if "evaluation.json" in visible or "evaluation_assets" in visible:
                raise L2SpecError(f"{stream_id}/{expected_id}: private evaluation path leaked into agent_view")
            policy = episode.get("interaction_policy")
            if not isinstance(policy, dict) or not isinstance(policy.get("max_interactions"), int):
                raise L2SpecError(f"{stream_id}/{expected_id}: interaction_policy.max_interactions is required")
            if not 1 <= int(policy["max_interactions"]) <= 3:
                raise L2SpecError(f"{stream_id}/{expected_id}: max_interactions must be 1..3")
            evaluation_ref = episode.get("evaluation_ref")
            if not isinstance(evaluation_ref, str):
                raise L2SpecError(f"{stream_id}/{expected_id}: evaluation_ref is required")
            evaluation = _load_json(_under(episode_path.parent, evaluation_ref))
            if evaluation.get("visibility") != "benchmark_private":
                raise L2SpecError(f"{stream_id}/{expected_id}: evaluation must be benchmark_private")
            scoring = evaluation.get("scoring")
            if not isinstance(scoring, dict) or scoring.get("episode_score_policy") != "latest_interaction":
                raise L2SpecError(f"{stream_id}/{expected_id}: scorer must use latest_interaction")

        mode = "api" if runtime.get("runner") else "browser-smoke"
        result.append(L2StreamInfo(str(stream_id), len(episodes), mode, str(workspace["initial_snapshot"])))
    if not result:
        raise L2SpecError(f"no L2 streams found in {streams_root}")
    return result


def run_l2_in_docker(
    *,
    package_dir: str | Path,
    stream_id: str,
    output_root: str | Path,
    image: str,
    model: str | None,
    api_base: str | None,
    reasoning_effort: str,
    allow_generated_code: bool,
) -> int:
    """Run one L2 Stream in a constrained, disposable Docker container.

    The package source is read-only.  The only host write mount is ``output_root``.
    The API key, when needed, is inherited by name rather than serialized to a
    command line or a run artifact.  L2 generated-code verifiers still need their
    explicit opt-in because Docker is containment, not proof of safe execution.
    """
    root = Path(package_dir).resolve()
    streams = {item.stream_id: item for item in inspect_l2_package(root)}
    if stream_id not in streams:
        raise L2SpecError(f"unknown stream: {stream_id}")
    info = streams[stream_id]
    if not allow_generated_code:
        raise L2SpecError("L2 execution requires --allow-generated-code")
    if info.mode == "api" and not os.environ.get("L2_API_KEY"):
        raise L2SpecError("L2_API_KEY must be set in the environment for an API Stream")

    destination = Path(output_root).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        "docker", "run", "--rm", "--name", f"trievo-l2-{uuid.uuid4().hex[:12]}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
        "-v", f"{root}:/opt/l2_demo:ro", "-v", f"{destination}:/output",
        "-e", "PYTHONPATH=/opt/l2_demo/runtime/src", "-e", "MPLBACKEND=Agg",
    ]
    if info.mode == "api":
        command.extend(["-e", "L2_API_KEY"])
        command.extend(["-e", f"L2_API_BASE={api_base or os.environ.get('L2_API_BASE', 'https://api.deepseek.com/v1')}"])
        command.extend(["-e", f"L2_MODEL={model or os.environ.get('L2_MODEL', 'deepseek-v4-flash')}"])
    command.extend([image, "python", "/opt/l2_demo/demo.py"])
    if info.mode == "api":
        command.extend([
            "run", stream_id, "--model", model or os.environ.get("L2_MODEL", "deepseek-v4-flash"),
            "--reasoning-effort", reasoning_effort, "--output-root", "/output",
            "--allow-unsafe-code-execution",
        ])
        if api_base:
            command.extend(["--api-base", api_base])
    else:
        command.extend(["smoke", stream_id])
    return subprocess.run(command, check=False).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or run a materialized L2 demo package.")
    parser.add_argument("--package", required=True, help="extracted l2_demo directory")
    parser.add_argument("--stream", help="stream id; omit with --inspect")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", default="runs/l2")
    parser.add_argument("--image", default="trievo-l2:0.1")
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--allow-generated-code", action="store_true")
    args = parser.parse_args()
    inventory = inspect_l2_package(args.package)
    if args.inspect:
        print(json.dumps([asdict(item) for item in inventory], ensure_ascii=False, indent=2))
        return
    if not args.stream:
        raise SystemExit("--stream is required unless --inspect is used")
    raise SystemExit(run_l2_in_docker(
        package_dir=args.package,
        stream_id=args.stream,
        output_root=args.output,
        image=args.image,
        model=args.model,
        api_base=args.api_base,
        reasoning_effort=args.reasoning_effort,
        allow_generated_code=args.allow_generated_code,
    ))


if __name__ == "__main__":
    main()
