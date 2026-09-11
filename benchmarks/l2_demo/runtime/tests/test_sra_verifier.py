import json
import unittest
from pathlib import Path

from l2_runtime.sra_bigcodebench import (
    SraBigCodeBenchVerifier,
    _clean_subprocess_environment,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
STREAM_ROOT = (
    REPO_ROOT
    / "l2_demo"
    / "streams"
    / "sra_bigcodebench_pandas_dataframe_viz_001"
)


class SraBigCodeBenchVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = SraBigCodeBenchVerifier(allow_unsafe_execution=True)

    def test_known_correct_passes_and_broken_fails_for_every_episode(self):
        for episode_id in ("E001", "E002"):
            with self.subTest(episode=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                assets = episode_dir / "evaluation_assets"
                source_item = json.loads((assets / "source_item.json").read_text())
                correct = (assets / "code_prompt.txt").read_text() + (
                    assets / "reference_answer.txt"
                ).read_text()
                entrypoint = source_item["eval_data"]["entry_point"]
                broken = f"def {entrypoint}(*args, **kwargs):\n    return None\n"

                good_result = self.verifier.verify(correct, episode_dir)
                bad_result = self.verifier.verify(broken, episode_dir)

                self.assertEqual(good_result.category, "pass")
                self.assertEqual(good_result.score, 1.0)
                self.assertEqual(bad_result.category, "fail")
                self.assertEqual(bad_result.score, 0.0)

    def test_final_response_contract_accepts_direct_python_source(self):
        episode_dir = STREAM_ROOT / "episodes" / "E001"
        assets = episode_dir / "evaluation_assets"
        raw_code = (assets / "code_prompt.txt").read_text() + (
            assets / "reference_answer.txt"
        ).read_text()

        result = self.verifier.verify(raw_code, episode_dir)
        self.assertEqual((result.category, result.score), ("pass", 1.0))

    def test_invalid_submission_sanitizes_to_stable_fail_not_evaluator_error(self):
        episode_dir = STREAM_ROOT / "episodes" / "E001"

        for invalid_source in (
            "this is not Python code",
            "```python\nthis is still not Python\n```",
            "def unrelated():\n    return 1\n",
        ):
            with self.subTest(invalid_source=invalid_source):
                result = self.verifier.verify(invalid_source, episode_dir)
                self.assertEqual((result.category, result.score), ("fail", 0.0))
                self.assertNotEqual(result.category, "error")

    def test_body_only_gold_is_not_mislabeled_as_raw_model_output(self):
        evaluation = json.loads(
            (STREAM_ROOT / "episodes/E001/evaluation.json").read_text()
        )
        self.assertEqual(
            evaluation["gold"]["reference_answer_format"],
            "function_body",
        )
        self.assertEqual(
            evaluation["gold"]["known_correct_raw_output_assembly"],
            "code_prompt + reference_answer",
        )

    def test_generated_code_environment_does_not_inherit_credentials(self):
        import os
        import tempfile

        previous = os.environ.get("L2_API_KEY")
        os.environ["L2_API_KEY"] = "test-secret-that-must-not-cross-boundary"
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                child_env = _clean_subprocess_environment(Path(temp_dir))
        finally:
            if previous is None:
                os.environ.pop("L2_API_KEY", None)
            else:
                os.environ["L2_API_KEY"] = previous

        self.assertNotIn("L2_API_KEY", child_env)
        self.assertFalse(any("KEY" in name or "TOKEN" in name for name in child_env))
