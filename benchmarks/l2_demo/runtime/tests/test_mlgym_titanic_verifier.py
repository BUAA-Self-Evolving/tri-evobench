import unittest
import json
from pathlib import Path

import pandas as pd

from l2_runtime.mlgym_titanic import MLGymTitanicVerifier


REPO_ROOT = Path(__file__).resolve().parents[3]
STREAM_ROOT = REPO_ROOT / "l2_demo/streams/mlgym_titanic_tabular_classification_001"


class MLGymTitanicVerifierTests(unittest.TestCase):
    def test_known_correct_passes_and_broken_fails(self):
        verifier = MLGymTitanicVerifier(allow_unsafe_execution=True)
        for episode_id in ("E001", "E002", "E003"):
            with self.subTest(episode_id=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                assets = episode_dir / "evaluation_assets"
                good = verifier.verify(
                    (assets / "known_correct.py").read_text(), episode_dir
                )
                bad = verifier.verify(
                    (assets / "known_broken.py").read_text(), episode_dir
                )
                self.assertEqual(good.category, "pass")
                self.assertGreaterEqual(
                    good.score,
                    json.loads((episode_dir / "evaluation.json").read_text())[
                        "scoring"
                    ]["success_threshold"],
                )
                self.assertEqual(bad.category, "fail")

    def test_private_ground_truth_is_not_in_agent_workspace(self):
        episode_dir = STREAM_ROOT / "episodes" / "E001"
        stream_dir = episode_dir.parents[1]
        self.assertFalse((stream_dir / "workspace/data/ground_truth.csv").exists())
        self.assertTrue((episode_dir / "evaluation_assets/ground_truth.csv").is_file())
        evaluation = json.loads((episode_dir / "evaluation.json").read_text())
        self.assertEqual(evaluation["scoring"]["success_threshold"], 0.76555)
        train = pd.read_csv(stream_dir / "workspace/data/train.csv")
        test = pd.read_csv(stream_dir / "workspace/data/test.csv")
        self.assertIn("Survived", train.columns)
        self.assertNotIn("Survived", test.columns)

    def test_derived_episodes_use_namespaced_data_in_one_workspace(self):
        for episode_id in ("E002", "E003"):
            with self.subTest(episode_id=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                episode = json.loads((episode_dir / "episode.json").read_text())
                test = pd.read_csv(STREAM_ROOT / f"workspace/episodes/{episode_id}/data/test.csv")
                truth = pd.read_csv(episode_dir / "evaluation_assets/ground_truth.csv")
                self.assertEqual(episode["workspace_transition"]["mode"], "continue")
                self.assertIn(
                    "derived_fixed_test_slice",
                    episode["source_provenance"]["variant_type"],
                )
                self.assertEqual(len(test), len(truth))
