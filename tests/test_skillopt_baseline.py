from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from baseline.adapters.skillopt.bridge import SkillOptBridge, _trajectory_results
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class SkillOptBaselineContractTest(unittest.TestCase):
    def test_manifest_is_registered_with_flow_scoped_skill_state(self) -> None:
        manifest = load_manifest(Path(__file__).parents[1] / "baseline" / "manifests" / "skillopt.json")
        self.assertEqual(manifest.baseline_id, "skillopt")
        self.assertEqual(manifest.adapter["type"], "process")
        self.assertIn("skill.md", manifest.state["paths"])
        self.assertTrue(manifest.state["enforce_turn_freeze"])
        self.assertEqual(manifest.evolution["policy"], "turn_boundary")

    def test_baseline_type_selects_skillopt_without_runner_changes(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {"type": "skillopt", "options": {"edit_budget": 2}},
            "flows": [{
                "id": "flow",
                "turns": [{
                    "id": "turn",
                    "request": "answer",
                    "verifier": {"type": "file_contains", "path": "/workspace/a", "value": "a"},
                }],
            }],
        })
        baseline_id, manifest, overrides = resolve_baseline_selection(
            spec,
            baseline_override=None,
            agent_override=None,
            config_override=None,
            manifest_override=None,
        )
        self.assertEqual(baseline_id, "skillopt")
        self.assertIsNone(manifest)
        self.assertEqual(overrides["options"]["edit_budget"], 2)

    def test_bridge_validates_protocol_and_maps_public_trajectory(self) -> None:
        bridge = SkillOptBridge()
        result = bridge.initialize({
            "protocol_version": "1.0",
            "baseline_id": "skillopt",
            "options": {},
            "evolution": {"policy": "turn_boundary"},
        })
        self.assertEqual(result["protocol_version"], "1.0")
        rows = _trajectory_results(
            [{"index": 1, "request": "q", "response": "a", "score": 0.5, "passed": False}],
            [{"interaction_index": 1, "feedback": "try again", "score": 0.5, "passed": False}],
        )
        self.assertEqual(rows[0]["hard"], 0)
        self.assertEqual(rows[0]["soft"], 0.5)
        self.assertEqual(rows[0]["feedback"], "try again")

    def test_override_file_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text(
                json.dumps({"options": {"target_model": "test-model"}}),
                encoding="utf-8",
            )
            self.assertEqual(json.loads(path.read_text())["options"]["target_model"], "test-model")

    def test_docker_override_uses_container_paths(self) -> None:
        override = json.loads(
            (Path(__file__).parents[1] / "examples" / "skillopt-docker.override.json").read_text(encoding="utf-8")
        )
        self.assertEqual(override["workspace"]["adapter_path"], "/workspace")
        self.assertEqual(override["state"]["adapter_path"], "/agent-state")


if __name__ == "__main__":
    unittest.main()
