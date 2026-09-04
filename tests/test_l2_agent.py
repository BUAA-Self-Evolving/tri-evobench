from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trievo_bench.baselines.base import BaselineReply
from trievo_bench.baselines.hermes_bridge import _parse_l2_agent_limits
from trievo_bench.l2_agent import L2AgentRunError, L2AgentStreamRunner, _agent_request, _limit_reasons, _work_limits


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
            self.assertIn("-i", run.call_args.args[0])
            self.assertIn("l2_native_verifier_request/1.0", run.call_args.kwargs["input"])
            self.assertEqual(result["category"], "fail")


if __name__ == "__main__":
    unittest.main()
