from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from baseline.adapters.opencode.bridge import OpenCodeBridge, _parse_output
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class _FakeOpenCode:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.environments.append(dict(kwargs["env"]))
        stdout = "\n".join(
            [
                json.dumps({"type": "tool_use", "sessionID": "ses-flow", "part": {"tool": "bash", "state": {"status": "completed", "input": {"command": "pwd"}, "output": "/workspace"}}}),
                json.dumps({"type": "step_finish", "sessionID": "ses-flow", "part": {"reason": "tool-calls", "cost": 0.01, "tokens": {"total": 20, "input": 10, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 4}}}}),
                json.dumps({"type": "text", "sessionID": "ses-flow", "part": {"text": "done"}}),
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class OpenCodeBaselineContractTest(unittest.TestCase):
    def test_manifest_registers_persistent_flow_session(self) -> None:
        manifest = load_manifest(Path(__file__).parents[1] / "baseline" / "manifests" / "opencode.json")
        self.assertEqual(manifest.baseline_id, "opencode")
        self.assertEqual(manifest.source["revision"], "df23b7f9488a38e6f8064a0739d4f8cde86d7cfb")
        self.assertIn("session.json", manifest.state["paths"])
        self.assertFalse(manifest.state["enforce_turn_freeze"])
        self.assertEqual(manifest.evolution["policy"], "disabled")

    def test_spec_hyperparameters_select_opencode(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {"type": "opencode", "options": {"model": "openai/gpt-5"}},
            "flows": [{"id": "f", "turns": [{"id": "t", "request": "x", "verifier": {"type": "always"}}]}],
        })
        baseline_id, manifest, overrides = resolve_baseline_selection(
            spec, baseline_override=None, agent_override=None, config_override=None, manifest_override=None
        )
        self.assertEqual(baseline_id, "opencode")
        self.assertIsNone(manifest)
        self.assertEqual(overrides["options"]["model"], "openai/gpt-5")

    def test_all_turns_and_interactions_reuse_one_session(self) -> None:
        fake = _FakeOpenCode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = OpenCodeBridge(run_command=fake)
            initialized = bridge.initialize({
                "protocol_version": "1.0",
                "baseline_id": "opencode",
                "options": {"executable": ["opencode"], "model": "openai/gpt-5"},
                "evolution": {"policy": "disabled"},
            })
            started = bridge.start_flow({
                "flow_id": "stream-1", "workspace": str(root / "workspace"), "state_dir": str(root / "state")
            })
            bridge.start_turn({"turn_id": "turn-1"})
            first = bridge.interact({"turn_id": "turn-1", "interaction_index": 1, "request": "first"})
            bridge.interact({"turn_id": "turn-1", "interaction_index": 2, "request": "retry"})
            bridge.evolve({"turn_id": "turn-1", "update_enabled": True})
            bridge.start_turn({"turn_id": "turn-2"})
            bridge.interact({"turn_id": "turn-2", "interaction_index": 1, "request": "next"})

            self.assertEqual(initialized["session_policy"], "one_per_flow")
            self.assertFalse(started["resumed_session"])
            self.assertNotIn("--session", fake.commands[0])
            self.assertEqual(fake.commands[1][fake.commands[1].index("--session") + 1], "ses-flow")
            self.assertEqual(fake.commands[2][fake.commands[2].index("--session") + 1], "ses-flow")
            self.assertEqual(first["text"], "done")
            self.assertEqual(first["usage"]["total_tokens"], 20)
            self.assertEqual(first["usage"]["api_calls"], 1)
            self.assertEqual(first["tool_calls"][0]["tool"], "bash")
            self.assertEqual(json.loads((root / "state" / "session.json").read_text())["session_id"], "ses-flow")
            self.assertEqual(fake.environments[0]["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "16384")

    def test_checkpointed_session_id_is_resumed(self) -> None:
        fake = _FakeOpenCode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            (state / "session.json").write_text(json.dumps({"session_id": "ses-old"}), encoding="utf-8")
            bridge = OpenCodeBridge(run_command=fake)
            bridge.initialize({"protocol_version": "1.0", "baseline_id": "opencode", "options": {"executable": ["opencode"]}})
            result = bridge.start_flow({"flow_id": "stream-1", "workspace": str(root / "workspace"), "state_dir": str(state)})
            self.assertTrue(result["resumed_session"])
            self.assertEqual(bridge.session_id, "ses-old")

    def test_parser_ignores_non_json_diagnostics_and_reports_errors(self) -> None:
        parsed = _parse_output('diagnostic\n{"type":"error","sessionID":"s","error":{"data":{"message":"bad"}}}\n')
        self.assertEqual(parsed["session_id"], "s")
        self.assertEqual(parsed["error"], "bad")


if __name__ == "__main__":
    unittest.main()
