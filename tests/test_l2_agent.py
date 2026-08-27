from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trievo_bench.baselines.hermes_bridge import _parse_l2_agent_limits
from trievo_bench.l2_agent import L2AgentRunError, _agent_request, _limit_reasons, _work_limits


class L2AgentLimitsTest(unittest.TestCase):
    def test_default_limits_are_bounded_and_visible_to_the_agent(self) -> None:
        limits = _work_limits({})
        self.assertEqual(limits.max_model_calls, 10)
        self.assertEqual(limits.max_tool_calls, 8)
        self.assertEqual(limits.terminal_timeout_seconds, 90)
        self.assertEqual(limits.episode_timeout_seconds, 480)
        self.assertEqual(limits.evolution_max_model_calls, 4)

        with tempfile.TemporaryDirectory() as tmp:
            episode = Path(tmp)
            prompt = _agent_request(episode, {"request": "write answer.txt", "assets": []}, limits)

        self.assertIn("at most 10 model calls", prompt)
        self.assertIn("8 tool calls", prompt)
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
            "evolution_max_model_calls": 4,
            "evolution_timeout_seconds": 120,
        }})
        self.assertEqual(parsed["max_tool_calls"], 8)
        self.assertEqual(_limit_reasons({"limit_exhausted": True, "limit_reasons": ["max_tool_calls"]}), ["max_tool_calls"])
        self.assertEqual(_limit_reasons({"limit_exhausted": False}), [])


if __name__ == "__main__":
    unittest.main()
