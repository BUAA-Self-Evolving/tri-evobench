import json
import tempfile
import unittest
from pathlib import Path

from trievo_bench.l2_compat import L2SpecError, inspect_l2_package


class L2CompatTest(unittest.TestCase):
    def _make_package(self, root: Path, *, leak: bool = False) -> Path:
        stream = root / "streams" / "demo_stream"
        episode = stream / "episodes" / "E001"
        (stream / "workspace").mkdir(parents=True)
        episode.mkdir(parents=True)
        (stream / "stream.json").write_text(json.dumps({
            "stream_id": "demo_stream", "level": "L2",
            "workspace": {"initial_snapshot": "workspace"},
            "ordering": {"episode_order": ["E001"]},
            "episodes": ["episodes/E001/episode.json"],
            "runtime": {"runner": "demo.runner", "episode_result_policy": "latest_interaction"},
        }), encoding="utf-8")
        request = "private evaluation_assets" if leak else "write a result"
        (episode / "episode.json").write_text(json.dumps({
            "episode_id": "E001", "stream_id": "demo_stream",
            "agent_view": {"request": request, "assets": [], "available_tools": []},
            "interaction_policy": {"max_interactions": 3},
            "evaluation_ref": "evaluation.json",
        }), encoding="utf-8")
        (episode / "evaluation.json").write_text(json.dumps({
            "visibility": "benchmark_private",
            "scoring": {"episode_score_policy": "latest_interaction"},
        }), encoding="utf-8")
        return root

    def test_inspects_valid_l2_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = inspect_l2_package(self._make_package(Path(tmp)))
        self.assertEqual(inventory[0].stream_id, "demo_stream")
        self.assertEqual(inventory[0].mode, "api")
        self.assertEqual(inventory[0].episode_count, 1)

    def test_rejects_private_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(L2SpecError):
                inspect_l2_package(self._make_package(Path(tmp), leak=True))


if __name__ == "__main__":
    unittest.main()
