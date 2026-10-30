"""Adapter for ClawArena-derived L1-Arena packages.

L1-Arena keeps a persistent workspace and evaluates each turn with the
package-owned executable checker.  This module translates the public task
messages into the existing Tri-EvoBench Runner while keeping evaluation
scripts outside the agent-visible request.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .baselines.manifest import load_mapping
from .runner import Runner
from .spec import BenchmarkSpec


def _workspace_files(workspace: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files.append({"path": "/workspace/" + path.relative_to(workspace).as_posix(), "content": content})
    return files


def _task_request(task: dict[str, Any], tools: dict[str, Any]) -> str:
    agent_input = task.get("agent_input", {})
    messages = agent_input.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"{task.get('turn_id', '<unknown>')} has no agent_input.messages")
    transcript = "\n\n".join(
        f"{str(item.get('role', 'user')).upper()}:\n{str(item.get('content', ''))}"
        for item in messages if isinstance(item, dict)
    )
    names = [str(item) for item in agent_input.get("available_tools", [])]
    schemas = {name: tools[name] for name in names if name in tools}
    return (
        "You are executing an L1-Arena task in a persistent /workspace. "
        "Use the native terminal and file tools to inspect and edit only /workspace; "
        "leave every required artifact there and report what was actually executed.\n\n"
        "The public session store is materialized at /workspace/.session_store: sessions.json lists sessions, "
        "and each listed JSONL file contains its messages. Native file/terminal tools can read these records "
        "as the equivalent of the logical session-list/history tools.\n\n"
        "ClawArena logical tools visible to the task:\n" + json.dumps(schemas, ensure_ascii=False) +
        "\n\nPUBLIC TASK TRANSCRIPT:\n" + transcript
    )


def _checker_path(stream_dir: Path, task: dict[str, Any]) -> Path:
    command = str(task.get("evaluation", {}).get("exec_check", {}).get("command", ""))
    match = re.search(r"check_q(\d+)\.py", command)
    if not match:
        raise ValueError(f"{task.get('turn_id', '<unknown>')} has no supported check_qN.py command")
    path = stream_dir / "evaluation" / "scripts" / f"check_q{match.group(1)}.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def load_l1_arena_stream(
    package: str | Path,
    stream: str = "stream-1",
    turns: list[str] | None = None,
    backend: str = "docker",
    image: str = "trievo-ubuntu:22.04",
) -> BenchmarkSpec:
    root = Path(package).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "l1_arena/1.5":
        raise ValueError("expected an l1_arena/1.5 package")
    tools_doc = json.loads((root / "tools.json").read_text(encoding="utf-8"))
    tools = tools_doc.get("tools", {}) if isinstance(tools_doc, dict) else {}
    stream_dir = root / stream
    workspace = stream_dir / "agent_workspace" / "workspace"
    if not workspace.is_dir():
        raise FileNotFoundError(workspace)
    selected = turns or ["turn1"]
    converted: list[dict[str, Any]] = []
    for index, turn_id in enumerate(selected):
        task_path = stream_dir / turn_id / "task.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        available = [str(item) for item in task.get("agent_input", {}).get("available_tools", [])]
        missing = [name for name in available if name not in tools]
        if missing:
            raise ValueError(f"{task_path}: tools missing from tools.json: {', '.join(missing)}")
        evaluation = task.get("evaluation", {})
        converted.append({
            "id": turn_id,
            "request": _task_request(task, tools),
            "max_interactions": int(task.get("user_simulator", {}).get("continuation_policy", {}).get("max_total_interactions", 3)),
            "success_threshold": float(evaluation.get("success_policy", {}).get("turn_score_threshold", 1.0)),
            # DockerSandbox has a shared host bind mount; bulk-copy the
            # fixture there to avoid Win32 command-line limits from encoding
            # every documentation file into individual `docker exec` calls.
            "setup": {
                **({"host_copy": str(workspace), "arena_session_seed": str(stream_dir / "agent_workspace" / "session_store")} if index == 0 else {}),
                "arena_updates": [{**update, "stream_dir": str(stream_dir)} for update in evaluation.get("environment_updates", [])],
            },
            "user": {"llm_post_failure": task.get("user_simulator", {}), "agent_input": task.get("agent_input", {})},
            "verifier": {"type": "l1_arena", "evaluation": evaluation, "script_root": str(stream_dir / "evaluation" / "scripts")},
            "metadata": {"public": {
                "source_format": "l1_arena_task_json",
                "available_tools": available,
                # The transport must outlive the agent's advertised episode
                # budget so it can return its bounded result and usage.
                "interaction_timeout_seconds": 660,
                "agent_limits": {
                    "max_model_calls": 12,
                    "max_tool_calls": 16,
                    "terminal_timeout_seconds": 120,
                    "episode_timeout_seconds": 600,
                    "evolution_max_model_calls": 2,
                    "evolution_timeout_seconds": 180,
                },
            }},
        })
    return BenchmarkSpec.from_dict({
        "schema_version": "0.1",
        "benchmark": {"id": f"l1-arena-{stream}", "max_total_tokens": 10_000_000},
        "agent": {"type": "openai_compatible", "thinking": "disabled", "max_output_tokens": 2400},
        "judge": {"type": "deterministic", "max_output_tokens": 1},
        "flows": [{"id": task["stream_id"], "sandbox": {"backend": backend, "image": image}, "turns": converted}],
    })


def inspect_l1_arena(package: str | Path) -> dict[str, Any]:
    root = Path(package).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "l1_arena/1.5":
        raise ValueError("expected an l1_arena/1.5 package")
    streams = manifest.get("streams", [])
    return {"package": str(root), "schema_version": manifest["schema_version"], "stream_count": len(streams), "total_turn_count": manifest.get("total_turn_count"), "streams": streams}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an L1-Arena stream through Tri-EvoBench.")
    parser.add_argument("--package", required=True)
    parser.add_argument("--stream", default="stream-1")
    parser.add_argument("--turns", default="turn1")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", default="runs/l1-arena")
    parser.add_argument("--backend", choices=["local", "docker"], default="docker")
    parser.add_argument("--image", default="trievo-ubuntu:22.04")
    parser.add_argument("--baseline")
    parser.add_argument("--condition", choices=["reset", "frozen", "evolving"], default=None)
    parser.add_argument("--baseline-config")
    parser.add_argument("--max-total-tokens", type=int, default=3_000_000)
    parser.add_argument("--resume-checkpoint", help="post-turn checkpoint directory; rerun only subsequent turns")
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(inspect_l1_arena(args.package), ensure_ascii=False, indent=2))
        return
    spec = load_l1_arena_stream(args.package, args.stream, [x.strip() for x in args.turns.split(",") if x.strip()], args.backend, args.image)
    baseline_config = load_mapping(args.baseline_config) if args.baseline_config else None
    if args.baseline == "deepseek-harness" and baseline_config:
        output_cap = int(baseline_config.get("options", {}).get("max_output_tokens", 4096))
        for turn in spec.flows[0].turns:
            turn.metadata["public"]["agent_limits"]["max_output_tokens"] = output_cap
    result = Runner(spec, output_dir=Path(args.output), backend=args.backend, image=args.image, agent_mode=None if args.baseline else "openai_compatible", judge_mode="deterministic", baseline=args.baseline, baseline_config=baseline_config, max_total_tokens=args.max_total_tokens, resume_checkpoint=Path(args.resume_checkpoint) if args.resume_checkpoint else None, fail_fast_infrastructure=True, condition=args.condition).run(spec.flows[0])
    print(json.dumps({"flow_id": result["flow_id"], "metrics": result["metrics"], "token_budget": result["token_budget"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
