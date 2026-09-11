from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trievo_bench.baselines.base import BaselineReply
from trievo_bench.baselines.hermes_bridge import _parse_l2_agent_limits
from trievo_bench.l2_agent import (
    L2AgentRunError,
    L2AgentStreamRunner,
    _agent_request,
    _apply_stream_limit_overrides,
    _effective_work_limits,
    _limit_reasons,
    _public_submission_contract,
    _public_submission_instruction,
    _public_taskstream_tools,
    _stream_submission_carriers,
    _work_limits,
)


class L2AgentLimitsTest(unittest.TestCase):
    def test_default_limits_are_bounded_and_visible_to_the_agent(self) -> None:
        limits = _work_limits({})
        self.assertEqual(limits.max_model_calls, 10)
        self.assertEqual(limits.max_tool_calls, 8)
        self.assertEqual(limits.terminal_timeout_seconds, 90)
        self.assertEqual(limits.episode_timeout_seconds, 480)
        self.assertEqual(limits.max_output_tokens, 12000)
        self.assertEqual(limits.evolution_max_model_calls, 4)

        with tempfile.TemporaryDirectory() as tmp:
            episode = Path(tmp)
            prompt = _agent_request(episode, {"request": "write answer.txt", "assets": []}, limits)

        self.assertIn("at most 10 model calls", prompt)
        self.assertIn("8 tool calls", prompt)
        self.assertIn("12000 output tokens", prompt)
        self.assertIn("90 seconds", prompt)
        self.assertIn("480 seconds total", prompt)
        self.assertIn("records it as a failure", prompt)

    def test_limits_can_be_overridden_but_invalid_values_are_rejected(self) -> None:
        limits = _work_limits({"options": {"l2_agent_limits": {"max_model_calls": 12}}})
        self.assertEqual(limits.max_model_calls, 12)

        with self.assertRaises(L2AgentRunError):
            _work_limits({"options": {"l2_agent_limits": {"max_tool_calls": 0}}})
        with self.assertRaises(L2AgentRunError):
            _work_limits({"options": {"l2_agent_limits": {"episode_timeout_seconds": 60, "terminal_timeout_seconds": 90}}})

    def test_bridge_limit_metadata_and_reply_marker(self) -> None:
        parsed = _parse_l2_agent_limits({"l2_agent_limits": {
            "max_model_calls": 10,
            "max_tool_calls": 8,
            "terminal_timeout_seconds": 90,
            "episode_timeout_seconds": 480,
            "max_output_tokens": 12000,
            "evolution_max_model_calls": 4,
            "evolution_timeout_seconds": 120,
        }})
        self.assertEqual(parsed["max_tool_calls"], 8)
        self.assertEqual(parsed["max_output_tokens"], 12000)
        self.assertEqual(_parse_l2_agent_limits({"agent_limits": {
            "max_model_calls": 3,
            "max_tool_calls": 1,
            "terminal_timeout_seconds": 90,
            "episode_timeout_seconds": 180,
            "evolution_max_model_calls": 2,
            "evolution_timeout_seconds": 120,
        }})["evolution_max_model_calls"], 2)
        self.assertEqual(_limit_reasons({"limit_exhausted": True, "limit_reasons": ["max_tool_calls"]}), ["max_tool_calls"])
        self.assertEqual(_limit_reasons({"limit_exhausted": False}), [])

    def test_stream_carrier_qualification_override_cannot_enter_formal_budget(self) -> None:
        configured = _effective_work_limits(
            _work_limits({"options": {"l2_agent_limits": {"max_tool_calls": 20}}}),
            {
                "max_tool_calls_per_interaction": 20,
                "turn_timeout_seconds": 900,
                "max_output_tokens": 12000,
            },
        )
        overrides = {
            "options": {
                "l2_stream_limit_overrides": [{
                    "stream_id": "skillsbench_data_to_d3_001",
                    "baseline": "hermes",
                    "carrier": "workspace_artifact",
                    "limits": {"max_tool_calls": 40},
                }],
            },
        }

        with self.assertRaisesRegex(L2AgentRunError, "formal stream limit override"):
            _apply_stream_limit_overrides(
                configured=configured,
                overrides=overrides,
                baseline="hermes",
                stream_id="skillsbench_data_to_d3_001",
                carriers=("workspace_artifact",),
                execution_requirements={
                    "max_tool_calls_per_interaction": 20,
                    "turn_timeout_seconds": 900,
                    "max_output_tokens": 12000,
                },
                budget_profile="formal",
                applied=[],
            )

        applied: list[dict[str, object]] = []
        qualified = _apply_stream_limit_overrides(
            configured=configured,
            overrides={**overrides, "options": {**overrides["options"], "budget_profile": "qualification"}},
            baseline="hermes",
            stream_id="skillsbench_data_to_d3_001",
            carriers=("workspace_artifact",),
            execution_requirements={
                "max_tool_calls_per_interaction": 20,
                "turn_timeout_seconds": 900,
                "max_output_tokens": 12000,
            },
            budget_profile="qualification",
            applied=applied,
        )
        self.assertEqual(qualified.max_tool_calls, 40)
        self.assertEqual(applied[0]["budget_profile"], "qualification")

    def test_stream_submission_carriers_read_workspace_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turn = root / "turn1"
            turn.mkdir()
            (turn / "task.json").write_text(
                '{"evaluation":{"submission_contract":{"mode":"workspace_artifact"}}}',
                encoding="utf-8",
            )

            self.assertEqual(
                _stream_submission_carriers(root, ["turn1"]),
                ("workspace_artifact",),
            )

    def test_public_submission_contract_exports_workspace_required_path(self) -> None:
        task = {
            "evaluation": {
                "submission_contract": {"mode": "workspace_artifact", "source_binding": "submission"},
                "native_verifier": {
                    "input_bindings": [
                        {
                            "name": "submission",
                            "source": "workspace",
                            "path": "agent_workspace/episodes/E001/submission.csv",
                        },
                    ],
                },
            },
        }
        self.assertEqual(
            _public_submission_contract(task),
            {
                "mode": "workspace_artifact",
                "source_binding": "submission",
                "required_paths": ["/workspace/episodes/E001/submission.csv"],
            },
        )
        task["evaluation"]["native_verifier"]["input_bindings"][0]["path"] = "agent_workspace/output"
        self.assertEqual(
            _public_submission_contract(task)["required_paths"],
            ["/workspace/output"],
        )

    def test_public_submission_instruction_freezes_final_response_carriers(self) -> None:
        task = {
            "evaluation": {
                "submission_contract": {
                    "mode": "final_response",
                    "payload_kind": "python_source",
                    "source_binding": "submission",
                },
                "native_verifier": {"input_bindings": []},
            },
        }
        source = _public_submission_instruction(task)
        self.assertIn("only the complete Python source code", source)
        self.assertIn("Do not return a summary or file link", source)

        task["evaluation"]["submission_contract"]["payload_kind"] = "json_tool_trajectory"
        trajectory = _public_submission_instruction(task)
        self.assertIn("one strict JSON object", trajectory)
        self.assertIn("Thought/Action", trajectory)
        self.assertIn("exactly requestor, name, and arguments", trajectory)
        self.assertIn("Do not emit user_tool_calls", trajectory)

    def test_taskstream_tools_expand_public_schema_from_catalog(self) -> None:
        task = {"agent_input": {"available_tools": ["enable_roaming", "toggle_roaming"]}}
        tools = _public_taskstream_tools(task, {
            "enable_roaming": {
                "description": "Permit roaming on the carrier line.",
                "requestor": "assistant",
                "arguments": {"customer_id": "C1001", "line_id": "L1002"},
            },
            "toggle_roaming": {
                "description": "Toggle phone roaming.",
                "requestor": "user",
                "arguments": {},
            },
        })
        self.assertEqual(tools[0]["requestor"], "assistant")
        self.assertEqual(tools[0]["arguments"]["line_id"], "L1002")
        self.assertEqual(tools[1]["arguments"], {})

    def test_taskstream_verifier_keeps_stdin_open_for_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turn_dir = root / "streams" / "s" / "turn1"
            (turn_dir / "evaluation_assets").mkdir(parents=True)
            (turn_dir / "evaluation_assets" / "verifier.py").write_text("", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            runner = object.__new__(L2AgentStreamRunner)
            runner.options = SimpleNamespace(package_dir=root, verifier_image="verifier:test")
            task = {
                "evaluation": {
                    "submission_contract": {"source_binding": "submission"},
                    "native_verifier": {
                        "verifier_id": "test-verifier",
                        "entrypoint": "evaluation_assets/verifier.py",
                        "input_bindings": [{"name": "submission", "source": "workspace", "path": "answer.txt"}],
                    },
                },
            }
            completed = SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"schema_version":"l2_native_verifier_result/1.0",'
                    '"verifier_id":"test-verifier","status":"fail","score":0,'
                    '"checks":[],"artifacts":[],"public_summary":'
                    '{"result_category":"fail","observable_deficiencies":["missing"]},'
                    '"private_diagnostics":{},"error":null}\n'
                ),
                stderr="",
            )
            with patch("trievo_bench.l2_agent.subprocess.run", return_value=completed) as run:
                result = runner._verify_taskstream(task=task, turn_dir=turn_dir, workspace=workspace, reply=BaselineReply())
            verifier_call = run.call_args_list[0]
            self.assertIn("-i", verifier_call.args[0])
            self.assertIn("--name", verifier_call.args[0])
            self.assertIn("l2_native_verifier_request/1.0", verifier_call.kwargs["input"])
            self.assertEqual(run.call_args_list[-1].args[0][1:3], ["rm", "-f"])
            self.assertEqual(result["category"], "fail")

    def test_taskstream_verifier_recovers_rootless_docker_output_from_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turn_dir = root / "streams/s/turn1"
            (turn_dir / "evaluation_assets").mkdir(parents=True)
            (turn_dir / "evaluation_assets/verifier.py").write_text("")
            workspace = root / "workspace"
            workspace.mkdir()
            runner = object.__new__(L2AgentStreamRunner)
            runner.options = SimpleNamespace(package_dir=root, verifier_image="verifier:test")
            task = {
                "evaluation": {
                    "submission_contract": {"source_binding": "submission"},
                    "native_verifier": {
                        "verifier_id": "test-verifier",
                        "entrypoint": "evaluation_assets/verifier.py",
                        "input_bindings": [{
                            "name": "submission", "source": "interaction_record", "path": "final_response"
                        }],
                    },
                },
            }
            payload = (
                '{"schema_version":"l2_native_verifier_result/1.0",'
                '"verifier_id":"test-verifier","status":"fail","score":0,'
                '"checks":[],"artifacts":[],"public_summary":'
                '{"result_category":"fail","observable_deficiencies":["invalid"]},'
                '"private_diagnostics":{},"error":null}\n'
            )
            responses = [
                SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=0, stdout=payload, stderr=""),
                SimpleNamespace(returncode=0, stdout="", stderr=""),
            ]
            with patch("trievo_bench.l2_agent.subprocess.run", side_effect=responses) as run:
                result = runner._verify_taskstream(
                    task=task, turn_dir=turn_dir, workspace=workspace,
                    reply=BaselineReply(text="not python"),
                )
            self.assertEqual(run.call_args_list[1].args[0][1], "logs")
            self.assertEqual(run.call_args_list[2].args[0][1:3], ["rm", "-f"])
            self.assertEqual(result["category"], "fail")

    def test_taskstream_verifier_rejects_invalid_path_binding_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turn_dir = root / "streams" / "s" / "turn1"
            (turn_dir / "evaluation_assets").mkdir(parents=True)
            (turn_dir / "evaluation_assets" / "verifier.py").write_text("", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            runner = object.__new__(L2AgentStreamRunner)
            runner.options = SimpleNamespace(package_dir=root, verifier_image="verifier:test")

            invalid_paths = {
                "missing": {},
                "empty": {"path": ""},
                "non-string": {"path": 7},
            }
            for case, path_field in invalid_paths.items():
                binding = {
                    "name": "submission",
                    "source": "workspace",
                    **path_field,
                }
                task = {
                    "evaluation": {
                        "submission_contract": {"source_binding": "submission"},
                        "native_verifier": {
                            "verifier_id": "test-verifier",
                            "entrypoint": "evaluation_assets/verifier.py",
                            "input_bindings": [binding],
                        },
                    },
                }
                with self.subTest(case=case), patch("trievo_bench.l2_agent.subprocess.run") as run:
                    result = runner._verify_taskstream(
                        task=task,
                        turn_dir=turn_dir,
                        workspace=workspace,
                        reply=BaselineReply(),
                    )

                run.assert_not_called()
                self.assertEqual(result["category"], "error")
                self.assertIsNone(result["score"])
                self.assertEqual(result["details"]["type"], "invalid_native_binding")
                self.assertEqual(result["details"]["name"], "submission")
                self.assertEqual(result["details"]["source"], "workspace")
                self.assertEqual(result["details"]["reason"], "path must be a non-empty string")


if __name__ == "__main__":
    unittest.main()
