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

    def test_taskstream_preflight_validates_runtime_relevant_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream = root / "streams" / "taskstream"
            (stream / "agent_workspace").mkdir(parents=True)
            (stream / "turn1" / "assets").mkdir(parents=True)
            (stream / "turn1" / "assets" / "input.txt").write_text("input")
            manifest = {
                "schema_version": "l2_taskstream/1.0",
                "stream_id": "taskstream",
                "agent_workspace": "agent_workspace",
                "ordering": {"turn_order": ["turn1"]},
                "execution_requirements": {
                    "network": "disabled",
                    "path_scope": "agent_workspace",
                    "max_interactions": 3,
                    "max_tool_calls_per_interaction": 20,
                    "turn_timeout_seconds": 900,
                    "max_output_tokens": 12000,
                },
                "turns": [{
                    "turn_id": "turn1",
                    "turn_index": 1,
                    "sequence_role": "learning",
                }],
            }
            task = {
                "stream_id": "taskstream",
                "turn_id": "turn1",
                "turn_index": 1,
                "sequence_role": "learning",
                "agent_input": {"messages": [{"role": "user", "content": "work"}]},
                "workspace_transition": {
                    "mode": "continue_with_patch",
                    "operations": [{
                        "op": "copy",
                        "source": "assets/input.txt",
                        "target": "agent_workspace/input.txt",
                    }],
                },
                "evaluation": {"native_verifier": {"required": True}},
            }
            (stream / "stream.json").write_text(json.dumps(manifest))
            (stream / "turn1" / "task.json").write_text(json.dumps(task))

            inventory = inspect_l2_package(root)
            self.assertEqual(inventory[0].mode, "taskstream")

            task["sequence_role"] = "forgetting"
            (stream / "turn1" / "task.json").write_text(json.dumps(task))
            with self.assertRaisesRegex(L2SpecError, "identity|sequence_role"):
                inspect_l2_package(root)


if __name__ == "__main__":
    unittest.main()
