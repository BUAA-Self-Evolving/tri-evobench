from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from trievo_bench.baselines.hermes_bridge import (
    HermesBridge,
    _extract_l1_protocol,
    _is_l1_metadata,
    _complete_l1_file_actions,
    _l1_persistent_state_context,
    _l1_readonly_workspace_context,
    _merge_l1_state,
)
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.process import ProcessBaselineRuntime
from trievo_bench.model import TokenBudget
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.runner import Runner
from trievo_bench.sandbox import LocalSandbox
from trievo_bench.spec import BenchmarkSpec
from trievo_bench.verifier import LLMJudge


class BaselineRuntimeTest(unittest.TestCase):
    def test_checkpoint_resume_retains_prefix_and_learning_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            flow = spec.flows[0]
            flow.turns.append(replace(flow.turns[0], id="process-turn-2"))
            manifest = self._manifest(root)
            original = Runner(spec, output_dir=root / "first", baseline="fake", baseline_manifest=str(manifest)).run(flow)
            checkpoint = next((root / "first" / "_checkpoints").glob("*/t0001"))
            resumed = Runner(spec, output_dir=root / "resumed", baseline="fake", baseline_manifest=str(manifest), resume_checkpoint=checkpoint).run(flow)
            self.assertEqual([row["turn_id"] for row in resumed["turns"]], ["process-turn", "process-turn-2"])
            self.assertEqual(resumed["turns"][0], original["turns"][0])
            live = (root / "resumed" / flow.id / "live_trace.jsonl").read_text(encoding="utf-8")
            self.assertNotIn('"turn_id": "process-turn"', live)
            self.assertEqual(resumed["token_budget"]["used"], original["token_budget"]["used"])

    def _manifest(
        self,
        root: Path,
        *,
        violate_state_freeze: bool = False,
        violate_start_turn: bool = False,
        write_ignored_telemetry: bool = False,
        incomplete_evolution: bool = False,
        source_revision: str | None = None,
        source_path: Path | None = None,
    ) -> Path:
        adapter = Path(__file__).parent / "fixtures" / "fake_baseline_adapter.py"
        manifest = {
            "schema_version": "1.0",
            "id": "fake",
            "adapter": {
                "type": "process",
                "command": [sys.executable, str(adapter)],
                "cwd": "{repo_root}",
                "timeout_seconds": 30,
            },
            "workspace": {"mode": "host_path", "adapter_path": "{workspace}"},
            "state": {
                "adapter_path": "{state_dir}",
                "paths": ["learning/*", "learning/**/*"],
                "ignore_paths": ["learning/telemetry.json"],
                "enforce_turn_freeze": True,
            },
            "evolution": {"policy": "turn_boundary", "timeout_seconds": 30},
            "options": {
                "violate_state_freeze": violate_state_freeze,
                "violate_start_turn": violate_start_turn,
                "write_ignored_telemetry": write_ignored_telemetry,
                "incomplete_evolution": incomplete_evolution,
            },
            "capabilities": ["direct_workspace", "usage", "evolution_barrier"],
        }
        if source_revision is not None:
            manifest["source"] = {
                "path": str(source_path or Path(__file__).parents[1]),
                "revision": source_revision,
            }
        path = root / "fake.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    @staticmethod
    def _spec() -> BenchmarkSpec:
        return BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "benchmark": {"max_total_tokens": 100},
            "judge": {"type": "deterministic"},
            "flows": [{
                "id": "process-flow",
                "sandbox": {"backend": "local"},
                "metadata": {
                    "private_marker": "must not leave the benchmark",
                    "public": {"stream": "visible"},
                },
                "turns": [{
                    "id": "process-turn",
                    "request": "Create answer.txt.",
                    "metadata": {
                        "private_marker": "must not leave the benchmark",
                        "public": {"category": "visible"},
                    },
                    "verifier": {
                        "type": "file_contains",
                        "path": "/workspace/answer.txt",
                        "value": "process baseline",
                    },
                    "oracle_update": {"private_answer": "must not leave the benchmark"},
                }],
            }],
        })

    def test_process_runtime_runs_evolves_and_accounts_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            result = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root)),
            ).run(spec.flows[0])

            learned_path = root / "runs" / "process-flow" / "baseline_state" / "learning" / "process-turn.json"
            learned = json.loads(learned_path.read_text(encoding="utf-8"))

        self.assertEqual(result["baseline"], "fake")
        self.assertEqual(result["metrics"]["turn_success_rate"], 1.0)
        self.assertEqual(result["token_budget"]["used"], 7)
        self.assertEqual(result["token_budget"]["calls"], 2)
        self.assertEqual(result["turns"][0]["evolution"]["changed_files"], ["learning/process-turn.json"])
        public_interaction = learned["trajectory"][0]
        self.assertNotIn("verifier_details", public_interaction)
        self.assertNotIn("oracle_update", json.dumps(learned))
        self.assertNotIn("private_marker", json.dumps(learned))
        self.assertEqual(learned["flow_metadata"], {"stream": "visible"})
        self.assertEqual(learned["turn_metadata"], {"category": "visible"})

    def test_process_runtime_restores_illegal_foreground_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            result = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root, violate_state_freeze=True)),
            ).run(spec.flows[0])
            illegal = root / "runs" / "process-flow" / "baseline_state" / "learning" / "illegal.txt"
            illegal_exists = illegal.exists()

        interaction = result["turns"][0]["interactions"][0]
        self.assertFalse(result["turns"][0]["passed"])
        self.assertEqual(interaction["verifier_details"]["type"], "model_error")
        self.assertIn("persistent learning state", interaction["feedback"])
        self.assertEqual(interaction["usage"]["total_tokens"], 5)
        self.assertFalse(illegal_exists)

    def test_process_runtime_rejects_first_turn_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            runner = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root, violate_start_turn=True)),
            )

            with self.assertRaisesRegex(RuntimeError, "during start_turn"):
                runner.run(spec.flows[0])

    def test_process_runtime_ignores_declared_foreground_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            result = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root, write_ignored_telemetry=True)),
            ).run(spec.flows[0])

            exported = root / "runs" / "process-flow" / "baseline_state" / "learning" / "telemetry.json"

        self.assertTrue(result["turns"][0]["passed"])
        self.assertFalse(exported.exists())

    def test_process_runtime_requires_completed_evolution_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            runner = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root, incomplete_evolution=True)),
            )

            with self.assertRaisesRegex(RuntimeError, "evolution did not complete"):
                runner.run(spec.flows[0])

    def test_process_runtime_archives_deep_state_on_windows_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = self._manifest(root)
            runtime = ProcessBaselineRuntime(
                load_manifest(manifest_path), budget=TokenBudget(limit=100), repo_root=root
            )
            runtime._state_dir = root / "state"
            deep_name = "x" * 80
            source = runtime._state_dir / "learning" / deep_name / "learned.json"
            source.parent.mkdir(parents=True)
            source.write_text("{}", encoding="utf-8")
            output = root / ("run" * 60)
            output.mkdir()

            runtime._export_state(output, runtime.snapshot_state())

            archive = output / "baseline_state.zip"
            self.assertTrue(archive.exists())
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn(f"learning/{deep_name}/learned.json", bundle.namelist())

    def test_hermes_usage_converts_cumulative_tokens_but_not_per_run_calls(self) -> None:
        bridge = HermesBridge()
        first = bridge._usage_delta({
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "api_calls": 2,
        })
        second = bridge._usage_delta({
            "input_tokens": 160,
            "output_tokens": 35,
            "total_tokens": 195,
            "api_calls": 2,
        })

        self.assertEqual(first["total_tokens"], 120)
        self.assertEqual(second["total_tokens"], 75)
        self.assertEqual(second["api_calls"], 2)

    def test_hermes_explicit_disabled_thinking_reaches_acp_request_overrides(self) -> None:
        agent = SimpleNamespace(request_overrides={"extra_body": {"existing": True}})
        state = SimpleNamespace(agent=agent, session_id="session-1")
        bridge = HermesBridge()
        bridge.initialize({
            "protocol_version": "1.0",
            "evolution": {"policy": "disabled"},
            "options": {"thinking": "disabled"},
        })
        bridge._manager = SimpleNamespace(create_session=lambda **_kwargs: state)
        bridge._workspace = "/workspace"

        bridge.start_turn({"turn_id": "turn-1", "metadata": {}})

        self.assertEqual(agent.reasoning_config, {"enabled": False})
        self.assertEqual(agent.request_overrides["extra_body"], {
            "existing": True,
            "thinking": {"type": "disabled"},
            "reasoning": {"enabled": False},
        })

    def test_hermes_failed_conversation_is_not_a_benchmark_response(self) -> None:
        result = {
            "failed": True,
            "completed": False,
            "final_response": "API call failed after 4 retries",
            "error": "provider connect timeout",
            "messages": [],
            "api_calls": 4,
        }
        agent = SimpleNamespace(run_conversation=lambda **_kwargs: result)
        state = SimpleNamespace(agent=agent, history=[], session_id="session-1", cwd="/workspace")
        bridge = HermesBridge()
        bridge._state = state
        bridge._turn_id = "turn-1"
        bridge._workspace = "/workspace"
        bridge._is_taskstream_turn = True
        bridge._manager = SimpleNamespace(save_session=lambda _session_id: None)
        bridge._install_callbacks = lambda *_args: None
        bridge._foreground_policy = lambda *_args: contextlib.nullcontext()

        with self.assertRaisesRegex(RuntimeError, "provider connect timeout"):
            bridge.interact({"turn_id": "turn-1", "request": "Do the task"})

    def test_hermes_background_usage_counts_cache_tokens(self) -> None:
        class Connection:
            def execute(self, query, params):
                return self

            def fetchone(self):
                return (10, 2, 3, 4, 5, 2, 0.25)

        class ReadContext:
            def __enter__(self):
                return Connection()

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class SessionDb:
            def flush_token_counts(self):
                pass

            def _read_ctx(self):
                return ReadContext()

        agent = type("Agent", (), {"_session_db": SessionDb(), "session_id": "session-1"})()
        usage = HermesBridge()._background_review_usage(agent)

        self.assertEqual(usage["total_tokens"], 19)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertEqual(usage["api_calls"], 2)

    def test_hermes_crosses_barrier_without_review_when_update_is_disabled(self) -> None:
        bridge = HermesBridge()
        bridge._state = SimpleNamespace(agent=SimpleNamespace())
        bridge._turn_id = "turn-1"
        bridge._turn_evolved = False
        bridge._policy = "turn_boundary"
        bridge._background_review_usage = lambda _agent: {}

        result = bridge.evolve({
            "turn_id": "turn-1",
            "trajectory": [{"response": "done"}],
            "feedback": [{"score": 1.0}],
            "update_enabled": False,
            "skip_reason": "condition:frozen",
        })

        self.assertTrue(result["completed"])
        self.assertEqual(result["events"], [{
            "type": "evolution.skipped",
            "policy": "turn_boundary",
            "reason": "condition:frozen",
        }])
        self.assertTrue(bridge._turn_evolved)

    def test_hermes_l1_protocol_keeps_only_visible_text_and_normalizes_fact_aliases(self) -> None:
        visible, state_update, calls, events, actions = _extract_l1_protocol(
            "已创建会议。\n"
            "TRIEVO_TOOL_CALL: {\"tool\": \"calendar.create_event\", \"arguments\": {\"calendar_id\": \"cal_delivery_team\"}}\n"
            "TRIEVO_EVENT: {\"event\": \"asked_for_calendar_id\"}\n"
            "TRIEVO_ACTION: {\"op\": \"write_file\", \"path\": \"/workspace/docs/a.txt\", \"content\": \"text\"}\n"
            "TRIEVO_STATE_UPDATE: {\"facts\": {\"default_work_language\": {\"value\": \"zh-CN\"}, \"delivery_calendar_id\": {\"value\": \"cal_delivery_team\"}}}",
            ["calendar.create_event"],
        )

        self.assertEqual(visible, "已创建会议。")
        self.assertEqual(calls[0]["tool"], "calendar.create_event")
        self.assertEqual(events, [{"event": "asked_for_calendar_id"}])
        self.assertEqual(actions, [{"op": "write_file", "path": "/workspace/docs/a.txt", "content": "text"}])
        self.assertIn("default_output_language", state_update["facts"])
        self.assertIn("default_delivery_calendar_id", state_update["facts"])

    def test_hermes_l1_readonly_context_is_scoped_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "data").mkdir()
            (workspace / "state").mkdir()
            (workspace / "docs" / "source.txt").write_text("source material", encoding="utf-8")
            (workspace / "data" / "large.txt").write_text("a" * 20_000, encoding="utf-8")
            (workspace / "state" / "secret.txt").write_text("must not leak", encoding="utf-8")
            (workspace / "unrelated.txt").write_text("must not leak either", encoding="utf-8")

            context = _l1_readonly_workspace_context(workspace)

        self.assertIn("FILE /workspace/docs/source.txt (read-only):\nsource material", context)
        self.assertIn("FILE /workspace/data/large.txt (read-only):\n" + "a" * 16_000, context)
        self.assertNotIn("a" * 16_001, context)
        self.assertNotIn("must not leak", context)

    def test_hermes_marks_l1_turns_from_metadata_even_without_simulated_tools(self) -> None:
        self.assertTrue(_is_l1_metadata({"source_format": "l1_demo_task_json", "available_tools": []}))
        self.assertFalse(_is_l1_metadata({"available_tools": []}))

    def test_llm_judge_receives_prior_interactions_as_evidence(self) -> None:
        class CapturingClient:
            def __init__(self) -> None:
                self.messages: list[dict[str, str]] = []

            def complete(self, messages, **kwargs):
                self.messages = messages
                return '{"score": 1, "passed": true, "feedback": "ok"}'

        client = CapturingClient()
        sandbox = LocalSandbox()
        try:
            LLMJudge(client, "unused").evaluate(
                {"type": "llm_rubric", "rubric": "Judge {final_response}"},
                "created the event",
                sandbox,
                {"interactions": [{"index": 1, "response": "Which calendar ID should I use?", "events": [{"event": "asked_for_calendar_id"}]}]},
            )
        finally:
            sandbox.close()

        prompt = client.messages[1]["content"]
        self.assertIn("当前 Turn 已发生的 interaction 轨迹", prompt)
        self.assertIn("asked_for_calendar_id", prompt)

    def test_hermes_l1_state_context_preserves_prior_facts_and_history(self) -> None:
        state = _merge_l1_state(
            {"facts": {"default_delivery_calendar_id": {"value": "cal_delivery_team"}}},
            {"facts": {"default_output_language": {"value": "en-US"}}, "fact_history": [{"key": "default_output_language"}]},
        )

        context = _l1_persistent_state_context(state)

        self.assertIn("cal_delivery_team", context)
        self.assertIn("en-US", context)
        self.assertEqual(state["fact_history"], [{"key": "default_output_language"}])

    def test_hermes_l1_file_create_content_becomes_auditable_write_action(self) -> None:
        actions = _complete_l1_file_actions(
            [{"tool": "file.create_file", "arguments": {"filename": "/workspace/docs/a.txt", "content": "complete text"}}],
            [],
        )

        self.assertEqual(actions, [{"op": "write_file", "path": "/workspace/docs/a.txt", "content": "complete text"}])

    def test_process_runtime_rejects_source_revision_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec()
            runner = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root, source_revision="0" * 40)),
            )
            with self.assertRaisesRegex(RuntimeError, "revision mismatch"):
                runner.run(spec.flows[0])

    def test_process_runtime_rejects_dirty_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Tri-EvoBench Test"], check=True)
            (source / "tracked.txt").write_text("tracked", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "fixture"], check=True)
            revision = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (source / "untracked.txt").write_text("dirty", encoding="utf-8")
            spec = self._spec()
            runner = Runner(
                spec,
                output_dir=root / "runs",
                baseline="fake",
                baseline_manifest=str(self._manifest(root, source_revision=revision, source_path=source)),
            )

            with self.assertRaisesRegex(RuntimeError, "source tree is dirty"):
                runner.run(spec.flows[0])

    def test_baseline_selection_removes_control_fields_from_manifest_overrides(self) -> None:
        spec = BenchmarkSpec.from_dict({
            "schema_version": "0.1",
            "baseline": {
                "type": "fake",
                "manifest": "baseline.json",
                "options": {"from_spec": True},
            },
            "flows": [{
                "id": "selection-flow",
                "sandbox": {"backend": "local"},
                "turns": [{
                    "id": "selection-turn",
                    "request": "request",
                    "verifier": {"type": "file_contains", "path": "/workspace/x", "value": "x"},
                }],
            }],
        })

        baseline_id, manifest, overrides = resolve_baseline_selection(
            spec,
            baseline_override="fake",
            agent_override=None,
            config_override={"type": "ignored", "manifest": "ignored.json", "options": {"from_cli": True}},
            manifest_override=None,
        )

        self.assertEqual(baseline_id, "fake")
        self.assertEqual(manifest, "ignored.json")
        self.assertNotIn("type", overrides)
        self.assertNotIn("manifest", overrides)
        self.assertEqual(overrides["options"], {"from_spec": True, "from_cli": True})


if __name__ == "__main__":
    unittest.main()
