from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from trievo_bench.baselines.hermes_bridge import HermesBridge, _extract_l1_protocol
from trievo_bench.baselines.manifest import load_manifest
from trievo_bench.baselines.process import ProcessBaselineRuntime
from trievo_bench.model import TokenBudget
from trievo_bench.baselines.registry import resolve_baseline_selection
from trievo_bench.runner import Runner
from trievo_bench.spec import BenchmarkSpec


class BaselineRuntimeTest(unittest.TestCase):
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

    def test_hermes_l1_protocol_keeps_only_visible_text_and_normalizes_fact_aliases(self) -> None:
        visible, state_update, calls = _extract_l1_protocol(
            "已创建会议。\n"
            "TRIEVO_TOOL_CALL: {\"tool\": \"calendar.create_event\", \"arguments\": {\"calendar_id\": \"cal_delivery_team\"}}\n"
            "TRIEVO_STATE_UPDATE: {\"facts\": {\"default_work_language\": {\"value\": \"zh-CN\"}, \"delivery_calendar_id\": {\"value\": \"cal_delivery_team\"}}}",
            ["calendar.create_event"],
        )

        self.assertEqual(visible, "已创建会议。")
        self.assertEqual(calls[0]["tool"], "calendar.create_event")
        self.assertIn("default_output_language", state_update["facts"])
        self.assertIn("default_delivery_calendar_id", state_update["facts"])

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
