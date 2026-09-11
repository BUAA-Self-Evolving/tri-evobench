from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from baseline.adapters.skillopt.bridge import (
    SkillOptBridge,
    _execute_dsml_tool,
    _extract_dsml_tool_calls,
    _extract_tool_use_calls,
    _looks_like_l1_protocol_task,
    _looks_like_workspace_task,
    _workspace_contract_paths,
    _trajectory_results,
)
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class SkillOptBaselineContractTest(unittest.TestCase):
    def test_manifest_is_registered_with_flow_scoped_skill_state(self) -> None:
        manifest = load_manifest(Path(__file__).parents[1] / "baseline" / "manifests" / "skillopt.json")
        self.assertEqual(manifest.baseline_id, "skillopt")
        self.assertEqual(manifest.adapter["type"], "process")
        self.assertIn("skill.md", manifest.state["paths"])
        self.assertTrue(manifest.state["enforce_turn_freeze"])
        self.assertEqual(manifest.evolution["policy"], "turn_boundary")

    def test_baseline_type_selects_skillopt_without_runner_changes(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {"type": "skillopt", "options": {"edit_budget": 2}},
            "flows": [{
                "id": "flow",
                "turns": [{
                    "id": "turn",
                    "request": "answer",
                    "verifier": {"type": "file_contains", "path": "/workspace/a", "value": "a"},
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
        self.assertEqual(baseline_id, "skillopt")
        self.assertIsNone(manifest)
        self.assertEqual(overrides["options"]["edit_budget"], 2)

    def test_bridge_validates_protocol_and_maps_public_trajectory(self) -> None:
        bridge = SkillOptBridge()
        result = bridge.initialize({
            "protocol_version": "1.0",
            "baseline_id": "skillopt",
            "options": {},
            "evolution": {"policy": "turn_boundary"},
        })
        self.assertEqual(result["protocol_version"], "1.0")
        rows = _trajectory_results(
            [{"index": 1, "request": "q", "response": "a", "score": 0.5, "passed": False}],
            [{"interaction_index": 1, "feedback": "try again", "score": 0.5, "passed": False}],
        )
        self.assertEqual(rows[0]["hard"], 0)
        self.assertEqual(rows[0]["soft"], 0.5)
        self.assertEqual(rows[0]["feedback"], "try again")

    def test_override_file_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text(
                json.dumps({"options": {"target_model": "test-model"}}),
                encoding="utf-8",
            )
            self.assertEqual(json.loads(path.read_text())["options"]["target_model"], "test-model")

    def test_docker_override_uses_container_paths(self) -> None:
        override = json.loads(
            (Path(__file__).parents[1] / "examples" / "skillopt-docker.override.json").read_text(encoding="utf-8")
        )
        self.assertEqual(override["workspace"]["adapter_path"], "/workspace")
        self.assertEqual(override["state"]["adapter_path"], "/agent-state")

    def test_workspace_actions_are_safe_and_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            bridge = SkillOptBridge()
            bridge.workspace = workspace
            result = bridge._apply_workspace_action({
                "op": "write_file",
                "path": "/workspace/profile.txt",
                "content": "name=Lin\nlanguage=Chinese\n",
            })
            self.assertEqual(result["path"], "/workspace/profile.txt")
            self.assertEqual((workspace / "profile.txt").read_text(), "name=Lin\nlanguage=Chinese\n")
            with self.assertRaises(ValueError):
                bridge._apply_workspace_action({"op": "remove", "path": "/workspace/../secret.txt"})

    def test_workspace_task_detection(self) -> None:
        self.assertTrue(_looks_like_workspace_task("Create /workspace/a.txt"))
        self.assertFalse(_looks_like_workspace_task("Answer the question in one sentence."))
        self.assertFalse(_looks_like_l1_protocol_task(
            "AVAILABLE TOOLS: terminal, filesystem",
            {"source_format": "l2_taskstream_task_json"},
        ))

    def test_l2_filesystem_tool_aliases_execute_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            written = _execute_dsml_tool({
                "name": "filesystem",
                "arguments": {"operation": "write_file", "path": "/workspace/result.txt", "content": "ok"},
            }, workspace, {})
            read = _execute_dsml_tool({
                "name": "filesystem",
                "arguments": {"path": "/workspace/result.txt"},
            }, workspace, {})
            self.assertEqual(written, "[tool completed]")
            self.assertEqual(read, "ok")
            self.assertIn("result.txt", _execute_dsml_tool({
                "name": "filesystem", "arguments": {"path": "/workspace"},
            }, workspace, {}))

    def test_claude_style_text_tool_envelope_is_parsed(self) -> None:
        text = """<tool_uses><tool_use><tool_name>Write</tool_name>
        <parameter name="file_path">/workspace/analysis/result.json</parameter>
        <parameter name="content">{"ok": true}</parameter>
        </tool_use></tool_uses>"""
        self.assertEqual(
            _extract_tool_use_calls(text),
            [{
                "name": "Write",
                "arguments": {
                    "file_path": "/workspace/analysis/result.json",
                    "content": '{"ok": true}',
                },
            }],
        )

    def test_dsml_parser_keeps_all_parameters(self) -> None:
        text = (
            '<｜｜DSML｜｜invoke name="Write">'
            '<｜｜DSML｜｜parameter name="file_path" string="true">/workspace/a.json</｜｜DSML｜｜parameter>'
            '<｜｜DSML｜｜parameter name="content" string="true">{"ok": true}</｜｜DSML｜｜parameter>'
            '</｜｜DSML｜｜invoke>'
        )
        self.assertEqual(
            _extract_dsml_tool_calls(text),
            [{"name": "Write", "arguments": {"file_path": "/workspace/a.json", "content": '{"ok": true}'}}],
        )

    def test_l2_workspace_artifact_contract_names_output_path(self) -> None:
        bridge = SkillOptBridge()
        bridge.config = {"env": "generic"}
        bridge.turn_metadata = {
            "source_format": "l2_taskstream_task_json",
            "submission_contract": {"required_paths": ["/workspace/output"]},
        }
        system, user = bridge._build_prompt(
            "# skill",
            "Create a workspace artifact with the answer.",
            {"history": [], "sandbox": {}},
        )
        self.assertIn("/workspace/output", system)
        self.assertIn("parseable action or tool call", system)
        self.assertIn("Create a workspace artifact", user)
        self.assertEqual(
            _workspace_contract_paths(
                "Create a workspace artifact with the answer.",
                {
                    "source_format": "l2_taskstream_task_json",
                    "submission_contract": {"required_paths": ["/workspace/output"]},
                },
            ),
            ["/workspace/output"],
        )

    def test_csv_submission_contract_does_not_add_output_path(self) -> None:
        metadata = {
            "source_format": "l2_taskstream_task_json",
            "submission_contract": {
                "mode": "workspace_artifact",
                "required_paths": ["/workspace/episodes/E001/submission.csv"],
            },
        }
        paths = _workspace_contract_paths("Create the workspace artifact submission.", metadata)
        self.assertEqual(paths, ["/workspace/episodes/E001/submission.csv"])
        self.assertNotIn("/workspace/output", paths)

    def test_skillsbench_output_contract_adds_output_path(self) -> None:
        self.assertEqual(
            _workspace_contract_paths(
                "SkillsBench data-to-d3 browser artifact must be saved in the workspace output directory.",
                {"source_format": "l2_taskstream_task_json"},
            ),
            ["/workspace/output"],
        )
        self.assertEqual(
            _workspace_contract_paths(
                "Create the artifact.",
                {
                    "source_format": "l2_taskstream_task_json",
                    "submission_contract": {"required_paths": ["agent_workspace/output"]},
                },
            ),
            ["/workspace/output"],
        )

    def test_workspace_artifact_without_parseable_action_fails_closed(self) -> None:
        skillopt_module = ModuleType("skillopt")
        model_module = ModuleType("skillopt.model")
        calls: list[str] = []

        def fake_chat_target(**kwargs):
            calls.append(str(kwargs.get("stage")))
            return "I saved the required artifact in the workspace.", {"api_calls": 1}

        model_module.chat_target = fake_chat_target
        model_module.chat_target_messages = lambda **kwargs: ("", {})
        model_module.is_target_exec_backend = lambda: False
        previous_skillopt = sys.modules.get("skillopt")
        previous_model = sys.modules.get("skillopt.model")
        sys.modules["skillopt"] = skillopt_module
        sys.modules["skillopt.model"] = model_module
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bridge = SkillOptBridge()
                bridge.workspace = root / "workspace"
                bridge.workspace.mkdir()
                bridge.skill_file = root / "skill.md"
                bridge.skill_file.write_text("# skill\n", encoding="utf-8")
                bridge.turn_id = "turn"
                bridge.config = {"env": "generic", "max_completion_tokens": 1024}
                bridge.options = {"retries": 0}
                bridge.turn_metadata = {
                    "source_format": "l2_taskstream_task_json",
                    "submission_contract": {"required_paths": ["/workspace/output"]},
                }
                result = bridge.interact({
                    "turn_id": "turn",
                    "request": "Create a workspace artifact with the answer.",
                    "history": [],
                    "sandbox": {},
                })
        finally:
            if previous_skillopt is None:
                sys.modules.pop("skillopt", None)
            else:
                sys.modules["skillopt"] = previous_skillopt
            if previous_model is None:
                sys.modules.pop("skillopt.model", None)
            else:
                sys.modules["skillopt.model"] = previous_model

        self.assertEqual(calls, ["target", "target_format_retry"])
        self.assertFalse(result["actions_are_executed"])
        self.assertEqual(result["actions"][0]["op"], "parse_error")
        self.assertIn("no parseable action or tool call", result["raw"]["action_error"])
        self.assertEqual(result["events"][-1]["type"], "action.failed")


if __name__ == "__main__":
    unittest.main()
