import unittest
import json
from pathlib import Path

import pandas as pd

from l2_runtime.airs_sick import AirsSickAccuracyVerifier


REPO_ROOT = Path(__file__).resolve().parents[3]
STREAM_ROOT = REPO_ROOT / "l2_demo/streams/airs_sick_textual_entailment_accuracy_001"


class AirsSickVerifierTests(unittest.TestCase):
    def test_known_correct_passes_and_broken_fails(self):
        verifier = AirsSickAccuracyVerifier(allow_unsafe_execution=True)
        for episode_id in ("E001", "E002", "E003"):
            with self.subTest(episode_id=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                assets = episode_dir / "evaluation_assets"
                good = verifier.verify((assets / "known_correct.py").read_text(), episode_dir)
                bad = verifier.verify((assets / "known_broken.py").read_text(), episode_dir)
                self.assertEqual(good.category, "pass")
                self.assertEqual(good.score, 1.0)
                self.assertEqual(bad.category, "fail")
                self.assertEqual(bad.score, 0.0)

    def test_native_split_shape_and_private_labels_are_preserved(self):
        episode_dir = STREAM_ROOT / "episodes" / "E001"
        stream_dir = episode_dir.parents[1]
        test = pd.read_csv(stream_dir / "workspace/data/test.csv")
        labels = pd.read_csv(episode_dir / "evaluation_assets/test_labels.csv")
        self.assertEqual(len(test), 4906)
        self.assertEqual(len(labels), 4906)
        self.assertEqual(list(test.columns), ["sentence_A", "sentence_B"])
        self.assertEqual(list(labels.columns), ["label"])
        evaluation = json.loads((episode_dir / "evaluation.json").read_text())
        self.assertEqual(
            evaluation["scoring"]["success_threshold_source"],
            "native metadata.logging_info.estimated_worst_score",
        )

    def test_derived_episodes_use_namespaced_data_in_one_workspace(self):
        for episode_id in ("E002", "E003"):
            with self.subTest(episode_id=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                episode = json.loads((episode_dir / "episode.json").read_text())
                labels = pd.read_csv(episode_dir / "evaluation_assets/test_labels.csv")
                test = pd.read_csv(STREAM_ROOT / f"workspace/episodes/{episode_id}/data/test.csv")
                self.assertEqual(episode["workspace_transition"]["mode"], "continue")
                self.assertIn("derived_fixed_test_slice", episode["source_provenance"]["variant_type"])
                self.assertEqual(len(labels), 1200)
                self.assertEqual(len(test), 1200)
