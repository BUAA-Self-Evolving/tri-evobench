import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from l2_runtime.api import ModelResponse
from l2_runtime.runner import StreamRunner, _materialize_agent_view
from l2_runtime.sra_bigcodebench import VerifyResult


REPO_ROOT = Path(__file__).resolve().parents[3]
STREAM_ROOT = (
    REPO_ROOT
    / "l2_demo"
    / "streams"
    / "sra_bigcodebench_pandas_dataframe_viz_001"
)


@dataclass
class FakeModel:
    agent_calls: int = 0
    learner_calls: int = 0
    e002_saw_memory: bool = False

    def generate(self, messages, *, purpose, episode_id, attempt):
        if purpose == "learner":
            self.learner_calls += 1
            return ModelResponse(
                text=f"Reusable lesson learned after {episode_id}.",
                model="fake-model",
                usage={"total_tokens": 1},
                request_id=f"learner-{episode_id}",
            )

        self.agent_calls += 1
        joined = "\n".join(message["content"] for message in messages)
        if episode_id == "E002" and "Reusable lesson learned after E001" in joined:
            self.e002_saw_memory = True

        assets = STREAM_ROOT / "episodes" / episode_id / "evaluation_assets"
        if episode_id == "E001" and attempt == 1:
            text = "def task_func(*args, **kwargs):\n    return None\n"
        else:
            text = (assets / "code_prompt.txt").read_text() + (
                assets / "reference_answer.txt"
            ).read_text()
        return ModelResponse(
            text=text,
            model="fake-model",
            usage={"total_tokens": 1},
            request_id=f"agent-{episode_id}-{attempt}",
        )


class FailingLearnerModel(FakeModel):
    def generate(self, messages, *, purpose, episode_id, attempt):
        if purpose == "learner":
            raise RuntimeError("simulated learner outage")
        return super().generate(
            messages, purpose=purpose, episode_id=episode_id, attempt=attempt
        )


class AlwaysFailVerifier:
    def verify(self, raw_model_output, episode_dir):
        return VerifyResult(category="fail", score=0.0, details={"forced": True})


class DegradingFailVerifier:
    def __init__(self):
        self.scores = iter([0.5, 0.25, 0.0])

    def verify(self, raw_model_output, episode_dir):
        score = next(self.scores)
        return VerifyResult(category="fail", score=score, details={"forced": True})


class TimeoutThenPassVerifier:
    def __init__(self):
        self.calls = 0

    def verify(self, raw_model_output, episode_dir):
        self.calls += 1
        if self.calls == 1:
            return VerifyResult(category="timeout", score=0.0, details={"forced": True})
        return VerifyResult(category="pass", score=1.0, details={"forced": True})


class WorkspacePatchVerifier:
    def verify(self, raw_model_output, episode_dir, *, workspace):
        expected = episode_dir.name
        marker = (workspace / "marker.txt").read_text()
        score = 1.0 if marker == expected else 0.0
        return VerifyResult(
            category="pass" if score == 1.0 else "fail",
            score=score,
            details={"marker": marker, "expected": expected},
        )


class StreamRunnerTests(unittest.TestCase):
    def test_agent_visible_tools_and_assets_are_inlined(self):
        episode_path = (
            REPO_ROOT
            / "l2_demo/streams/tau2_telecom_mobile_data_roaming_001/episodes/E001/episode.json"
        )
        episode = json.loads(episode_path.read_text())

        rendered = _materialize_agent_view(
            episode_path.parent, episode["agent_view"]
        )

        self.assertIn('"name": "toggle_roaming"', rendered)
        self.assertIn("Unavailable Mobile Data Troubleshooting", rendered)

    def test_workspace_data_is_declared_without_being_inlined(self):
        episode_path = (
            REPO_ROOT
            / "l2_demo/streams/airs_sick_textual_entailment_accuracy_001/episodes/E001/episode.json"
        )
        episode = json.loads(episode_path.read_text())

        rendered = _materialize_agent_view(
            episode_path.parent, episode["agent_view"]
        )

        self.assertIn('<WORKSPACE_ASSET path="data/train.csv" type="file" />', rendered)
        self.assertNotIn("A soccer game with multiple males playing", rendered)

    def test_request_file_is_inlined_for_artifact_handoff(self):
        episode_path = (
            REPO_ROOT
            / "l2_demo/streams/skilllearn_stock_data_visualization_001/episodes/E001/episode.json"
        )
        episode = json.loads(episode_path.read_text())

        rendered = _materialize_agent_view(
            episode_path.parent, episode["agent_view"]
        )

        self.assertIn("Please use D3.js (v6) to visualize input data", rendered)
        self.assertIn('<WORKSPACE_ASSET path="workspace/data/indiv-stock/" type="directory" />', rendered)

    def test_private_evaluation_assets_cannot_be_declared_agent_visible(self):
        episode_path = (
            REPO_ROOT
            / "l2_demo/streams/airs_sick_textual_entailment_accuracy_001/episodes/E001/episode.json"
        )
        episode = json.loads(episode_path.read_text())

        for private_request in (
            "evaluation_assets/test_labels.csv",
            "../E001/evaluation_assets/test_labels.csv",
        ):
            agent_view = dict(episode["agent_view"])
            agent_view["request_file"] = private_request
            with self.subTest(request_file=private_request), self.assertRaisesRegex(
                ValueError, "must be under assets"
            ):
                _materialize_agent_view(episode_path.parent, agent_view)

        agent_view = dict(episode["agent_view"])
        agent_view["assets"] = [
            "episodes/E001/evaluation_assets/test_labels.csv"
        ]
        with self.assertRaisesRegex(ValueError, "invalid agent-visible asset"):
            _materialize_agent_view(episode_path.parent, agent_view)

    def test_stream_runs_with_retry_memory_and_one_persistent_workspace(self):
        model = FakeModel()
        with tempfile.TemporaryDirectory() as output_root:
            result = StreamRunner(
                stream_dir=STREAM_ROOT,
                model=model,
                output_root=Path(output_root),
                allow_unsafe_execution=True,
            ).run()

            self.assertTrue(result["success"])
            self.assertEqual(result["episodes"][0]["interaction_count"], 2)
            self.assertEqual(result["episodes"][1]["interaction_count"], 1)
            self.assertEqual(result["episodes"][0]["latest_interaction"], 2)
            self.assertEqual(result["episodes"][0]["final_score"], 1.0)
            self.assertNotIn("best_interaction", result["episodes"][0])
            self.assertNotIn("best_score", result["episodes"][0])
            self.assertEqual(model.learner_calls, 2)
            self.assertTrue(model.e002_saw_memory)

            run_dir = Path(result["run_dir"])
            memory = (run_dir / "workspace/.l2_state/memory.md").read_text()
            self.assertIn("E001", memory)
            self.assertIn("E002", memory)
            protocol = json.loads(
                (run_dir / "workspace/.l2_state/protocol.json").read_text()
            )
            self.assertEqual(
                protocol["episode_result_policy"], "latest_interaction"
            )
            self.assertTrue(
                (run_dir / "benchmark_private/episodes/E001/interactions.json").is_file()
            )
            self.assertTrue(
                (run_dir / "benchmark_private/episodes/E002/interactions.json").is_file()
            )
            self.assertFalse(
                (run_dir / "workspace/.l2_state/episodes/E001/interactions.json").exists()
            )

            response_path = (
                run_dir
                / "workspace/.l2_state/episodes/E001/interaction-1-response.json"
            )
            response = json.loads(response_path.read_text())
            self.assertEqual(response["schema_version"], "model-response-0.1")
            self.assertEqual(response["purpose"], "agent")
            self.assertEqual(response["role"], "assistant")
            self.assertEqual(response["episode_id"], "E001")
            self.assertEqual(response["interaction"], 1)
            self.assertIn("def task_func", response["content"])
            self.assertEqual(len(response["content_sha256"]), 64)
            self.assertFalse(response_path.with_suffix(".txt").exists())
            interactions = json.loads(
                (
                    run_dir
                    / "benchmark_private/episodes/E001/interactions.json"
                ).read_text()
            )
            self.assertEqual(
                interactions[0]["response_file"],
                ".l2_state/episodes/E001/interaction-1-response.json",
            )
            self.assertEqual(
                interactions[0]["response_sha256"], response["content_sha256"]
            )

            learner_path = (
                run_dir / "workspace/.l2_state/episodes/E001/learner-update.json"
            )
            learner = json.loads(learner_path.read_text())
            self.assertEqual(learner["purpose"], "learner")
            self.assertIsNone(learner["interaction"])
            self.assertEqual(learner["source_interaction"], 2)
            self.assertIn("Reusable lesson", learner["content"])
            self.assertFalse(learner_path.with_suffix(".md").exists())

            report = json.loads((run_dir / "report.json").read_text())
            self.assertNotIn("api_key", json.dumps(report).lower())
            self.assertEqual(report["stream_id"], result["stream_id"])
            self.assertIn("reasoning_effort", report)

    def test_continue_with_patch_updates_run_workspace_before_verification(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root_path = Path(temp_root)
            stream = temp_root_path / "stream"
            (stream / "workspace").mkdir(parents=True)
            (stream / "workspace/marker.txt").write_text("E001")
            for episode_id in ("E001", "E002"):
                episode_dir = stream / "episodes" / episode_id
                (episode_dir / "assets").mkdir(parents=True)
                (episode_dir / "evaluation_assets").mkdir()
                (episode_dir / "evaluation.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "demo-0.1",
                            "episode_id": episode_id,
                            "visibility": "benchmark_private",
                            "verifier": {
                                "entrypoint": "l2_runtime.sra_bigcodebench:SraBigCodeBenchVerifier"
                            },
                            "scoring": {
                                "success_threshold": 1.0,
                                "episode_score_policy": "latest_interaction",
                            },
                        }
                    )
                )
                transition = {"mode": "continue", "workspace_patch": []}
                if episode_id == "E002":
                    (episode_dir / "patch").mkdir()
                    (episode_dir / "patch/marker.txt").write_text("E002")
                    transition = {
                        "mode": "continue_with_patch",
                        "workspace_patch": [
                            {
                                "op": "copy",
                                "from": "episodes/E002/patch/marker.txt",
                                "to": "workspace/marker.txt",
                            }
                        ],
                    }
                (episode_dir / "episode.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "demo-0.1",
                            "episode_id": episode_id,
                            "stream_id": "patch_stream",
                            "source_provenance": {"instance_id": episode_id},
                            "agent_view": {
                                "request": f"Return {episode_id}",
                                "assets": [],
                                "available_tools": [],
                            },
                            "workspace_transition": transition,
                            "interaction_policy": {
                                "max_interactions": 1,
                                "stop_on_success": True,
                            },
                            "evaluation_ref": "evaluation.json",
                        }
                    )
                )
            (stream / "stream.json").write_text(
                json.dumps(
                    {
                        "schema_version": "demo-0.1",
                        "stream_id": "patch_stream",
                        "workspace": {"initial_snapshot": "workspace/"},
                        "ordering": {
                            "method": "fixed",
                            "seed": None,
                            "episode_order": ["E001", "E002"],
                        },
                        "episodes": [
                            "episodes/E001/episode.json",
                            "episodes/E002/episode.json",
                        ],
                    }
                )
            )

            runner = StreamRunner(
                stream_dir=stream,
                model=FakeModel(),
                output_root=temp_root_path / "runs",
                allow_unsafe_execution=True,
            )
            runner.verifier = WorkspacePatchVerifier()
            result = runner.run()

            self.assertTrue(result["success"])
            self.assertEqual(
                (Path(result["workspace"]) / "marker.txt").read_text(), "E002"
            )

    def test_latest_interaction_is_terminal_when_all_attempts_fail(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root_path = Path(temp_root)
            stream_copy = temp_root_path / "stream"
            shutil.copytree(STREAM_ROOT, stream_copy)
            manifest_path = stream_copy / "stream.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["ordering"]["episode_order"] = ["E001"]
            manifest["episodes"] = ["episodes/E001/episode.json"]
            manifest_path.write_text(json.dumps(manifest))

            runner = StreamRunner(
                stream_dir=stream_copy,
                model=FakeModel(),
                output_root=temp_root_path / "runs",
                allow_unsafe_execution=True,
            )
            runner.verifier = AlwaysFailVerifier()
            result = runner.run()

            episode_result = result["episodes"][0]
            self.assertFalse(result["success"])
            self.assertFalse(episode_result["passed"])
            self.assertEqual(episode_result["interaction_count"], 3)
            self.assertEqual(episode_result["latest_interaction"], 3)
            self.assertEqual(episode_result["final_score"], 0.0)

            run_dir = Path(result["run_dir"])
            learner = json.loads(
                (
                    run_dir
                    / "workspace/.l2_state/episodes/E001/learner-update.json"
                ).read_text()
            )
            self.assertEqual(learner["source_interaction"], 3)
            interactions = json.loads(
                (
                    run_dir
                    / "benchmark_private/episodes/E001/interactions.json"
                ).read_text()
            )
            self.assertEqual([item["attempt"] for item in interactions], [1, 2, 3])

    def test_timeout_gets_bounded_retry_and_can_recover(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root_path = Path(temp_root)
            stream_copy = temp_root_path / "stream"
            shutil.copytree(STREAM_ROOT, stream_copy)
            manifest_path = stream_copy / "stream.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["ordering"]["episode_order"] = ["E001"]
            manifest["episodes"] = ["episodes/E001/episode.json"]
            manifest_path.write_text(json.dumps(manifest))

            runner = StreamRunner(
                stream_dir=stream_copy,
                model=FakeModel(),
                output_root=temp_root_path / "runs",
                allow_unsafe_execution=True,
            )
            runner.verifier = TimeoutThenPassVerifier()
            result = runner.run()

            episode_result = result["episodes"][0]
            self.assertTrue(result["success"])
            self.assertEqual(episode_result["interaction_count"], 2)
            interactions = json.loads(
                (
                    Path(result["run_dir"])
                    / "benchmark_private/episodes/E001/interactions.json"
                ).read_text()
            )
            self.assertEqual([item["result"] for item in interactions], ["timeout", "pass"])
            self.assertIn("上一轮执行超时", interactions[1]["request"])

    def test_report_is_flushed_when_episode_errors_after_interactions(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root_path = Path(temp_root)
            stream_copy = temp_root_path / "stream"
            shutil.copytree(STREAM_ROOT, stream_copy)
            manifest_path = stream_copy / "stream.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["ordering"]["episode_order"] = ["E001"]
            manifest["episodes"] = ["episodes/E001/episode.json"]
            manifest_path.write_text(json.dumps(manifest))

            runner = StreamRunner(
                stream_dir=stream_copy,
                model=FailingLearnerModel(),
                output_root=temp_root_path / "runs",
                allow_unsafe_execution=True,
            )
            runner.verifier = TimeoutThenPassVerifier()

            with self.assertRaisesRegex(RuntimeError, "simulated learner outage"):
                runner.run()

            reports = list((temp_root_path / "runs").glob("*/**/report.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertFalse(report["success"])
            self.assertIn("RuntimeError: simulated learner outage", report["error"])
            self.assertEqual(report["episodes"][0]["episode_id"], "E001")
            self.assertEqual(report["episodes"][0]["status"], "error")
            self.assertIn(
                "RuntimeError: simulated learner outage",
                report["episodes"][0]["error"],
            )
            interactions = json.loads(
                (
                    reports[0].parent
                    / "benchmark_private/episodes/E001/interactions.json"
                ).read_text()
            )
            self.assertEqual([item["result"] for item in interactions], ["timeout", "pass"])

    def test_latest_interaction_is_used_even_when_an_earlier_score_was_higher(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root_path = Path(temp_root)
            stream_copy = temp_root_path / "stream"
            shutil.copytree(STREAM_ROOT, stream_copy)
            manifest_path = stream_copy / "stream.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["ordering"]["episode_order"] = ["E001"]
            manifest["episodes"] = ["episodes/E001/episode.json"]
            manifest_path.write_text(json.dumps(manifest))

            runner = StreamRunner(
                stream_dir=stream_copy,
                model=FakeModel(),
                output_root=temp_root_path / "runs",
                allow_unsafe_execution=True,
            )
            runner.verifier = DegradingFailVerifier()
            result = runner.run()

            episode_result = result["episodes"][0]
            self.assertEqual(episode_result["interaction_count"], 3)
            self.assertEqual(episode_result["latest_interaction"], 3)
            self.assertEqual(episode_result["final_score"], 0.0)

            interactions = json.loads(
                (
                    Path(result["run_dir"])
                    / "benchmark_private/episodes/E001/interactions.json"
                ).read_text()
            )
            self.assertEqual([item["score"] for item in interactions], [0.5, 0.25, 0.0])

    def test_stream_rejects_more_than_three_interactions(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root_path = Path(temp_root)
            stream_copy = temp_root_path / "stream"
            shutil.copytree(STREAM_ROOT, stream_copy)
            episode_path = stream_copy / "episodes/E001/episode.json"
            episode = json.loads(episode_path.read_text())
            episode["interaction_policy"]["max_interactions"] = 4
            episode_path.write_text(json.dumps(episode))

            with self.assertRaisesRegex(ValueError, "between 1 and 3"):
                StreamRunner(
                    stream_dir=stream_copy,
                    model=FakeModel(),
                    output_root=temp_root_path / "runs",
                    allow_unsafe_execution=True,
                ).run()
