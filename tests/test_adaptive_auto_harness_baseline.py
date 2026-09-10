from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from baseline.adapters.adaptive_auto_harness.bridge import (
    AdaptiveAutoHarnessBridge,
    _allowed_state_path,
    _apply_proposal,
    _evolve_config,
    _public_evolution_context,
)
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class AdaptiveAutoHarnessBaselineContractTest(unittest.TestCase):
    def test_manifest_registers_fixed_revision_and_contract_state(self) -> None:
        manifest = load_manifest(
            Path(__file__).parents[1] / "baseline" / "manifests" / "adaptive-auto-harness.json"
        )
        self.assertEqual(manifest.baseline_id, "adaptive-auto-harness")
        self.assertEqual(manifest.source["revision"], "17bc9ebb7d4d142af1b109b43ef160031967cc9a")
        self.assertIn("prompts/**/*", manifest.state["paths"])
        self.assertIn("skills/**/*", manifest.state["paths"])
        self.assertIn("memory/**/*", manifest.state["paths"])
        self.assertTrue(manifest.state["enforce_turn_freeze"])

    def test_spec_hyperparameters_select_adaptive_auto_harness(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {"type": "adaptive-auto-harness", "options": {"max_steps": 7}},
            "flows": [{"id": "f", "turns": [{"id": "t", "request": "x", "verifier": {"type": "always"}}]}],
        })
        baseline_id, manifest, overrides = resolve_baseline_selection(
            spec, baseline_override=None, agent_override=None, config_override=None, manifest_override=None
        )
        self.assertEqual(baseline_id, "adaptive-auto-harness")
        self.assertIsNone(manifest)
        self.assertEqual(overrides["options"]["max_steps"], 7)

    def test_evolution_proposal_can_only_touch_enabled_contract_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _evolve_config({"evolve_tools": False})
            changed = _apply_proposal(root, {
                "files": {
                    "prompts/system.md": "improved",
                    "skills/review/SKILL.md": "---\nname: review\n---\ncheck",
                    "tools/unsafe.py": "bad",
                    "../escape": "bad",
                }
            }, config)
            self.assertEqual(changed, ["prompts/system.md", "skills/review/SKILL.md"])
            self.assertFalse((root.parent / "escape").exists())
            self.assertIsNone(_allowed_state_path("tools/unsafe.py", config))

    def test_disabled_evolution_crosses_barrier_without_backend(self) -> None:
        bridge = AdaptiveAutoHarnessBridge()
        bridge.turn_id = "t"
        bridge.state_dir = Path("/tmp/not-used")
        result = bridge.evolve({"turn_id": "t", "update_enabled": False, "skip_reason": "frozen"})
        self.assertTrue(result["completed"])
        self.assertEqual(result["events"][0]["type"], "evolution.skipped")

    def test_private_fields_are_removed_from_evolution_context(self) -> None:
        public = _public_evolution_context({
            "turn_id": "t",
            "trajectory": [{"request": "q", "response": "a", "verifier_details": {"hidden": True}}],
            "feedback": [{"feedback": "public", "oracle_update": "hidden"}],
        })
        encoded = json.dumps(public)
        self.assertNotIn("verifier_details", encoded)
        self.assertNotIn("oracle_update", encoded)
        self.assertIn("public", encoded)


if __name__ == "__main__":
    unittest.main()
