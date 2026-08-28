"""L1-Life v2 adapter with a strict agent/evaluator visibility boundary."""

from __future__ import annotations

import argparse
import json
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
        except UnicodeDecodeError:
            continue
        files.append({"path": "/workspace/" + path.relative_to(workspace).as_posix(), "content": content})
    return files


def _messages_to_request(messages: list[dict[str, Any]], tool_schemas: dict[str, Any]) -> str:
    if not messages:
        raise ValueError("L1-Life agent_input.messages must not be empty")
    transcript = "\n\n".join(
        f"{str(item.get('role', 'user')).upper()}:\n{str(item.get('content', ''))}" for item in messages
    )
    if not tool_schemas:
        return transcript
    return "Available mock-tool schemas for this turn:\n" + json.dumps(tool_schemas, ensure_ascii=False) + "\n\n" + transcript


def _life_verifier(task: dict[str, Any]) -> dict[str, Any]:
    evaluation = task.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("protocol") != "l1_life_llm_judge_v1":
        raise ValueError(f"{task.get('turn_id', '<unknown>')} is not an L1-Life judge task")
    config = evaluation.get("judge_config", {})
    if not isinstance(config, dict):
        raise ValueError("L1-Life judge_config must be an object")
    # These fields stay in TurnSpec.verifier and are never handed to a runtime
    # through its public TurnContext metadata.
    return {
        "type": "l1_life_judge",
        "stream_id": task["stream_id"],
        "turn_id": task["turn_id"],
        "turn_type": task["turn_type"],
        "turn_origin": task["turn_origin"],
        "agent_input": task["agent_input"],
        "judge_prompt": config.get("judge_prompt", ""),
        # GLM-5.3-Flash rejects oversized max_tokens on the compatible API;
        # judge output is a compact JSON object, so keep a bounded ceiling.
        "max_output_tokens": min(int(config.get("max_output_tokens", 1024)), 1024),
        "checks": evaluation.get("checks", []),
        "rubrics": evaluation.get("rubrics", {}),
        "expected_state": evaluation.get("expected_state", {}),
        "expected_tool_call": evaluation.get("expected_tool_call"),
        "expected_workspace_effect": evaluation.get("expected_workspace_effect"),
        "reference_context": evaluation.get("reference_context"),
        "gt": evaluation.get("gt", {}),
        "pass_threshold": 1.0,
    }


def load_l1_life_stream(
    package: str | Path,
    stream: str = "stream-1",
    turns: list[str] | None = None,
    backend: str = "docker",
    image: str = "trievo-ubuntu:22.04",
) -> BenchmarkSpec:
    root = Path(package).resolve()
    manifest_path = root / "manifest.json"
    tools_path = root / "tools.json"
    stream_dir = root / stream
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tools_document = json.loads(tools_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "l1_life/2.0":
        raise ValueError("expected an l1_life/2.0 package")
    if not stream_dir.is_dir():
        raise FileNotFoundError(stream_dir)
    workspace = stream_dir / "agent_workspace"
    if not workspace.is_dir():
        raise FileNotFoundError(workspace)
    selected = turns or [f"turn{i}" for i in range(1, 61)]
    tool_catalog = tools_document.get("tools", {})
    converted: list[dict[str, Any]] = []
    for index, turn_id in enumerate(selected):
        task_path = stream_dir / turn_id / "task.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        agent_input = task.get("agent_input", {})
        available_tools = [str(name) for name in agent_input.get("available_tools", [])]
        missing_tools = [name for name in available_tools if name not in tool_catalog]
        if missing_tools:
            raise ValueError(f"{task_path}: unavailable tools not in tools.json: {', '.join(missing_tools)}")
        visible_schemas = {name: tool_catalog[name] for name in available_tools}
        simulator = task.get("user_simulator")
        converted.append({
            "id": turn_id,
            "request": _messages_to_request(list(agent_input.get("messages", [])), visible_schemas),
            "max_interactions": 2 if isinstance(simulator, dict) else 1,
            "success_threshold": 1.0,
            "setup": {"files": _workspace_files(workspace)} if index == 0 else {},
            "user": {"simulator": simulator} if isinstance(simulator, dict) else {},
            "verifier": _life_verifier(task),
            "metadata": {"public": {
                "source_format": "l1_life_v2_task_json",
                "available_tools": available_tools,
                "interaction_timeout_seconds": 180,
                "agent_limits": {
                    "max_model_calls": 2,
                    "max_tool_calls": max(1, len(available_tools)),
                    "terminal_timeout_seconds": 90,
                    "episode_timeout_seconds": 180,
                    "evolution_max_model_calls": 1,
                    "evolution_timeout_seconds": 120,
                },
            }},
        })
    raw = {
        "schema_version": "0.1",
        "benchmark": {"id": f"l1-life-v2-{stream}", "max_total_tokens": 50_000_000},
        "agent": {
            "type": "openai_compatible",
            "response_format": "l1_json",
            "include_workspace_content": False,
            "max_output_tokens": 2400,
            "timeout": 180,
            # GLM-5.3-Flash is a mandatory-thinking model; its API rejects
            # thinking=disabled (HTTP 1210). Use the lowest supported level.
            "thinking": "low",
            "system_prompt": """You are the task agent for an L1-Life benchmark. The user message contains the complete current conversation; answer its latest request faithfully. Return one JSON object only with response (string), actions (array), tool_calls (array of {tool, arguments}), events (array), and state_update (object). Call only tools whose schemas appear in the user message. For a fact explicitly stated to matter in future turns, save only a semantic facts entry such as {\"facts\": {\"default_workspace\": {\"value\": \"docs/project/drafts\"}}}; never save a one-turn instruction. Do not claim a tool effect without including the corresponding tool_calls entry. You never receive evaluator rubrics, hidden facts, or expected values.""",
        },
        "judge": {"type": "hybrid", "max_output_tokens": 1024, "timeout": 180},
        "flows": [{"id": task["stream_id"], "sandbox": {"backend": backend, "image": image}, "turns": converted}],
    }
    return BenchmarkSpec.from_dict(raw)


def inspect_l1_life(package: str | Path) -> dict[str, Any]:
    root = Path(package).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "l1_life/2.0":
        raise ValueError("expected an l1_life/2.0 package")
    streams = manifest.get("streams", [])
    return {
        "package": str(root),
        "schema_version": manifest["schema_version"],
        "benchmark": manifest.get("benchmark"),
        "stream_count": len(streams),
        "turns_per_stream": manifest.get("turns_per_stream"),
        "total_turn_count": manifest.get("total_turn_count"),
        "streams": [{"directory": item.get("directory"), "stream_id": item.get("stream_id"), "turn_count": item.get("turn_count")} for item in streams],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated L1-Life v2 stream through Tri-EvoBench.")
    parser.add_argument("--package", required=True)
    parser.add_argument("--stream", default="stream-1")
    parser.add_argument("--turns", default="turn1", help="comma-separated turn ids; default turn1")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", default="runs/l1-life")
    parser.add_argument("--backend", choices=["local", "docker"], default="docker")
    parser.add_argument("--image", default="trievo-ubuntu:22.04")
    parser.add_argument("--learner", choices=["frozen", "raw-replay", "evolving", "oracle-update"], default="evolving")
    parser.add_argument("--baseline", help="run the stream through a registered real-agent baseline")
    parser.add_argument("--baseline-config", help="JSON/YAML manifest overrides for --baseline")
    parser.add_argument("--max-total-tokens", type=int, default=3_000_000)
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(inspect_l1_life(args.package), ensure_ascii=False, indent=2))
        return
    selected = [item.strip() for item in args.turns.split(",") if item.strip()]
    spec = load_l1_life_stream(args.package, args.stream, selected, args.backend, args.image)
    baseline_config = load_mapping(args.baseline_config) if args.baseline_config else None
    result = Runner(
        spec,
        output_dir=Path(args.output),
        backend=args.backend,
        image=args.image,
        agent_mode=None if args.baseline else "openai_compatible",
        judge_mode="hybrid",
        learner_mode=args.learner,
        max_total_tokens=args.max_total_tokens,
        baseline=args.baseline,
        baseline_config=baseline_config,
    ).run(spec.flows[0])
    print(json.dumps({"flow_id": result["flow_id"], "metrics": result["metrics"], "token_budget": result["token_budget"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
