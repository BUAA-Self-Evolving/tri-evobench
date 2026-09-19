from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from trievo_bench.l2_compat import inspect_l2_package


MODULE_PATH = Path(__file__).parents[1] / "tools" / "l2_data_pipeline.py"
SPEC = importlib.util.spec_from_file_location("l2_data_pipeline", MODULE_PATH)
assert SPEC and SPEC.loader
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)


def test_parser_exposes_all_pipeline_phases() -> None:
    parser = pipeline.build_parser()
    for command in ("discover", "classify", "review", "qualify", "compose", "validate"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([command, "--help"])
        assert exc.value.code == 0


def test_compose_cli_routes_to_source_semantic_composer() -> None:
    parser = pipeline.build_parser()
    args = parser.parse_args([
        "compose", "--classified", "classified.jsonl",
        "--qualification", "qualification.jsonl", "--plan", "plan.json",
        "--output-package", "L2_Demo_V5",
    ])
    assert args.func is pipeline._cmd_compose_semantic


def test_generated_v5_judge_prompt_declares_strict_output_shape() -> None:
    evaluation = pipeline._evaluation(
        "stream-a",
        "turn1",
        {"source_binding": "submission"},
        {"name": "submission", "source": "interaction_record", "path": "final_response"},
    )
    prompt = evaluation["judge_config"]["judge_prompt"]

    assert "{stream_id}" in prompt
    assert "{native_verifier_result}" in prompt
    assert '"check_scores"' in prompt
    assert '"source_rubric_scores"' in prompt
    assert "Return JSON only" in prompt


def test_generated_v5_user_simulator_prompt_declares_schema() -> None:
    prompt = pipeline.l2_user_simulator_prompt()

    assert "{agent_input}" in prompt
    assert "{bounded_evaluation_feedback}" in prompt
    assert '"schema_version": "l2_user_simulator_output/1.0"' in prompt
    assert '"next_user_message"' in prompt
    assert '"rationale_category"' in prompt


def test_cache_key_binds_source_taxonomy_prompt_and_model() -> None:
    first = pipeline.classification_cache_key("a" * 64, "taxonomy/1", "prompt/1", "deepseek-chat")
    assert first == pipeline.classification_cache_key(
        "a" * 64, "taxonomy/1", "prompt/1", "deepseek-chat"
    )
    assert first != pipeline.classification_cache_key(
        "a" * 64, "taxonomy/2", "prompt/1", "deepseek-chat"
    )


def test_native_taxonomy_wins_without_llm_call() -> None:
    calls = []
    task = {
        "source": {"source_hash": "a" * 64},
        "benchmark_native_labels": {
            "domain_id": "software_engineering",
            "task_type_ids": ["code_repair"],
            "capability_id": "dependency_vulnerability_audit",
        },
    }
    result = pipeline.classify_task(
        task,
        codebook={"capabilities": {"dependency_vulnerability_audit": {
            "domain_id": "software_engineering",
            "task_type_ids": ["code_repair"],
        }}},
        llm_fill=lambda *_args, **_kwargs: calls.append(True),
    )
    assert result["normalized_taxonomy"]["label_source"] == "upstream_direct"
    assert calls == []


def test_unknown_native_label_enters_review_instead_of_becoming_new_codebook_label() -> None:
    task = {
        "source": {"source_hash": "a" * 64},
        "benchmark_native_labels": {"category": "new-unknown-area"},
    }
    result = pipeline.classify_task(task, codebook={"capabilities": {}}, llm_fill=None)
    assert result["normalized_taxonomy"]["needs_review"] is True
    assert result["normalized_taxonomy"]["capability_id"] is None


@pytest.mark.parametrize(
    ("instances", "turn_count"),
    [(4, 12), (5, 15), (6, 18)],
)
def test_role_schedule_is_round_robin_with_delayed_probes(instances: int, turn_count: int) -> None:
    schedule = pipeline.role_schedule(["a", "b", "c"], instances)
    assert len(schedule) == turn_count
    assert [row["capability_id"] for row in schedule[:3]] == ["a", "b", "c"]
    for capability in ("a", "b", "c"):
        roles = [row["sequence_role"] for row in schedule if row["capability_id"] == capability]
        assert roles[0] == "learning"
        assert roles[-1] == "probe"
        assert set(roles[1:-1]) == {"application"}
        probe = next(row for row in schedule if row["capability_id"] == capability and row["sequence_role"] == "probe")
        assert probe["interference_capability_ids"] == sorted({"a", "b", "c"} - {capability})
        assert probe["expected_transfer_positions"]


def test_role_schedule_rejects_out_of_contract_sizes() -> None:
    with pytest.raises(ValueError, match="three capabilities"):
        pipeline.role_schedule(["a", "b"], 4)
    with pytest.raises(ValueError, match="4..6"):
        pipeline.role_schedule(["a", "b", "c"], 7)


def test_metrics_are_split_by_label_source_and_include_macro_f1() -> None:
    predictions = [
        {"source_id": "1", "label_source": "upstream_direct", "domain_id": "a", "task_type_ids": ["x"], "capability_id": "c1"},
        {"source_id": "2", "label_source": "rule_normalized", "domain_id": "b", "task_type_ids": ["y"], "capability_id": "c2"},
        {"source_id": "3", "label_source": "llm_filled", "domain_id": "wrong", "task_type_ids": ["z"], "capability_id": "c3"},
    ]
    gold = [
        {"source_id": "1", "domain_id": "a", "task_type_ids": ["x"], "capability_id": "c1"},
        {"source_id": "2", "domain_id": "b", "task_type_ids": ["y"], "capability_id": "c2"},
        {"source_id": "3", "domain_id": "c", "task_type_ids": ["z"], "capability_id": "c4"},
    ]
    metrics = pipeline.classification_metrics(predictions, gold)
    assert set(metrics["by_label_source"]) == {"upstream_direct", "rule_normalized", "llm_filled"}
    assert metrics["by_label_source"]["upstream_direct"]["capability"]["accuracy"] == 1.0
    assert metrics["by_label_source"]["llm_filled"]["capability"]["macro_f1"] == 0.0


def test_metrics_report_needs_review_recall() -> None:
    predictions = [
        {"source_id": "accepted", "label_source": "upstream_direct", "needs_review": False},
        {"source_id": "caught", "label_source": "unclassified", "needs_review": True},
        {"source_id": "missed", "label_source": "llm_filled", "needs_review": False},
    ]
    gold = [
        {"source_id": "accepted", "needs_review": False},
        {"source_id": "caught", "needs_review": True},
        {"source_id": "missed", "needs_review": True},
    ]

    metrics = pipeline.classification_metrics(predictions, gold)

    assert metrics["needs_review"] == {
        "sample_count": 3,
        "positive_count": 2,
        "precision": 1.0,
        "recall": 0.5,
        "f1": 2 / 3,
    }


def test_jsonl_writer_is_resume_safe_and_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    pipeline.write_jsonl(path, [{"id": "b"}, {"id": "a"}], key="id")
    pipeline.write_jsonl(path, [{"id": "a", "updated": True}, {"id": "c"}], key="id")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"id": "a", "updated": True}, {"id": "b"}, {"id": "c"}]


def _composition_inputs(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    monkeypatch.setattr(pipeline, "REPO_ROOT", repo)
    classified: list[dict] = []
    qualified: list[dict] = []
    streams: list[dict] = []
    runtime_profiles = []
    benchmark_profiles = {
        "skilllearnbench": "skilllearn_task_v1",
        "skillsbench": "skillsbench_task_v1",
        "sr_agents": "sr_agents_python_v1",
        "tau2_bench": "tau2_tool_v1",
    }
    for profile_id in benchmark_profiles.values():
        runtime_profiles.append({
            "runtime_profile_id": profile_id,
            "task_sandbox_image_digest": "sha256:" + hashlib.sha256(profile_id.encode()).hexdigest(),
            "verifier_image_digest": "sha256:" + hashlib.sha256((profile_id + "-verifier").encode()).hexdigest(),
            "verifier_image_class": "python",
            "tool_profile_id": "terminal_filesystem_v1",
            "dependency_class": "source_native",
            "network": "disabled",
            "turn_timeout_seconds": 900,
            "learner_state_mount_profile": "baseline_explicit_v1",
            "learner_state_abi": "baseline_state/1.0",
            "serialization_version": "1",
            "mount_path": "/baseline_state",
            "owner": "baseline",
            "permission": "rw",
        })

    def add_stream(stream_id: str, pool_id: str, benchmarks: list[str]) -> None:
        capabilities = []
        for capability_index, benchmark in enumerate(benchmarks, start=1):
            capability_id = f"{stream_id}_cap_{capability_index}"
            source_ids = []
            for item_index in range(1, 6):
                source_task_id = f"{capability_id}_{item_index}"
                source_id = f"{benchmark}:{source_task_id}"
                source_ids.append(source_id)
                source_path = f"tasks/{source_task_id}"
                metadata: dict = {}
                if benchmark == "sr_agents":
                    source_path = f"data/bench/instances/bigcodebench.json#{source_task_id}"
                    metadata["native_source_item"] = {
                        "instance_id": source_task_id,
                        "question": f"Implement {source_task_id}",
                        "eval_data": {
                            "answer": "    return 1",
                            "code_prompt": "def task_func():\n",
                            "test": "import unittest\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(task_func(), 1)\n",
                            "entry_point": "task_func",
                        },
                    }
                elif benchmark == "tau2_bench":
                    source_path = f"data/tau2/domains/test/tasks.json#{source_task_id}"
                    metadata["task"] = {
                        "id": source_task_id,
                        "evaluation_criteria": {
                            "actions": [{"name": "resolve_case", "arguments": {"case_id": source_task_id}}]
                        },
                    }
                else:
                    upstream = repo / "third_party" / "src" / benchmark / source_path
                    native_dir = upstream / ("tests" if benchmark == "skilllearnbench" else "verifier")
                    native_dir.mkdir(parents=True, exist_ok=True)
                    (native_dir / "test.sh").write_text("#!/bin/sh\ntest -f result.json\n", encoding="utf-8")
                    (native_dir / "test_outputs.py").write_text(
                        "import os\nfrom pathlib import Path\n"
                        "def test_result(): assert (Path(os.environ['L2_AGENT_WORKSPACE'])/'result.json').is_file()\n",
                        encoding="utf-8",
                    )
                    oracle_dir = upstream / ("solution" if benchmark == "skilllearnbench" else "oracle")
                    oracle_dir.mkdir(parents=True, exist_ok=True)
                    (oracle_dir / "solve.sh").write_text("#!/bin/sh\nprintf '{}\\n' > result.json\n", encoding="utf-8")
                    environment = upstream / "environment"
                    environment.mkdir(exist_ok=True)
                    (environment / "fixture.txt").write_text(source_task_id, encoding="utf-8")
                classified.append({
                    "source_id": source_id,
                    "source": {
                        "benchmark_id": benchmark,
                        "benchmark_revision": hashlib.sha1(benchmark.encode()).hexdigest(),
                        "source_task_id": source_task_id,
                        "source_path": source_path,
                        "source_hash": hashlib.sha256(source_id.encode()).hexdigest(),
                    },
                    "prompt": f"Complete native task {source_task_id}",
                    "benchmark_native_labels": {"family": capability_id},
                    "normalized_taxonomy": {
                        "domain_id": f"domain_{capability_index}",
                        "task_type_ids": [f"task_type_{capability_index}"],
                        "capability_id": capability_id,
                        "label_source": "upstream_direct",
                        "confidence": 1.0,
                        "needs_review": False,
                    },
                    "runtime": {
                        "materializable": True,
                        "runtime_profile_id": benchmark_profiles[benchmark],
                        "verifier_kind": "native",
                        "external_data_status": "bundled",
                        "network": "disabled",
                    },
                    "source_metadata": metadata,
                })
                qualified.append({
                    "source_id": source_id,
                    "admitted": True,
                    "failure_reasons": [],
                    "runtime_profile_id": benchmark_profiles[benchmark],
                })
            capabilities.append({"capability_id": capability_id, "source_ids": source_ids})
        pool_type = "same_benchmark" if pool_id.startswith("intra_") else "cross_benchmark"
        streams.append({
            "stream_id": stream_id,
            "pool_id": pool_id,
            "pool_type": pool_type,
            "composition_policy": (
                "three_distinct_capabilities_one_benchmark"
                if pool_type == "same_benchmark"
                else "three_capabilities_three_benchmarks"
            ),
            "license_scope": "permissive",
            "relation_class": "native_family_sibling",
            "capabilities": capabilities,
        })

    intra_benchmarks = ["skilllearnbench", "skillsbench", "sr_agents", "tau2_bench", "sr_agents"]
    for index, benchmark in enumerate(intra_benchmarks, start=1):
        add_stream(
            f"intra_{index:02d}",
            "intra_benchmark_multi_capability",
            [benchmark, benchmark, benchmark],
        )
    for index in range(1, 6):
        add_stream(
            f"cross_{index:02d}",
            "cross_benchmark_multi_capability",
            ["skilllearnbench", "sr_agents", "tau2_bench"],
        )

    classified_path = tmp_path / "classified.jsonl"
    qualification_path = tmp_path / "qualification.jsonl"
    plan_path = tmp_path / "plan.json"
    pipeline.replace_jsonl(classified_path, classified, key="source_id")
    pipeline.replace_jsonl(qualification_path, qualified, key="source_id")
    plan_path.write_text(json.dumps({
        "schema_version": "l2_stream_plan/1.0",
        "package_id": "L2_Demo_V5",
        "taxonomy_version": pipeline.TAXONOMY_VERSION,
        "classifier_version": "l2-data-pipeline/1.0",
        "review_version": "adjudicated-gold/1.0",
        "selection_seed": 20260919,
        "runtime_profiles": runtime_profiles,
        "streams": streams,
    }), encoding="utf-8")
    return classified_path, qualification_path, plan_path


def test_compose_materializes_ten_fifteen_turn_v5_streams(tmp_path: Path, monkeypatch) -> None:
    classified, qualification, plan = _composition_inputs(tmp_path, monkeypatch)
    package = tmp_path / "L2_Demo_V5"

    result = pipeline.compose_package(classified, qualification, plan, package)

    assert result == {"stream_count": 10, "turn_count": 150, "unique_source_task_count": 150}
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "l2_taskstream/1.2"
    assert manifest["unique_source_task_count"] == manifest["turn_placement_count"] == 150
    assert "derivation" not in json.dumps(manifest)
    assert (package / "stream_plan.json").read_bytes() == plan.read_bytes()
    assert manifest["selection_manifest"] == {
        "schema_version": "l2_stream_plan/1.0",
        "path": "stream_plan.json",
        "sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
    }
    pools = json.loads((package / "pools.json").read_text(encoding="utf-8"))["pools"]
    assert [len(pool["stream_ids"]) for pool in pools] == [5, 5]
    for stream_entry in manifest["streams"]:
        stream_dir = package / stream_entry["path"].replace("/stream.json", "")
        stream = json.loads((stream_dir / "stream.json").read_text(encoding="utf-8"))
        assert stream["turn_count"] == 15
        assert len(stream["capabilities"]) == 3
        assert "selection_provenance" in stream
        probes = []
        for turn_id in stream["ordering"]["turn_order"]:
            task = json.loads((stream_dir / turn_id / "task.json").read_text(encoding="utf-8"))
            assert task["source_basis"]["source_hash"]
            assert "turn_lineage" not in task and "derivation" not in task
            if task["sequence_role"] == "probe":
                probes.append(task["probe_spec"])
        assert len(probes) == 3
        assert all(probe["expected_transfer_from_turn_ids"] for probe in probes)
        assert all(len(probe["interference_capability_ids"]) == 2 for probe in probes)

    sr_assets = package / "streams/intra_03/turn1/evaluation_assets"
    assert (sr_assets / "source_item.json").is_file()
    assert (sr_assets / "native_tests.py").is_file()
    tau_assets = package / "streams/intra_04/turn1/evaluation_assets"
    expected = json.loads((tau_assets / "expected_actions.json").read_text(encoding="utf-8"))
    assert expected["actions"][0]["name"] == "resolve_case"
    skill_assets = package / "streams/intra_01/turn1/evaluation_assets"
    assert (skill_assets / "native_test.sh").is_file()
    assert (skill_assets / "oracle_solution.sh").is_file()
    inventory = inspect_l2_package(package)
    assert len(inventory) == 10
    assert sum(item.episode_count for item in inventory) == 150


def test_compose_fails_closed_on_missing_native_oracle(tmp_path: Path, monkeypatch) -> None:
    classified, qualification, plan = _composition_inputs(tmp_path, monkeypatch)
    rows = pipeline.read_jsonl(classified)
    sr = next(row for row in rows if row["source"]["benchmark_id"] == "sr_agents")
    sr["source_metadata"]["native_source_item"]["eval_data"].pop("test")
    pipeline.replace_jsonl(classified, rows, key="source_id")

    with pytest.raises(ValueError, match="native oracle|native test"):
        pipeline.compose_package(classified, qualification, plan, tmp_path / "package")


def test_composed_carriers_accept_native_answer_and_reject_broken(tmp_path: Path, monkeypatch) -> None:
    classified, qualification, plan = _composition_inputs(tmp_path, monkeypatch)
    package = tmp_path / "L2_Demo_V5"
    pipeline.compose_package(classified, qualification, plan, package)

    def run(turn: Path, binding: dict, *, env: dict[str, str] | None = None) -> dict:
        request = {
            "schema_version": "l2_native_verifier_request/1.0",
            "bindings": {"submission": binding},
        }
        completed = subprocess.run(
            [sys.executable, str(turn / "evaluation_assets/verifier_cli.py")],
            input=json.dumps(request), text=True, capture_output=True, check=True, env=env,
        )
        return json.loads(completed.stdout)

    sr_turn = package / "streams/intra_03/turn1"
    correct = (sr_turn / "evaluation_assets/known_correct_response.py").read_text(encoding="utf-8")
    broken = (sr_turn / "evaluation_assets/known_broken_response.py").read_text(encoding="utf-8")
    assert run(sr_turn, {"kind": "inline", "value": correct})["status"] == "pass"
    assert run(sr_turn, {"kind": "inline", "value": broken})["status"] == "fail"

    tau_turn = package / "streams/intra_04/turn1"
    tau_correct = (tau_turn / "evaluation_assets/known_correct_response.json").read_text(encoding="utf-8")
    tau_broken = (tau_turn / "evaluation_assets/known_broken_response.json").read_text(encoding="utf-8")
    assert run(tau_turn, {"kind": "inline", "value": tau_correct})["status"] == "pass"
    assert run(tau_turn, {"kind": "inline", "value": tau_broken})["status"] == "fail"

    skill_turn = package / "streams/intra_01/turn1"
    workspace = tmp_path / "native-workspace"
    workspace.mkdir()
    subprocess.run(
        ["bash", str(skill_turn / "evaluation_assets/oracle_solution.sh")],
        cwd=workspace, check=True,
    )
    native_env = dict(
        os.environ,
        L2_VERIFIER_WORKSPACE=str(workspace),
        L2_VERIFIER_COMPAT_ROOT=str(tmp_path / "compat-root"),
    )
    assert run(skill_turn, {"kind": "path", "path": "agent_workspace"}, env=native_env)["status"] == "pass"
    (workspace / "result.json").unlink()
    assert run(skill_turn, {"kind": "path", "path": "agent_workspace"}, env=native_env)["status"] == "fail"


def test_compose_rejects_reused_source_identity_before_writing(tmp_path: Path, monkeypatch) -> None:
    classified, qualification, plan = _composition_inputs(tmp_path, monkeypatch)
    value = json.loads(plan.read_text(encoding="utf-8"))
    value["streams"][1]["capabilities"][0]["source_ids"][0] = (
        value["streams"][0]["capabilities"][0]["source_ids"][0]
    )
    plan.write_text(json.dumps(value), encoding="utf-8")
    output = tmp_path / "package"

    with pytest.raises(ValueError, match="duplicate source identity|taxonomy"):
        pipeline.compose_package(classified, qualification, plan, output)
    assert not output.exists()


def test_compose_requires_five_streams_per_pool(tmp_path: Path, monkeypatch) -> None:
    classified, qualification, plan = _composition_inputs(tmp_path, monkeypatch)
    value = json.loads(plan.read_text(encoding="utf-8"))
    value["streams"].pop()
    plan.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly 5 streams"):
        pipeline.compose_package(classified, qualification, plan, tmp_path / "package")
