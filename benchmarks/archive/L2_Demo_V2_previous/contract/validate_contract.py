#!/usr/bin/env python3
"""Validate L2 Contract V1 schemas, prompts, and representative objects."""

from __future__ import annotations

import json
import math
import re
import sys
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "schemas"
PROMPT_DIR = ROOT / "prompts"
EXAMPLE_DIR = ROOT / "examples"
sys.path.insert(0, str(ROOT))
from judge_reference import (
    aggregate_judge_score,
    load_routed_rubrics,
    render_judge_prompt,
    validate_evaluation_profile,
    validate_judge_output,
    validate_native_metric_result,
)
from evolution_reference import aggregate_reference_runs


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def schema(name: str) -> dict:
    value = load_json(SCHEMA_DIR / name)
    assert isinstance(value, dict)
    Draft202012Validator.check_schema(value)
    return value


def validate(instance: object, schema_name: str) -> None:
    Draft202012Validator(schema(schema_name)).validate(instance)


def expect_schema_invalid(instance: object, schema_name: str) -> None:
    try:
        validate(instance, schema_name)
    except ValidationError:
        return
    raise AssertionError(f"expected {schema_name} validation to fail")


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{([a-z][a-z0-9_]*)\}", text))


judge_prompt = (PROMPT_DIR / "l2_judge_prompt.md").read_text(encoding="utf-8")
judge_prompt_v2 = (PROMPT_DIR / "l2_judge_prompt_v2.md").read_text(encoding="utf-8")
auto_research_rubric = (PROMPT_DIR / "rubrics" / "auto_research_v1.md").read_text(encoding="utf-8")
simulator_prompt = (PROMPT_DIR / "l2_user_simulator_prompt.md").read_text(encoding="utf-8")

expected_judge_placeholders = {
    "stream_id", "turn_id", "turn_type", "turn_origin", "interaction_index",
    "agent_input", "final_response", "interaction_transcript", "tool_calls",
    "tool_results", "state_before", "state_after", "state_diff",
    "workspace_before", "workspace_after", "workspace_diff",
    "workspace_transition_record", "native_verifier_result", "execution_error",
    "resource_usage", "reference_context", "rubrics", "checks",
    "expected_state", "expected_tool_call", "expected_workspace_effect", "gt",
}
expected_simulator_placeholders = {
    "agent_input", "interaction_transcript", "latest_agent_response",
    "bounded_evaluation_feedback",
}
expected_judge_v2_placeholders = expected_judge_placeholders | {
    "evaluation_profile", "metric_profile", "domain_rubric", "benchmark_rubric",
}
assert placeholders(judge_prompt) == expected_judge_placeholders
assert placeholders(judge_prompt_v2) == expected_judge_v2_placeholders
assert placeholders(simulator_prompt) == expected_simulator_placeholders

manifest = {
    "schema_version": "l2_taskstream/1.0",
    "benchmark": "l2-contract-example",
    "source_datasets": ["example-source"],
    "stream_count": 1,
    "total_turn_count": 1,
    "aggregation_profile": "l2_evolution_aggregation/1.0",
    "judge_prompt_version": "l2_judge/1.0",
    "user_simulator_prompt_version": "l2_user_simulator/1.0",
    "tools_version": "l2_tools/1.0",
    "judge_output_version": "l2_judge_output/1.0",
    "bounded_feedback_version": "l2_bounded_feedback/1.0",
    "native_verifier_request_version": "l2_native_verifier_request/1.0",
    "native_verifier_result_version": "l2_native_verifier_result/1.0",
    "streams": [{
        "stream_id": "example_stream",
        "directory": "example_stream",
        "turn_count": 1,
        "path": "streams/example_stream/stream.json",
    }],
}

stream = {
    "schema_version": "l2_taskstream/1.0",
    "stream_id": "example_stream",
    "directory": "example_stream",
    "profile": "contract validation fixture",
    "theme": "persistent workspace task evolution",
    "turn_count": 1,
    "task_layout": "turn_directories",
    "agent_workspace": "agent_workspace",
    "turn_type_counts": {"reference": 1},
    "fact_thread_count": 0,
    "capability": {
        "capability_id": "structured_artifact_editing",
        "family": "workspace_tasks",
        "relation_type": "same_capability_different_input",
        "description": "The stream exercises one capability over related inputs.",
    },
    "ordering": {
        "method": "fixed",
        "seed": None,
        "turn_order": ["turn1"],
        "note": "Contract validation uses one fixed representative Turn.",
    },
    "execution_requirements": {
        "network": "disabled",
        "path_scope": "agent_workspace",
        "max_interactions": 3,
        "max_tool_calls_per_interaction": 20,
        "turn_timeout_seconds": 900,
        "max_output_tokens": 12000,
    },
    "turns": [{
        "turn_id": "turn1",
        "turn_index": 1,
        "turn_type": "reference",
        "turn_origin": "source_task",
        "sequence_role": "learning",
        "source_task_id": "example-001",
    }],
}

checks = [
    {
        "check_id": "task_completion",
        "dimension": "task_success",
        "weight": 0.6,
        "scoring": "ordinal",
        "instruction": "Use the native verifier result as authoritative evidence for objective completion.",
    },
    {
        "check_id": "side_effect_control",
        "dimension": "safety",
        "weight": 0.4,
        "scoring": "ordinal",
        "instruction": "No unrelated files or state were modified.",
    },
]

task = {
    "schema_version": "l2_taskstream/1.0",
    "stream_id": "example_stream",
    "turn_id": "turn1",
    "turn_index": 1,
    "turn_type": "reference",
    "turn_origin": "source_task",
    "sequence_role": "learning",
    "interaction_arm": "post_failure_user_simulation",
    "source_basis": {
        "source_dataset": "example-source",
        "source_task_id": "example-001",
        "parent_source_task_ids": [],
        "mapping_rule": "Preserve the source request and native verifier semantics.",
        "source_revision": "0123456789abcdef",
        "source_path": "upstream/tasks/example-001.json",
        "source_selector": {"id": "example-001"},
        "source_metadata": {},
    },
    "capability_relation": {
        "capability_id": "structured_artifact_editing",
        "relation_type": "foundation",
        "parent_turn_ids": [],
        "expected_transfer": "Establish the capability used by later turns.",
    },
    "agent_input": {
        "agent_workspace": "agent_workspace",
        "workspace_persistence": "stream",
        "messages": [{"role": "user", "content": "Create the requested submission artifact."}],
        "available_tools": ["shell"],
        "assets": [{
            "path": "agent_workspace/input.csv",
            "kind": "file",
            "mime_type": "text/csv",
            "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "description": "Agent-visible input data.",
        }],
    },
    "workspace_transition": {
        "mode": "continue_with_patch",
        "operations": [{"op": "copy", "source": "assets/input.csv", "target": "agent_workspace/input.csv"}],
        "preserve_unrelated_files": True,
    },
    "user_simulator": {
        "type": "llm_post_failure",
        "trigger": "interaction_unsuccessful_and_budget_remaining",
        "prompt_version": "l2_user_simulator/1.0",
        "simulator_prompt": simulator_prompt,
        "inputs": ["agent_input", "interaction_transcript", "latest_agent_response", "bounded_evaluation_feedback"],
        "temperature": 0,
        "output_schema": "l2_user_simulator_output/1.0",
        "max_generated_messages": 2,
    },
    "evaluation": {
        "type": "llm_as_judge",
        "protocol": "l2_native_evidence_llm_judge_v1",
        "checks": checks,
        "rubrics": {
            "source_rubrics": [],
            "behavioral_rubrics": [{"rubric_id": "side_effect_control", "instruction": "Preserve unrelated files."}],
        },
        "gt": {
            "gt_version": "1.0",
            "status": "complete",
            "answer_gt": None,
            "rubric_gt": {},
            "state_gt": {},
            "tool_gt": {},
            "workspace_gt": {"required_files": ["agent_workspace/submission.csv"]},
            "negative_constraints": ["Do not modify unrelated files."],
            "notes": None,
        },
        "expected_state": {},
        "expected_tool_call": {},
        "expected_workspace_effect": {"required_files": ["agent_workspace/submission.csv"]},
        "reference_context": {},
        "submission_contract": {
            "mode": "workspace_artifact",
            "payload_kind": "file",
            "source_binding": "submission",
            "adapter": {
                "owner": "native_verifier",
                "type": "identity",
                "entrypoint": None,
                "version": "identity_workspace_artifact/1.0",
            },
            "audit": {
                "retain_raw_submission": True,
                "retain_adapted_submission": False,
                "hash_algorithm": "sha256",
            },
            "failure_semantics": {
                "empty_or_invalid_submission": "task_failure",
                "adapter_error": "verifier_error",
            },
        },
        "native_verifier": {
            "required": True,
            "verifier_id": "example_native_verifier",
            "entrypoint": "evaluation_assets/verifier.py",
            "input_bindings": [
                {"name": "submission", "source": "workspace", "path": "agent_workspace/submission.csv"},
                {"name": "ground_truth", "source": "evaluation_assets", "path": "evaluation_assets/ground_truth.csv"},
            ],
            "request_schema": "l2_native_verifier_request/1.0",
            "result_schema": "l2_native_verifier_result/1.0",
            "timeout_seconds": 300,
            "network": "disabled",
            "authority": "objective_evidence",
        },
        "judge_config": {
            "prompt_version": "l2_judge/1.0",
            "judge_prompt": judge_prompt,
            "model_role": "reference_judge",
            "temperature": 0,
            "inputs": sorted(expected_judge_placeholders),
            "output_format": "json",
            "output_schema": "l2_judge_output/1.0",
            "aggregation": "framework_weighted_non_null_mean",
            "failure_policy": "one_repair_then_turn_unsuccessful",
        },
        "success_policy": {
            "native_verifier_requirement": "valid_result_before_judge",
            "turn_score_threshold": 0.8,
            "judge_failure_blocks_success": True,
            "interaction_selection": "latest_only",
        },
        "feedback_policy": "bounded_public_failure_feedback_only",
        "feedback_projection": {
            "include": ["failed_dimensions", "observable_deficiency", "public_result_category"],
            "exclude": ["gt", "private_diagnostics", "evaluation_assets", "future_turns", "exact_fix_steps"],
            "output_schema": "l2_bounded_feedback/1.0",
        },
    },
}

validate(manifest, "l2-manifest.schema.json")
mixed_prompt_manifest = deepcopy(manifest)
del mixed_prompt_manifest["judge_prompt_version"]
mixed_prompt_manifest["judge_prompt_versions"] = ["l2_judge/1.0", "l2_judge/2.0"]
validate(mixed_prompt_manifest, "l2-manifest.schema.json")

ambiguous_prompt_manifest = deepcopy(manifest)
ambiguous_prompt_manifest["judge_prompt_versions"] = ["l2_judge/1.0"]
expect_schema_invalid(ambiguous_prompt_manifest, "l2-manifest.schema.json")
validate(
    {
        "schema_version": "l2_tools/1.0",
        "tools": {"shell": {"description": "Execute an allowed command.", "arguments": {}}},
    },
    "l2-tools.schema.json",
)
validate(stream, "l2-stream.schema.json")
validate(task, "l2-task.schema.json")
validate_evaluation_profile(task)

general_v2_task = deepcopy(task)
general_v2_task["evaluation"].update({
    "evaluation_profile": "general",
    "metric_profile": {
        "profile_id": "invalid_general_metric_v1",
        "metric_name": "accuracy",
        "raw_direction": "maximize",
        "raw_metric_required": True,
        "judge_value_source": "native_verifier_result.score",
        "judge_value_semantics": "normalized_higher_is_better_0_1",
        "performance_bands": {
            "partial_min_inclusive": 0.65,
            "full_min_inclusive": 0.8,
        },
    },
})
general_v2_task["evaluation"]["judge_config"].update({
    "prompt_version": "l2_judge/2.0",
    "judge_prompt": judge_prompt_v2,
    "inputs": sorted(expected_judge_v2_placeholders),
    "rubric_route": {
        "composition": "common_then_domain_then_benchmark",
        "domain_rubric": {
            "rubric_id": "general",
            "version": "1.0",
            "path": "prompts/rubrics/auto_research_v1.md",
        },
        "benchmark_rubric": None,
    },
})
expect_schema_invalid(general_v2_task, "l2-task.schema.json")
try:
    validate_evaluation_profile(general_v2_task)
except ValueError:
    pass
else:
    raise AssertionError("expected Contract V1 General + Judge V2 route to fail")

auto_research_task = deepcopy(task)
auto_research_task["evaluation"].update({
    "evaluation_profile": "auto_research",
    "metric_profile": {
        "profile_id": "example_accuracy_v1",
        "metric_name": "accuracy",
        "raw_direction": "maximize",
        "raw_metric_required": True,
        "judge_value_source": "native_verifier_result.score",
        "judge_value_semantics": "normalized_higher_is_better_0_1",
        "performance_bands": {
            "partial_min_inclusive": 0.65,
            "full_min_inclusive": 0.8,
        },
    },
    "checks": [{
        "check_id": "auto_research_overall",
        "dimension": "task_success",
        "weight": 1.0,
        "scoring": "ordinal",
        "instruction": "Apply the routed AutoResearch rubric to all declared evidence.",
    }],
})
auto_research_task["evaluation"]["judge_config"].update({
    "prompt_version": "l2_judge/2.0",
    "judge_prompt": judge_prompt_v2,
    "inputs": sorted(expected_judge_v2_placeholders),
    "rubric_route": {
        "composition": "common_then_domain_then_benchmark",
        "domain_rubric": {
            "rubric_id": "auto_research",
            "version": "1.0",
            "path": "prompts/rubrics/auto_research_v1.md",
        },
        "benchmark_rubric": None,
    },
})
auto_research_task["evaluation"]["success_policy"].update({
    "interaction_continuation": "invalid_or_unverifiable_submission_only",
    "low_metric_is_retryable": False,
})
auto_research_task["user_simulator"]["trigger"] = (
    "submission_invalid_or_unverifiable_and_budget_remaining"
)
validate(auto_research_task, "l2-task.schema.json")
validate_evaluation_profile(auto_research_task)

missing_metric_profile = deepcopy(auto_research_task)
del missing_metric_profile["evaluation"]["metric_profile"]
expect_schema_invalid(missing_metric_profile, "l2-task.schema.json")

invalid_retry_policy = deepcopy(auto_research_task)
invalid_retry_policy["evaluation"]["success_policy"]["low_metric_is_retryable"] = True
expect_schema_invalid(invalid_retry_policy, "l2-task.schema.json")

invalid_bands = deepcopy(auto_research_task)
invalid_bands["evaluation"]["metric_profile"]["performance_bands"] = {
    "partial_min_inclusive": 0.8,
    "full_min_inclusive": 0.65,
}
try:
    validate_evaluation_profile(invalid_bands)
except ValueError:
    pass
else:
    raise AssertionError("expected inverted AutoResearch performance bands to fail")
validate(
    {
        "schema_version": "l2_native_verifier_request/1.0",
        "bindings": {"submission": {"kind": "inline", "value": "candidate output"}},
    },
    "l2-native-verifier-request.schema.json",
)
validate(load_json(EXAMPLE_DIR / "judge-output.example.json"), "l2-judge-output.schema.json")
validate(load_json(EXAMPLE_DIR / "bounded-feedback.example.json"), "l2-bounded-feedback.schema.json")
validate(load_json(EXAMPLE_DIR / "native-verifier-result.example.json"), "l2-native-verifier-result.schema.json")
auto_research_native_result = {
    "schema_version": "l2_native_verifier_result/1.0",
    "verifier_id": "example_native_verifier",
    "status": "partial",
    "score": 0.72,
    "metric": {
        "name": "accuracy",
        "raw_value": 0.72,
        "raw_direction": "maximize",
        "normalized_value": 0.72,
    },
    "checks": [],
    "artifacts": [],
    "public_summary": {"result_category": "partial", "observable_deficiencies": []},
    "private_diagnostics": {},
    "error": None,
}
validate(auto_research_native_result, "l2-native-verifier-result.schema.json")
validate_native_metric_result(auto_research_task, auto_research_native_result)

inconsistent_native_metric = deepcopy(auto_research_native_result)
inconsistent_native_metric["metric"]["normalized_value"] = 0.71
try:
    validate_native_metric_result(auto_research_task, inconsistent_native_metric)
except ValueError:
    pass
else:
    raise AssertionError("expected inconsistent AutoResearch native metric to fail")
validate(load_json(EXAMPLE_DIR / "user-simulator-output.example.json"), "l2-user-simulator-output.schema.json")

judge_context = {name: {"field": name, "literal": "{must_not_expand}"} for name in expected_judge_placeholders}
rendered = render_judge_prompt(judge_prompt, sorted(expected_judge_placeholders), judge_context)
assert "{must_not_expand}" in rendered
judge_v2_context = {
    name: {"field": name, "literal": "{must_not_expand}"}
    for name in expected_judge_v2_placeholders
}
loaded_domain_rubric, loaded_benchmark_rubric = load_routed_rubrics(auto_research_task)
judge_v2_context.update({
    "evaluation_profile": "auto_research",
    "metric_profile": auto_research_task["evaluation"]["metric_profile"],
    "domain_rubric": loaded_domain_rubric,
    "benchmark_rubric": loaded_benchmark_rubric,
})
rendered_v2 = render_judge_prompt(
    judge_prompt_v2,
    sorted(expected_judge_v2_placeholders),
    judge_v2_context,
)
assert "[DOMAIN RUBRIC: AUTO_RESEARCH]" in rendered_v2
assert loaded_domain_rubric == auto_research_rubric
assert "{must_not_expand}" in rendered_v2
judge_output = {
    "stream_id": "example_stream",
    "turn_id": "turn1",
    "interaction_index": 1,
    "check_scores": [
        {"check_id": "task_completion", "score": 1, "reason": "native pass", "evidence_refs": ["native_verifier_result"]},
        {"check_id": "side_effect_control", "score": 0.5, "reason": "minor unrelated write", "evidence_refs": ["workspace_diff"]},
    ],
    "source_rubric_scores": [],
    "rationale": "The required objective passed with a minor side effect.",
}
validate_judge_output(task, 1, judge_output)
assert math.isclose(aggregate_judge_score(task, judge_output), 0.8, abs_tol=1e-12)

auto_research_output = {
    "stream_id": "example_stream",
    "turn_id": "turn1",
    "interaction_index": 1,
    "check_scores": [{
        "check_id": "auto_research_overall",
        "score": 0.5,
        "reason": "The valid artifact reached the partial native performance band.",
        "evidence_refs": ["native_verifier_result.metric", "native_verifier_result.artifacts"],
    }],
    "source_rubric_scores": [],
    "rationale": "The submission is valid but does not reach the full performance band.",
}
validate_judge_output(auto_research_task, 1, auto_research_output)
assert aggregate_judge_score(auto_research_task, auto_research_output) == 0.5

assert manifest["stream_count"] == len(manifest["streams"])
assert manifest["total_turn_count"] == sum(item["turn_count"] for item in manifest["streams"])
assert stream["turn_count"] == len(stream["turns"])
assert stream["ordering"]["turn_order"] == [item["turn_id"] for item in stream["turns"]]
assert math.isclose(sum(item["weight"] for item in checks), 1.0, abs_tol=1e-12)
assert all(item["scoring"] in {"ordinal", "mean_source_rubrics"} for item in checks)
assert task["evaluation"]["native_verifier"]["required"] is True
assert task["evaluation"]["submission_contract"]["source_binding"] == "submission"
assert task["user_simulator"]["trigger"] == "interaction_unsuccessful_and_budget_remaining"

evolution_records = []
for condition, offset in (("reset", 0.0), ("frozen", 0.1), ("evolving", 0.2)):
    for index in range(1, 21):
        evolution_records.append({
            "condition": condition,
            "stream_id": "example_stream",
            "turn_id": f"turn{index}",
            "turn_index": index,
            "sequence_role": "learning" if index == 1 else "probe" if index % 10 == 0 else "application",
            "turn_score": 0.4 + offset,
        })
aggregates = aggregate_reference_runs(evolution_records)
assert aggregates["aggregation_profile"] == "l2_evolution_aggregation/1.0"
assert len(
    aggregates["conditions"]["evolving"]["per_stream"]["example_stream"]["learning_curve_bins"]
) == 2
assert math.isclose(
    aggregates["gains"]["evolution_gain_vs_reset"]["turn_micro_gain"],
    0.2,
    abs_tol=1e-12,
)
assert math.isclose(
    aggregates["gains"]["evolution_gain_vs_frozen"]["turn_micro_gain"],
    0.1,
    abs_tol=1e-12,
)

reset_evolving_only = [
    record for record in evolution_records if record["condition"] != "frozen"
]
try:
    aggregate_reference_runs(reset_evolving_only)
except ValueError:
    pass
else:
    raise AssertionError("expected missing frozen condition to block formal aggregation")

invalid_role = deepcopy(task)
invalid_role["sequence_role"] = "forgetting"
expect_schema_invalid(invalid_role, "l2-task.schema.json")

unmatched_records = deepcopy(evolution_records)
unmatched_records[-1]["turn_id"] = "turn999"
try:
    aggregate_reference_runs(unmatched_records)
except ValueError:
    pass
else:
    raise AssertionError("expected unmatched evolution runs to fail")

role_mismatched_records = deepcopy(evolution_records)
role_mismatched_records[-1]["sequence_role"] = "application"
try:
    aggregate_reference_runs(role_mismatched_records)
except ValueError:
    pass
else:
    raise AssertionError("expected condition-specific sequence_role drift to fail")

null_score_records = deepcopy(evolution_records)
null_score_records[-1]["turn_score"] = None
try:
    aggregate_reference_runs(null_score_records)
except ValueError:
    pass
else:
    raise AssertionError("expected null terminal score to block formal aggregation")

print("PASS: 9 schemas, 3 canonical prompts, scoring/evolution fixtures, negative routing/matching tests, and cross-object invariants")
