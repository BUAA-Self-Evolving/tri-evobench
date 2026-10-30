from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from baseline.adapters.adaptive_auto_harness.bridge import (
    AdaptiveAutoHarnessBridge,
    OpenAIChatBackend,
    _allowed_state_path,
    _apply_proposal,
    _evolve_config,
    _public_evolution_context,
    _run_native_evolution,
    _Completion,
)
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class AdaptiveAutoHarnessBaselineContractTest(unittest.TestCase):
    def test_model_requests_honor_timeout_and_episode_deadline_without_sdk_retry(self) -> None:
        calls = []
        class Client:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            def with_options(self, **kwargs):
                calls.append(kwargs)
                return self
            def create(self, **kwargs):
                message = SimpleNamespace(content="ok", tool_calls=[])
                return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                                       usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=Client)}), \
             patch.dict(os.environ, {"TRIEVO_API_KEY": "offline-test"}):
            backend = OpenAIChatBackend({"model":"mock", "bounded_model_requests":True,
                                         "request_timeout_seconds":120}, stage="interaction")
            with patch("baseline.adapters.adaptive_auto_harness.bridge.time.monotonic", return_value=100):
                backend.set_deadline(103)
                backend.complete([])
                self.assertEqual(calls, [{"timeout":3, "max_retries":0}])
                backend.set_deadline(99)
                with self.assertRaises(TimeoutError):
                    backend.complete([])
                self.assertEqual(len(calls),1)

    def test_invalid_learning_proposal_returns_bounded_failure_without_retry(self) -> None:
        from baseline.adapters.adaptive_auto_harness.bridge import EvolutionProposalError
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                bridge = AdaptiveAutoHarnessBridge(backend_factory=lambda options, stage: object())
                bridge.state_dir = Path("unused")
                bridge.turn_id = "turn2"
                bridge.options = {"recoverable_evolution_proposal_errors": enabled}
                usage = {"api_calls": 1, "total_tokens": 8192}
                with patch("baseline.adapters.adaptive_auto_harness.bridge._run_native_evolution",
                           side_effect=EvolutionProposalError("no JSON", usage)) as native:
                    if enabled:
                        result = bridge.evolve({"turn_id": "turn2", "update_enabled": True})
                        self.assertFalse(result["completed"])
                        self.assertTrue(result["recoverable_failure"])
                        self.assertEqual(result["usage"], usage)
                        self.assertEqual(result["events"][0]["type"], "evolution.proposal_rejected")
                    else:
                        with self.assertRaises(EvolutionProposalError):
                            bridge.evolve({"turn_id": "turn2", "update_enabled": True})
                    native.assert_called_once()

    def test_failed_learning_reply_is_saved_before_parsing_without_retry(self) -> None:
        class Engine:
            def __init__(self, config):
                pass

            def evolve(self, workspace, logs, evo_number):
                return self._run_llm("public evidence", workspace.root)

        modules = {
            "agent_evolve.algorithms.aevolve.engine": SimpleNamespace(AEvolveEngine=Engine),
            "agent_evolve.config": SimpleNamespace(EvolveConfig=lambda **kwargs: SimpleNamespace(**kwargs)),
            "agent_evolve.contract.workspace": SimpleNamespace(AgentWorkspace=lambda root: SimpleNamespace(root=root)),
        }
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                replies = []
                reply = _Completion('{"files":', [], {"total_tokens": 17, "api_calls": 1}, "length",
                                    reasoning_text="original visible reasoning")
                def complete(messages):
                    replies.append(messages)
                    return reply
                with patch.dict(sys.modules, modules):
                    with self.assertRaises(ValueError):
                        _run_native_evolution(root, {"turn_id": "t1"}, SimpleNamespace(complete=complete),
                                              {"preserve_evolution_completions": enabled})
                self.assertEqual(len(replies), 1)
                paths = list((root / "evolution/history/completions").glob("*.json"))
                self.assertEqual(len(paths), int(enabled))
                if enabled:
                    saved = json.loads(paths[0].read_text())
                    self.assertEqual(saved["text"], reply.text)
                    self.assertEqual(saved["finish_reason"], "length")
                    self.assertEqual(saved["reasoning_text"], reply.reasoning_text)
                    self.assertEqual(saved["usage"], reply.usage)
                    self.assertEqual(saved["turn_id"], "t1")
                    self.assertNotIn("messages", saved)
                self.assertFalse((root / "prompts/system.md").exists())

    def test_friday_thinking_mode_is_forwarded(self) -> None:
        calls = []

        class Completions:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                message = SimpleNamespace(content="ok", tool_calls=[])
                choice = SimpleNamespace(message=message, finish_reason="stop")
                usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
                return SimpleNamespace(choices=[choice], usage=usage)

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=Completions())

        fake_module = SimpleNamespace(OpenAI=FakeOpenAI)
        environment = {**os.environ, "FRIDAY_APP_ID": "test", "TRIEVO_API_KEY": "test"}
        with patch.dict(sys.modules, {"openai": fake_module}), patch.dict(os.environ, environment, clear=True):
            backend = OpenAIChatBackend({"model": "deepseek-v4-flash", "thinking": "enabled"}, stage="interaction")
            backend.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "enabled"}})

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
