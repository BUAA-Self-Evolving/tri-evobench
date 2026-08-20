import json
import tempfile
import unittest
from pathlib import Path

from trievo_bench.runner import Runner
from trievo_bench.spec import BenchmarkSpec, load_spec


class RuntimeTest(unittest.TestCase):
    def test_smoke_flow(self) -> None:
        spec = load_spec(Path(__file__).parents[1] / "examples" / "smoke.json")
        with tempfile.TemporaryDirectory() as tmp:
            result = Runner(spec, output_dir=Path(tmp), backend="local", agent_mode="scripted", judge_mode="deterministic").run(spec.flows[0])
            self.assertEqual(result["metrics"]["turn_count"], 2)
            self.assertEqual(result["metrics"]["turn_success_rate"], 1.0)
            self.assertEqual(result["metrics"]["interaction_count"], 3)
            self.assertEqual(result["token_budget"]["used"], 0)
            saved = json.loads((Path(tmp) / "smoke-flow-001" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["metrics"]["turn_success_rate"], 1.0)

    def test_raw_replay_is_persistent(self) -> None:
        spec = load_spec(Path(__file__).parents[1] / "examples" / "smoke.json")
        with tempfile.TemporaryDirectory() as tmp:
            result = Runner(spec, output_dir=Path(tmp), backend="local", learner_mode="raw-replay").run(spec.flows[0])
            self.assertEqual(len(result["state"]["raw_replay"]), 2)

    def test_evolving_applies_agent_state_update_after_turn(self) -> None:
        spec = load_spec(Path(__file__).parents[1] / "examples" / "smoke.json")
        with tempfile.TemporaryDirectory() as tmp:
            result = Runner(spec, output_dir=Path(tmp), backend="local", learner_mode="evolving").run(spec.flows[0])
            self.assertEqual(result["state"]["last_feedback_target"], "report_snake_case.txt")

    def test_baseline_runs_to_max_and_uses_last_failed_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verifier_path = Path(tmp) / "verify.py"
            verifier_path.write_text(
                "def verify(response, workspace, trajectory):\n"
                "    score = float(response.split('=', 1)[1])\n"
                "    return {'score': score, 'feedback': 'continue'}\n",
                encoding="utf-8",
            )
            spec = BenchmarkSpec.from_dict({
                "schema_version": "0.1",
                "agent": {
                    "type": "scripted",
                    "policy": {
                        "turn-sequence": {
                            "1": {"response": "score=0.2"},
                            "2": {"response": "score=0.1"},
                            "3": {"response": "score=0.4"},
                        }
                    },
                },
                "flows": [{
                    "id": "baseline-flow",
                    "sandbox": {"backend": "local"},
                    "turns": [{
                        "id": "turn-sequence",
                        "request": "Try the task.",
                        "max_interactions": 3,
                        "success_threshold": 0.9,
                        "user": {"followups": ["Fix this: {feedback}", "Try one final time."]},
                        "verifier": {"type": "python_verifier", "path": str(verifier_path)},
                    }],
                }],
            })
            result = Runner(spec, output_dir=Path(tmp), backend="local").run(spec.flows[0])

        turn = result["turns"][0]
        self.assertEqual(result["metrics"]["interaction_count"], 3)
        self.assertFalse(turn["passed"])
        self.assertEqual(turn["score"], 0.4)
        self.assertEqual(turn["interactions"][1]["request"], "Fix this: continue")


if __name__ == "__main__":
    unittest.main()
