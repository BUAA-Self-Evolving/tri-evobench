import json
import tempfile
import unittest
from pathlib import Path

from trievo_bench.runner import Runner
from trievo_bench.spec import load_spec


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


if __name__ == "__main__":
    unittest.main()
