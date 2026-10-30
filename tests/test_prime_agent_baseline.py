from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from baseline.adapters.prime_agent.bridge import (
    PrimeAgentBridge,
    PrimeRpcError,
    PrimeRpcProcess,
    _public_evolution_context,
    _usage_delta,
)
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


class _LimitedPrimeRpc(_FakePrimeRpc):
    def __init__(self, command, env):
        super().__init__(command, env)
        self.abort_calls = 0

    def request(self, command_type, **params):
        if command_type == "prompt":
            self.prompts.append(params["message"])
            return {}, [
                {"type": "message_end", "message": {"role": "assistant"}},
                {"type": "tool_execution_start", "toolName": "ipython"},
                {"type": "tool_execution_end", "toolName": "ipython", "result": "ok"},
                {"type": "agent_end"},
            ]
        return super().request(command_type, **params)

    def abort(self):
        self.abort_calls += 1


class PrimeAgentBaselineContractTest(unittest.TestCase):
    def test_refine_response_is_terminal_despite_background_agent_events(self) -> None:
        code = r'''
import json, sys
def emit(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
    q = json.loads(line)
    if q["type"] == "refine":
        emit({"type": "agent_start"})
        emit({"type": "agent_end"})
        emit({"type": "refine_complete", "result": {"id": "saved-refinement"}})
        emit({"id": q["id"], "type": "response", "success": True,
              "data": {"id": "saved-refinement", "appliedEdits": [{"applied": True}]}})
    else:
        emit({"id": q["id"], "type": "response", "success": True,
              "data": {"isStreaming": True, "isCompacting": False}})
'''
        rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))
        try:
            result, events = rpc.request("refine", instructions="once", deadline=time.monotonic() + 0.3)
            self.assertEqual(result["id"], "saved-refinement")
            self.assertEqual(events[-1]["type"], "refine_complete")
            self.assertEqual(rpc._request_id, 1)
        finally:
            rpc.close()

    def test_episode_deadline_collects_original_prefix_after_confirmed_cancel(self) -> None:
        code = r'''
import json, sys
prompt_count = 0
def emit(v): print(json.dumps(v), flush=True)
for line in sys.stdin:
    q = json.loads(line); kind=q["type"]; data={}
    if kind == "prompt":
        prompt_count += 1
        assert prompt_count == 1
        emit({"type":"agent_start"})
        emit({"type":"tool_execution_end","toolName":"ipython","result":"original result"})
        continue
    if kind == "get_state": data={"isStreaming":False,"isCompacting":False}
    if kind == "get_session_stats": data={"assistantMessages":prompt_count,"tokens":{"input":prompt_count*10,"output":prompt_count*2}}
    if kind == "get_last_assistant_text": data={"text":"original prefix"}
    emit({"id":q["id"],"type":"response","success":True,"data":data})
'''
        rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))
        with tempfile.TemporaryDirectory() as tmp:
            bridge = PrimeAgentBridge(rpc_factory=lambda command, env: rpc)
            bridge.initialize({"protocol_version":"1.0", "options":{"collect_episode_limit_reply":True}})
            bridge.start_flow({"workspace":tmp+"/workspace", "state_dir":tmp+"/state"})
            try:
                bridge.start_turn({"turn_id":"t1", "metadata":{"agent_limits":{"episode_timeout_seconds":0.15}}})
                reply = bridge.interact({"turn_id":"t1", "request":"original request"})
            finally:
                rpc.close()
        self.assertEqual(reply["text"], "original prefix")
        self.assertTrue(reply["raw"]["limit_exhausted"])
        self.assertEqual(reply["raw"]["limit_reasons"], ["episode_timeout"])
        self.assertEqual(reply["usage"]["total_tokens"], 12)
        self.assertEqual(len(reply["tool_calls"]), 1)

    def test_cancel_grace_fails_closed_if_session_never_becomes_idle(self) -> None:
        code = r'''
import json, sys
for line in sys.stdin:
    q=json.loads(line)
    if q["type"]=="prompt": continue
    print(json.dumps({"id":q["id"],"type":"response","success":True,"data":{"isStreaming":True}}),flush=True)
'''
        rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))
        try:
            with self.assertRaisesRegex(PrimeRpcError,"absolute deadline"):
                rpc.request("prompt",message="only once",deadline=time.monotonic()+0.05,cancel_grace_seconds=0.1)
        finally:
            rpc.close()

    def test_budget_abort_ack_requires_idle_without_agent_end(self) -> None:
        code = r'''
import json, sys
def emit(v): print(json.dumps(v), flush=True)
p = json.loads(sys.stdin.readline())
emit({"type":"agent_start"})
emit({"type":"tool_execution_start", "toolName":"ipython"})
a = json.loads(sys.stdin.readline())
assert a["type"] == "abort"
emit({"id":p["id"],"type":"response","success":True})
emit({"id":a["id"],"type":"response","success":True})
s = json.loads(sys.stdin.readline())
assert s["type"] == "get_state"
emit({"id":s["id"],"type":"response","success":True,"data":{"isStreaming":False,"isCompacting":False}})
'''
        rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))
        try:
            _, events = rpc.request(
                "prompt", message="only once", deadline=time.monotonic() + 0.5,
                event_callback=lambda event: rpc.abort() if event.get("type") == "tool_execution_start" else None,
            )
        finally:
            rpc.close()
        self.assertEqual([e["type"] for e in events], ["agent_start", "tool_execution_start"])

    def test_rpc_uses_authoritative_idle_state_across_retry_race(self) -> None:
        code = r'''
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "success": True})
emit({"type": "agent_start"})
emit({"type": "agent_end"})
state1 = json.loads(sys.stdin.readline())
emit({"type": "agent_start"})
emit({"id": state1["id"], "type": "response", "success": True, "data": {"isStreaming": True}})
emit({"type": "agent_end"})
state2 = json.loads(sys.stdin.readline())
emit({"id": state2["id"], "type": "response", "success": True, "data": {"isStreaming": False}})
'''
        rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))
        try:
            _, events = rpc.request("prompt", message="x", deadline=time.monotonic() + 5)
        finally:
            rpc.close()
        self.assertEqual(
            [event["type"] for event in events],
            ["agent_start", "agent_end", "agent_start", "agent_end"],
        )

    def test_rpc_absolute_deadline_aborts_nonterminating_prompt(self) -> None:
        code = "import sys,time; sys.stdin.readline(); time.sleep(0.2)"
        rpc = PrimeRpcProcess([sys.executable, "-u", "-c", code], env=dict(os.environ))
        try:
            with self.assertRaisesRegex(PrimeRpcError, "absolute deadline"):
                rpc.request("prompt", message="x", deadline=time.monotonic() + 0.03)
        finally:
            rpc.close()

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

    def test_runtime_settings_bound_provider_timeout_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = PrimeAgentBridge(rpc_factory=lambda command, env: _FakePrimeRpc(command, env))
            bridge.initialize({
                "protocol_version": "1.0",
                "baseline_id": "prime-agent",
                "options": {"request_timeout_seconds": 17, "max_retries": 99},
                "evolution": {},
            })
            bridge.start_flow({"workspace": str(root / "workspace"), "state_dir": str(root / "state")})
            settings = json.loads((root / "state/prime-home/settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["retry"]["maxRetries"], 5)
            self.assertEqual(settings["retry"]["baseDelayMs"], 2000)
            self.assertEqual(settings["retry"]["provider"]["timeoutMs"], 17000)
            self.assertEqual(settings["retry"]["provider"]["maxRetryDelayMs"], 60000)

    def test_turn_tool_budget_blocks_a_second_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = _LimitedPrimeRpc([], {})
            bridge = PrimeAgentBridge(rpc_factory=lambda command, env: rpc)
            bridge.initialize({
                "protocol_version": "1.0",
                "baseline_id": "prime-agent",
                "options": {},
                "evolution": {},
            })
            bridge.start_flow({"workspace": str(root / "workspace"), "state_dir": str(root / "state")})
            bridge.start_turn({
                "turn_id": "t1",
                "metadata": {"agent_limits": {
                    "episode_timeout_seconds": 60,
                    "max_model_calls": 2,
                    "max_tool_calls": 1,
                }},
            })
            first = bridge.interact({"turn_id": "t1", "request": "one"})
            self.assertFalse(first["raw"]["limit_exhausted"])
            with self.assertRaisesRegex(PrimeRpcError, "max_tool_calls"):
                bridge.interact({"turn_id": "t1", "request": "two"})
            self.assertEqual(rpc.abort_calls, 1)
            self.assertEqual(rpc.prompts, ["one"])

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
