from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from baseline.adapters.prime_agent.bridge import PrimeAgentBridge, _public_evolution_context, _usage_delta
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.spec import BenchmarkSpec


class _FakePrimeRpc:
    instances: list["_FakePrimeRpc"] = []

    def __init__(self, command, env):
        self.command = command
        self.env = env
        self.prompts: list[str] = []
        self.refinements: list[str] = []
        self.closed = False
        self.__class__.instances.append(self)

    def request(self, command_type, **params):
        if command_type == "get_state":
            return {"sessionId": str(len(self.instances))}, []
        if command_type == "get_session_stats":
            calls = len(self.prompts) + len(self.refinements)
            return {
                "assistantMessages": calls,
                "tokens": {"input": calls * 10, "output": calls * 2, "cacheRead": 0, "cacheWrite": 0},
                "cost": calls * 0.01,
            }, []
        if command_type == "prompt":
            self.prompts.append(params["message"])
            return {}, [
                {"type": "tool_execution_end", "toolName": "ipython", "args": {}, "result": "ok"},
                {"type": "agent_end"},
            ]
        if command_type == "get_last_assistant_text":
            return {"text": "done"}, []
        if command_type == "refine":
            self.refinements.append(params["instructions"])
            return {"id": "ref-1", "appliedEdits": [{"applied": True}]}, [{"type": "refine_complete"}]
        raise AssertionError(command_type)

    def close(self):
        self.closed = True


class PrimeAgentBaselineContractTest(unittest.TestCase):
    def test_manifest_registers_fixed_revision_and_harness_state(self) -> None:
        manifest = load_manifest(Path(__file__).parents[1] / "baseline" / "manifests" / "prime-agent.json")
        self.assertEqual(manifest.baseline_id, "prime-agent")
        self.assertEqual(manifest.source["revision"], "163dd5798cd40e86b540aa9ecb68201920a83a1b")
        self.assertIn("prime-home/harness/**/*", manifest.state["paths"])
        self.assertTrue(manifest.state["enforce_turn_freeze"])
        self.assertEqual(manifest.evolution["policy"], "turn_boundary")
        self.assertTrue(manifest.options["use_dist"])

    def test_spec_hyperparameters_select_prime_agent(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {"type": "prime-agent", "options": {"thinking": "high"}},
            "flows": [{"id": "f", "turns": [{"id": "t", "request": "x", "verifier": {"type": "always"}}]}],
        })
        baseline_id, manifest, overrides = resolve_baseline_selection(
            spec, baseline_override=None, agent_override=None, config_override=None, manifest_override=None
        )
        self.assertEqual(baseline_id, "prime-agent")
        self.assertIsNone(manifest)
        self.assertEqual(overrides["options"]["thinking"], "high")

    def test_fresh_session_per_turn_and_explicit_refine_barrier(self) -> None:
        _FakePrimeRpc.instances.clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = PrimeAgentBridge(rpc_factory=lambda command, env: _FakePrimeRpc(command, env))
            bridge.initialize({
                "protocol_version": "1.0",
                "baseline_id": "prime-agent",
                "options": {"use_dist": True},
                "evolution": {},
            })
            bridge.start_flow({"workspace": str(root / "workspace"), "state_dir": str(root / "state")})
            bridge.start_turn({"turn_id": "t1"})
            reply = bridge.interact({"turn_id": "t1", "interaction_index": 1, "request": "solve"})
            evolution = bridge.evolve({
                "turn_id": "t1",
                "trajectory": [{"index": 1, "request": "q", "response": "a", "verifier_details": {"secret": 1}}],
                "feedback": [{"interaction_index": 1, "feedback": "retry", "oracle_update": "secret"}],
                "update_enabled": True,
            })
            bridge.start_turn({"turn_id": "t2"})
            self.assertEqual(len(_FakePrimeRpc.instances), 2)
            self.assertTrue(_FakePrimeRpc.instances[0].closed)
            self.assertIn("--dist", _FakePrimeRpc.instances[0].command)
            self.assertEqual(reply["text"], "done")
            self.assertEqual(reply["usage"]["total_tokens"], 12)
            self.assertEqual(evolution["events"][0]["refinement_id"], "ref-1")
            serialized = _FakePrimeRpc.instances[0].refinements[0]
            self.assertNotIn("verifier_details", serialized)
            self.assertNotIn("oracle_update", serialized)
            self.assertNotIn("secret", serialized)

    def test_usage_is_incremental_and_private_fields_are_removed(self) -> None:
        usage = _usage_delta(
            {"assistantMessages": 2, "tokens": {"input": 10, "output": 4, "cacheRead": 1, "cacheWrite": 2}, "cost": 0.1},
            {"assistantMessages": 4, "tokens": {"input": 30, "output": 10, "cacheRead": 3, "cacheWrite": 5}, "cost": 0.4},
        )
        self.assertEqual(usage["total_tokens"], 31)
        self.assertEqual(usage["api_calls"], 2)
        public = _public_evolution_context({
            "turn_id": "t",
            "trajectory": [{"request": "q", "verifier_details": {"hidden": True}}],
            "feedback": [{"feedback": "public", "oracle_update": "hidden"}],
        })
        encoded = json.dumps(public)
        self.assertNotIn("verifier_details", encoded)
        self.assertNotIn("oracle_update", encoded)


if __name__ == "__main__":
    unittest.main()
