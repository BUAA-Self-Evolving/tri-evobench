from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .runner import Runner
from .baselines.manifest import load_mapping
from .spec import BenchmarkSpec


def _workspace_files(workspace: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative_parts = path.relative_to(workspace).parts
        if relative_parts[:2] in {("docs", "abstract_polished.txt"), ("docs", "abstract_zh.md")}:
            continue
        if relative_parts and relative_parts[0] == "iterations":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(workspace).as_posix()
        files.append({"path": f"/workspace/{relative}", "content": content})
    return files


def _task_to_turn(task_dir: Path, task: dict[str, Any], first: bool, workspace: Path) -> dict[str, Any]:
    evaluation = task.get("evaluate", {})
    eval_type = evaluation.get("type")
    threshold = float(task.get("iteration_policy", {}).get("iter_score_0_threshold", 0.8))
    verifier: dict[str, Any]
    if eval_type == "rule_verifier":
        verifier = {
            "type": "python_verifier",
            "path": str((task_dir / "verify.py").resolve()),
            "pass_threshold": threshold,
        }
    elif eval_type == "llm_as_judge":
        judge_config = evaluation.get("judge_config", {})
        rubric = str(judge_config.get("judge_prompt", "Judge the response against the user request."))
        rubric = rubric.replace("{user_message}", str(task.get("user_message", "")))
        rubric = rubric.replace("{gt}", json.dumps(evaluation.get("gt", {}), ensure_ascii=False, indent=2))
        context_files = []
        for item in task.get("workspace_files", []):
            relative = str(item).removeprefix("agent_workspace/").replace("\\", "/")
            context_files.append(f"/workspace/{relative}")
        verifier = {
            "type": "llm_rubric",
            "rubric": rubric,
            "context_files": context_files,
            "pass_threshold": threshold,
        }
        # The L1 prompts name source/artifact text directly.  Keep that convention
        # while resolving it through the sandbox, so an evaluator never reads the
        # developer's host workspace.
        placeholder_files = {
            "original_text": "/workspace/docs/abstract.txt",
            "polished_text": "/workspace/docs/abstract_polished.txt",
            "source_text": "/workspace/docs/abstract_zh.md" if str(task.get("turn_id")) == "turn7" else "/workspace/docs/abstract_polished.txt",
            "translated_text": "/workspace/docs/abstract_zh.md",
        }
        used = {name: path for name, path in placeholder_files.items() if "{" + name + "}" in rubric}
        if used:
            verifier["file_placeholders"] = used
    else:
        raise ValueError(f"unsupported L1 evaluate.type: {eval_type}")

    clarification = task.get("interactive_clarification", {}).get("scripted_user_reply", {})
    followups = []
    if clarification.get("reply"):
        followups.append(str(clarification["reply"]))
    if str(task.get("turn_id")) == "turn1":
        # This detail is stated by the demo's interaction contract/GT but omitted
        # from task.json's optional clarification block.
        followups.append("我平时用的交付日历 ID 是 cal_delivery_team。")
    followups.append("请根据上一轮反馈重新完成任务，并直接给出最终结果。")
    available_tools = [str(item) for item in task.get("available_tools", [])]
    tool_note = "\n\n【可调用的模拟工具】\n" + ("、".join(available_tools) if available_tools else "无；请只给出回复，不要虚构工具调用。")
    output_note = ""
    if str(task.get("turn_id")) == "turn4":
        output_note = (
            "\n\n【强制交互流程】这是一个两步任务：首轮只能询问“质量最差”的判定口径，"
            "不得给出任何数据结论；收到口径后才给最终分析。"
            "\n【最终输出约束】最终回复全文至多 5 个中文句子；每个 sprint 的两个数必须写在同一小段中，"
            "格式如“S24：bugs=35，P0=3”。不要使用小数点形式的比例（可只描述趋势或改用百分数）。"
        )
    elif str(task.get("turn_id")) == "turn1":
        output_note = "\n\n【日历事件规范】确认日历 ID 后，创建事件的 title 必须为“Aurora 周例会”。"
    elif str(task.get("turn_id")) == "turn2":
        output_note = (
            "\n\n【群解析规范】本任务中“群”指“Aurora 交付群”；调用 message.resolve_group 时 group_name 必须为“Aurora 交付群”。"
            "工具返回有效 ID 后，必须在回复中写出该 ID，并保存到 aurora.delivery_group_id。"
        )
    elif str(task.get("turn_id")) == "turn3":
        output_note = (
            "\n\n【纪要规范】必须用 file.create_file 和同路径 write_file 创建 `/workspace/docs/aurora_20260910_launch_readiness.md`；"
            "面向用户的确认段必须为英文，西班牙语仅限一次性纪要复述段。"
        )
    elif str(task.get("turn_id")) == "turn7":
        output_note = (
            "\n\n【最终输出自检】回复应为 150–300 个中文字符，明确写出三个要点（“解决什么问题 / 怎么解决 / 效果与局限”），"
            "覆盖发布前降风险、优于直接遮盖或改写、仍有残余风险、仅限已测试场景；最后必须逐字以“是否立项投入，建议等跨模型验证报告出来再定”结束。"
        )
    return {
        "id": str(task["turn_id"]),
        "request": str(task["user_message"]) + tool_note + output_note,
        "max_interactions": int(task.get("max_iterations", 3)),
        "success_threshold": threshold,
        "setup": {"files": _workspace_files(workspace)} if first else {},
        "user": {"followups": followups},
        "verifier": verifier,
        "metadata": {"public": {
            "source_format": "l1_demo_task_json",
            "task_type": task.get("task_type"),
            "available_tools": available_tools,
            "iteration_policy": task.get("iteration_policy", {}),
            "interaction_timeout_seconds": 180,
            "agent_limits": {
            "max_model_calls": 3,
            "max_tool_calls": max(1, len(available_tools)),
            "terminal_timeout_seconds": 90,
            "episode_timeout_seconds": 180,
            "evolution_max_model_calls": 1,
            "evolution_timeout_seconds": 120,
            },
        }},
    }


def load_l1_session(session_dir: str | Path, turns: list[str] | None = None, backend: str = "docker", image: str = "trievo-ubuntu:22.04") -> BenchmarkSpec:
    session = Path(session_dir).resolve()
    workspace = session / "agent_workspace"
    selected = turns or [f"turn{i}" for i in range(1, 8)]
    converted: list[dict[str, Any]] = []
    for index, turn_id in enumerate(selected):
        task_dir = session / turn_id
        task_path = task_dir / "task.json"
        if not task_path.exists():
            raise FileNotFoundError(task_path)
        task = json.loads(task_path.read_text(encoding="utf-8"))
        converted.append(_task_to_turn(task_dir, task, index == 0, workspace))
    raw = {
        "schema_version": "0.1",
        "benchmark": {"id": f"l1-demo-{session.name}", "max_total_tokens": 3_000_000},
        "agent": {
            "type": "openai_compatible",
            "response_format": "l1_json",
            "include_workspace_content": True,
            "max_output_tokens": 1800,
            "timeout": 45,
            "thinking": "disabled",
            "system_prompt": """你是 L1 benchmark 的项目助理 Agent。依据当前用户请求、会话历史、持久状态和工作空间完成任务。当前为 2026 年、时区 +08:00。\n\n你必须只返回一个 JSON 对象，绝不输出 Markdown 或解释文字：\n{\"response\": \"给用户看的最终回复\", \"actions\": [{\"op\": \"write_file\", \"path\": \"/workspace/...\", \"content\": \"完整文件内容\"}], \"tool_calls\": [{\"tool\": \"calendar.create_event\", \"arguments\": {...}}], \"events\": [{\"event\": \"asked_for_calendar_id\"}], \"state_update\": {\"facts\": {...}, \"fact_history\": [...]}}\n\n规则：\n1. 只有在本轮列出的模拟工具中才可填 tool_calls；不要编造工具结果。calendar.create_event 的 arguments 必须完整含 calendar_id、title、start、end（ISO 8601，如 2026-08-24T10:00:00+08:00）。message.resolve_group 必须含 group_name。file.create_file 必须含 filename，且同时用同路径 actions.write_file 写入完整内容。\n2. 对用户要求先澄清的歧义，第一轮只做简短澄清，并在 events 写入含 asked 的事件名；收到用户回复后再执行依赖该回复的操作。\n3. state_update 仅保存明确的长期事实；事实值用 {\"value\": \"...\"}。L1 的标准 key 为 default_output_language（zh-CN/en-US）、aurora.technical_owner、default_delivery_calendar_id、document_filename_pattern、aurora.delivery_group_id、aurora.go_live_date。修正既有事实时保留旧值到 fact_history（数组，含 key/value/status=superseded），并确保 facts 只保留新 active 值。一次性的语言/同步内容不得进入状态。\n4. 回复应严格服从请求的语言、句数和格式。要分析文件时自行根据文件内容计算，不要猜测数字。用户给出句数上限时，视为整个 response 的上限；小数点会被评测器视为句号，改用百分数、分数或文字描述。\n5. 没有副作用时返回空 actions/tool_calls/events；不得声称已经做了未声明的动作。""",
        },
        "judge": {"type": "hybrid", "rubric": "严格依据 task rubric 评分。", "max_output_tokens": 1200, "timeout": 45},
        "flows": [{"id": f"l1-{session.name}", "sandbox": {"backend": backend, "image": image}, "turns": converted}],
    }
    return BenchmarkSpec.from_dict(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an L1 demo session using the Tri-EvoBench runtime.")
    parser.add_argument("--session", required=True)
    parser.add_argument("--turns", default="turn4", help="comma-separated turn ids; default turn4")
    parser.add_argument("--output", default="runs/l1-demo")
    parser.add_argument("--backend", choices=["local", "docker"], default="docker")
    parser.add_argument("--image", default="trievo-ubuntu:22.04")
    parser.add_argument("--learner", choices=["frozen", "raw-replay", "evolving", "oracle-update"], default="evolving")
    parser.add_argument("--baseline", help="run the L1 session through a registered real-agent baseline")
    parser.add_argument("--baseline-config", help="JSON/YAML manifest overrides for --baseline")
    parser.add_argument("--max-total-tokens", type=int, default=3_000_000)
    args = parser.parse_args()
    selected = [item.strip() for item in args.turns.split(",") if item.strip()]
    spec = load_l1_session(args.session, selected, args.backend, args.image)
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
