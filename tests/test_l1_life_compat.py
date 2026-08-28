from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trievo_bench.l1_life_compat import inspect_l1_life, load_l1_life_stream
from trievo_bench.runner import Runner
from trievo_bench.sandbox import LocalSandbox
from trievo_bench.spec import BenchmarkSpec
from trievo_bench.verifier import L1LifeJudge


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "benchmarks" / "l1_life_v2"


class L1LifeCompatTest(unittest.TestCase):
    def test_package_inspection_and_public_projection(self) -> None:
        report = inspect_l1_life(PACKAGE)
        self.assertEqual(report["schema_version"], "l1_life/2.0")
        self.assertEqual(report["stream_count"], 10)
        self.assertEqual(report["total_turn_count"], 600)

        spec = load_l1_life_stream(PACKAGE, "stream-1", ["turn1", "turn3", "turn20"], backend="local")
        turns = spec.flows[0].turns
        self.assertEqual([turn.id for turn in turns], ["turn1", "turn3", "turn20"])
        self.assertEqual(turns[1].max_interactions, 2)
        self.assertEqual(turns[2].metadata["public"]["available_tools"], ["calendar.create_reminder"])
        serialized_public = json.dumps(turns[0].metadata["public"], ensure_ascii=False)
        self.assertNotIn("expected_state", serialized_public)
        self.assertNotIn("rubrics", serialized_public)
        self.assertNotIn("gt", serialized_public)

    def test_life_tools_change_only_declared_workspace_state(self) -> None:
        sandbox = LocalSandbox()
        try:
            sandbox.write_file("/workspace/docs/project/drafts/analysis-journal.md", "# Analysis Journal\n\nBody\n")
            sandbox.write_file("/workspace/tool_state/current_state.json", json.dumps({"calendar_reminders": []}))
            runner = object.__new__(Runner)
            calls = runner._apply_tool_calls([
                {"tool": "file.edit_text", "arguments": {
                    "path": "docs/project/drafts/analysis-journal.md",
                    "edit_instruction": "Replace only the first Markdown heading with the current project label",
                    "replacement": "# Project Review",
                }},
                {"tool": "calendar.create_reminder", "arguments": {
                    "calendar_id": "cal_project", "title": "Review", "date": "2026-11-15",
                }},
            ], sandbox)
            self.assertEqual(sandbox.read_file("/workspace/docs/project/drafts/analysis-journal.md"), "# Project Review\n\nBody\n")
            tool_state = json.loads(sandbox.read_file("/workspace/tool_state/current_state.json"))
            self.assertEqual(tool_state["calendar_reminders"][0]["calendar_id"], "cal_project")
            self.assertEqual(len(calls), 2)
            with self.assertRaises(Exception):
                sandbox.read_file("/workspace/state/tool_calls.json")
        finally:
            sandbox.close()

    def test_l1_life_judge_validates_and_aggregates_components(self) -> None:
        class Client:
            def complete(self, messages, **kwargs):
                return json.dumps({
                    "stream_id": "stream-a",
                    "turn_id": "turn-a",
                    "check_scores": [
                        {"check_id": "answer", "score": 1},
                        {"check_id": "state", "score": 0.5},
                    ],
                    "source_rubric_scores": [{"rubric_index": 1, "score": 1}],
                    "rationale": "observable evidence",
                })

        check = {
            "stream_id": "stream-a", "turn_id": "turn-a", "judge_prompt": "{final_response}",
            "checks": [
                {"check_id": "answer", "dimension": "task_success", "weight": 0.6, "scoring": "mean_source_rubrics"},
                {"check_id": "state", "dimension": "update_precision", "weight": 0.4, "scoring": "ordinal"},
            ],
            "rubrics": {"source_rubrics": ["source rubric"]},
        }
        sandbox = LocalSandbox()
        try:
            result = L1LifeJudge(Client()).evaluate(check, "answer", sandbox, {})
        finally:
            sandbox.close()
        self.assertAlmostEqual(result.score, 0.8)
        self.assertFalse(result.passed)
        self.assertEqual(result.feedback, "Failed dimensions: update_precision.")

    def test_l1_life_judge_retries_one_invalid_ordinal_response(self) -> None:
        class Client:
            calls = 0

            def complete(self, messages, **kwargs):
                self.calls += 1
                score = 0.75 if self.calls == 1 else 1
                return json.dumps({
                    "stream_id": "stream-retry", "turn_id": "turn-retry",
                    "check_scores": [{"check_id": "state", "score": score}],
                    "source_rubric_scores": [], "rationale": "retry test",
                })

        client = Client()
        check = {
            "stream_id": "stream-retry", "turn_id": "turn-retry", "judge_prompt": "{final_response}",
            "checks": [{"check_id": "state", "dimension": "update_precision", "weight": 1, "scoring": "ordinal"}],
            "rubrics": {"source_rubrics": []},
        }
        sandbox = LocalSandbox()
        try:
            result = L1LifeJudge(client).evaluate(check, "answer", sandbox, {})
        finally:
            sandbox.close()
        self.assertEqual(client.calls, 2)
        self.assertEqual(result.score, 1.0)

    def test_runner_smoke_executes_life_tool_and_component_judge(self) -> None:
        class Client:
            def complete(self, messages, **kwargs):
                return json.dumps({
                    "stream_id": "stream-smoke", "turn_id": "turn-tool",
                    "check_scores": [{"check_id": "tool", "score": 1}],
                    "source_rubric_scores": [], "rationale": "tool result is present",
                })

        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "agent": {"type": "scripted", "policy": {"turn-tool": {"1": {
                "response": "Reminder created.",
                "tool_calls": [{"tool": "calendar.create_reminder", "arguments": {
                    "calendar_id": "cal_smoke", "title": "Smoke", "date": "2026-11-15",
                }}],
            }}}},
            "judge": {"type": "deterministic"},
            "flows": [{
                "id": "stream-smoke", "sandbox": {"backend": "local"},
                "turns": [{
                    "id": "turn-tool", "request": "Create a local reminder.",
                    "setup": {"files": [{"path": "/workspace/tool_state/current_state.json", "content": "{}"}]},
                    "verifier": {
                        "type": "l1_life_judge", "stream_id": "stream-smoke", "turn_id": "turn-tool",
                        "judge_prompt": "{tool_calls}",
                        "checks": [{"check_id": "tool", "dimension": "tool_execution", "weight": 1, "scoring": "ordinal"}],
                        "rubrics": {"source_rubrics": []},
                    },
                }],
            }],
        })
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(spec, output_dir=Path(tmp), backend="local", judge_mode="deterministic")
            runner.judge_mode = "hybrid"
            runner.client = Client()
            result = runner.run(spec.flows[0])
        self.assertEqual(result["metrics"]["turn_success_rate"], 1.0)
        tool_call = result["turns"][0]["interactions"][0]["tool_calls"][0]
        self.assertEqual(tool_call["result"]["status"], "created")


if __name__ == "__main__":
    unittest.main()
