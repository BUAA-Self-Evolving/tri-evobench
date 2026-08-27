from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from baseline.adapters.trace2skill.bridge import (
    Trace2SkillBridge,
    _normalize_generation_config,
    _select_agent_name,
    _steps_to_trace,
    _trajectory_records,
)
from baseline.Trace2Skill.src.react_agent.converter import ReActConverter
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class Trace2SkillBaselineContractTest(unittest.TestCase):
    def test_manifest_is_registered_with_flow_scoped_skill_state(self) -> None:
        manifest = load_manifest(Path(__file__).parents[1] / "baseline" / "manifests" / "trace2skill.json")
        self.assertEqual(manifest.baseline_id, "trace2skill")
        self.assertEqual(manifest.adapter["type"], "process")
        self.assertIn("skills/**/*", manifest.state["paths"])
        self.assertIn("history/**/*", manifest.state["paths"])
        self.assertTrue(manifest.state["enforce_turn_freeze"])
        self.assertEqual(manifest.evolution["policy"], "turn_boundary")

    def test_baseline_type_selects_trace2skill_without_runner_changes(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {"type": "trace2skill", "options": {"max_turns": 7}},
            "flows": [{
                "id": "flow",
                "turns": [{
                    "id": "turn",
                    "request": "edit",
                    "verifier": {"type": "always", "passed": True},
                }],
            }],
        })
        baseline_id, manifest, overrides = resolve_baseline_selection(
            spec,
            baseline_override=None,
            agent_override=None,
            config_override=None,
            manifest_override=None,
        )
        self.assertEqual(baseline_id, "trace2skill")
        self.assertIsNone(manifest)
        self.assertEqual(overrides["options"]["max_turns"], 7)

    def test_public_trajectory_conversion_excludes_private_fields(self) -> None:
        rows = _trajectory_records(
            [{
                "index": 1,
                "request": "q",
                "response": "a",
                "score": 0.0,
                "passed": False,
                "feedback": "wrong",
                "verifier_details": {"secret": "hidden"},
            }],
            [{"interaction_index": 1, "score": 0.0, "passed": False, "feedback": "wrong"}],
        )
        encoded = json.dumps(rows, ensure_ascii=False)
        self.assertEqual(rows[0]["record_source"], "error")
        self.assertNotIn("verifier_details", encoded)
        self.assertNotIn("secret", encoded)

    def test_bridge_seeds_skill_and_respects_disabled_evolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "references").mkdir(parents=True)
            (source / "SKILL.md").write_text("# seed\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state"
            seen: dict[str, str] = {}

            class FakeAgent:
                _agent = SimpleNamespace(_last_steps=[])

                def run(self, context):
                    seen["working_dir"] = context.working_dir
                    seen["input_file"] = context.input_file
                    return {"success": True, "answer": "done", "turns": 1}

            bridge = Trace2SkillBridge(
                client_factory=lambda options, stage: SimpleNamespace(model="fake"),
                agent_factory=lambda client, options, working_dir, skills_dir: FakeAgent(),
            )
            bridge.initialize({
                "protocol_version": "1.0",
                "options": {"skill": str(source), "skill_name": "xlsx"},
                "evolution": {"policy": "disabled"},
            })
            bridge.start_flow({
                "flow_id": "flow",
                "workspace": str(workspace),
                "state_dir": str(state),
            })
            bridge.start_turn({"turn_id": "turn", "metadata": {}})
            response = bridge.interact({"turn_id": "turn", "request": "edit"})
            evolution = bridge.evolve({
                "turn_id": "turn",
                "trajectory": [{
                    "index": 1,
                    "request": "edit",
                    "response": "done",
                    "verifier_details": {"secret": "hidden"},
                }],
                "feedback": [{"interaction_index": 1, "feedback": "ok", "oracle_update": "hidden"}],
            })
            self.assertEqual(response["text"], "done")
            self.assertTrue(response["tool_calls_are_executed"])
            self.assertEqual(evolution["events"][0]["type"], "evolution.skipped")
            self.assertEqual((state / "skills" / "xlsx" / "SKILL.md").read_text(), "# seed\n")
            history = (state / "history" / "turn" / "trajectory.json").read_text(encoding="utf-8")
            self.assertNotIn("verifier_details", history)
            self.assertNotIn("secret", history)
            self.assertEqual(Path(seen["working_dir"]), workspace.resolve())

    def test_steps_are_serialized_as_auditable_tool_trace(self) -> None:
        step = SimpleNamespace(
            turn=1,
            thought="inspect",
            action=SimpleNamespace(name="bash", arguments={"command": "ls"}),
            observation="input.xlsx",
            is_final=False,
            is_format_error=False,
        )
        trace = _steps_to_trace([step])
        self.assertEqual(trace["tool_calls"][0]["name"], "bash")
        self.assertEqual(trace["events"][0]["type"], "tool.completed")

    def test_auto_agent_uses_cli_only_for_generic_tasks(self) -> None:
        self.assertEqual(_select_agent_name({"agent": "auto"}, {"instruction": "Create /workspace/a.txt"}), "cli_workspace")
        self.assertEqual(_select_agent_name({"agent": "auto"}, {"input_file": "input.xlsx"}), "cli_skill_preloaded")

    def test_deepseek_thinking_is_normalized_for_openai_sdk(self) -> None:
        config = _normalize_generation_config({"max_tokens": 128, "thinking": {"type": "disabled"}})
        self.assertNotIn("thinking", config)
        self.assertEqual(config["extra_body"]["thinking"]["type"], "disabled")

    def test_react_converter_parses_deepseek_dsml_tool_call(self) -> None:
        response = (
            'prefix\n'
            '<\\ufffd\\ufffd\\ufffd\\ufffdDSML\\ufffd\\ufffd\\ufffd\\ufffd1"python">\n'
            '<argument1>{"code": "print(1)"}</argument1>\n'
            '</argument1>\n'
            '</\\ufffd\\ufffd\\ufffd\\ufffdDSML\\ufffd\\ufffd\\ufffd\\ufffd1>'
        )
        result = ReActConverter().parse_response(response)
        self.assertTrue(result.is_action)
        self.assertEqual(result.action.name, "python")
        self.assertEqual(result.action.arguments, {"code": "print(1)"})

    def test_react_converter_parses_dsml_eval_execute_and_fenced_calls(self) -> None:
        eval_response = (
            '<｜｜DSML｜｜eval_execute>\n'
            '<｜｜DSML｜｜parameter name="code">print(2)</｜｜DSML｜｜parameter>\n'
            '</｜｜DSML｜｜eval_execute>'
        )
        result = ReActConverter().parse_response(eval_response)
        self.assertTrue(result.is_action)
        self.assertEqual(result.action.name, "python")
        self.assertEqual(result.action.arguments, {"code": "print(2)"})

        fenced = ReActConverter().parse_response("```bash\necho ok\n```")
        self.assertTrue(fenced.is_action)
        self.assertEqual(fenced.action.name, "bash")
        self.assertEqual(fenced.action.arguments, {"command": "echo ok"})

    def test_react_converter_parses_dsml_json_action_with_reasoning_prefix(self) -> None:
        response = (
            "I will inspect the workspace first.\n"
            "<｜｜DSML｜｜eval_action>\n"
            '{"name": "bash", "arguments": {"command": "ls -la /workspace"}}\n'
            "</｜｜DSML｜｜eval_action>"
        )
        result = ReActConverter().parse_response(response)
        self.assertTrue(result.is_action)
        self.assertEqual(result.action.name, "bash")
        self.assertEqual(result.action.arguments, {"command": "ls -la /workspace"})


if __name__ == "__main__":
    unittest.main()
