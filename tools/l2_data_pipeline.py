#!/usr/bin/env python3
"""Build and audit Tri-EvoBench L2 heterogeneous TaskStreams.

The pipeline keeps source discovery, taxonomy normalization, optional LLM
fill, review, runtime qualification, composition, and validation as explicit
restartable stages. Third-party checkouts are read-only inputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_MANIFEST = REPO_ROOT / "third_party" / "manifest.tsv"
TAXONOMY_VERSION = "tri-evobench-l2-v5-codebook/1.0"
PROMPT_VERSION = "l2-taxonomy-fill/1.0"
LABEL_SOURCES = ("upstream_direct", "rule_normalized", "llm_filled")
COMPOSITION_SEED = 20260919
DEFAULT_LOCAL_L2_IMAGE_DIGEST = os.environ.get(
    "TRIEVO_L2_V5_IMAGE_DIGEST",
    "sha256:aef1eb316941f620ab1241b34b271edec73b83e1a8a743dd8595abe6697dc9fb",
)

GENERIC_V5_VERIFIER = """#!/usr/bin/env python3
raise SystemExit("generic V5 verification was removed; use the source-semantic composer")
"""

_FALLBACK_L2_JUDGE_PROMPT_V2 = """You are the reference evaluator for L2 TaskStream. Evaluate one completed Interaction strictly from supplied observable evidence.

[IDENTITY]
Stream: {stream_id}
Turn: {turn_id}
Turn type: {turn_type}
Turn origin: {turn_origin}
Interaction index: {interaction_index}

[EVALUATION PROFILE]
Profile: {evaluation_profile}
Metric profile: {metric_profile}

[ROUTED RUBRIC EXTENSIONS]
Domain rubric:
{domain_rubric}

Benchmark rubric:
{benchmark_rubric}

[AGENT-VISIBLE TASK]
{agent_input}

[EXECUTION EVIDENCE]
Final response:
{final_response}

Interaction transcript:
{interaction_transcript}

Tool calls:
{tool_calls}

Tool results:
{tool_results}

Workspace before:
{workspace_before}

Workspace after:
{workspace_after}

Workspace diff:
{workspace_diff}

Workspace transition record:
{workspace_transition_record}

Native verifier result:
{native_verifier_result}

Execution error:
{execution_error}

Resource usage:
{resource_usage}

[SEALED EVALUATION DATA]
Reference context:
{reference_context}

Source and behavioral rubrics:
{rubrics}

Evaluation checks:
{checks}

Expected state:
{expected_state}

Expected tool call:
{expected_tool_call}

Expected workspace effect:
{expected_workspace_effect}

Structured ground truth:
{gt}

[SCORING RULES]
1. Return exactly one check_scores item for every declared check, preserving check_id.
2. For scoring=ordinal, use only 1, 0.5, 0, or null.
3. Use null only when a check is logically inapplicable.
4. Do not output a weighted total or a pass/fail decision.

[OUTPUT]
Return JSON only, with no Markdown fence or surrounding text:
{
  "stream_id": "<exact stream_id>",
  "turn_id": "<exact turn_id>",
  "interaction_index": 1,
  "check_scores": [
    {
      "check_id": "<exact declared check_id>",
      "score": 1,
      "reason": "brief evidence-based justification",
      "evidence_refs": ["native_verifier_result"]
    }
  ],
  "source_rubric_scores": [],
  "rationale": "brief overall diagnosis"
}
"""


def l2_judge_prompt_v2() -> str:
    prompt = (
        REPO_ROOT
        / "benchmarks"
        / "current"
        / "L2_Demo_V5"
        / "contract"
        / "prompts"
        / "l2_judge_prompt_v2.md"
    )
    if prompt.is_file():
        return prompt.read_text(encoding="utf-8")
    return _FALLBACK_L2_JUDGE_PROMPT_V2


def l2_user_simulator_prompt() -> str:
    prompt = (
        REPO_ROOT
        / "benchmarks"
        / "current"
        / "L2_Demo_V5"
        / "contract"
        / "prompts"
        / "l2_user_simulator_prompt.md"
    )
    if prompt.is_file():
        return prompt.read_text(encoding="utf-8")
    return (
        "Ask for another attempt using bounded public feedback only.\n"
        "Return JSON only with schema_version, next_user_message, and rationale_category."
    )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def classification_cache_key(
    source_hash: str,
    taxonomy_version: str,
    prompt_version: str,
    model_id: str,
) -> str:
    return digest_json({
        "source_hash": source_hash,
        "taxonomy_version": taxonomy_version,
        "prompt_version": prompt_version,
        "model_id": model_id,
    })


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: JSONL row must be an object")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, key: str) -> None:
    merged = {
        str(row[key]): row
        for row in [*read_jsonl(path), *rows]
        if key in row
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(merged[item], ensure_ascii=False, sort_keys=True) + "\n"
        for item in sorted(merged)
    )
    path.write_text(content, encoding="utf-8")


def replace_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, key: str) -> None:
    values = list(rows)
    identities = [str(row[key]) for row in values]
    duplicates = sorted(item for item, count in Counter(identities).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate {key} values: {duplicates[:5]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in sorted(values, key=lambda item: str(item[key]))
        ),
        encoding="utf-8",
    )


def _git_checkout_state(path: Path) -> tuple[str, bool]:
    revision = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip())
    return revision, dirty


def _source_record(
    *,
    benchmark_id: str,
    revision: str,
    source_task_id: str,
    source_path: str,
    prompt: str,
    native_labels: dict[str, Any],
    runtime: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_hash = digest_json({
        "benchmark_id": benchmark_id,
        "revision": revision,
        "source_task_id": source_task_id,
        "source_path": source_path,
        "prompt": prompt,
        "native_labels": native_labels,
        "metadata": metadata or {},
    })
    return {
        "source_id": f"{benchmark_id}:{source_task_id}",
        "source": {
            "benchmark_id": benchmark_id,
            "benchmark_revision": revision,
            "source_task_id": source_task_id,
            "source_path": source_path,
            "source_hash": source_hash,
        },
        "prompt": prompt,
        "benchmark_native_labels": native_labels,
        "source_metadata": metadata or {},
        "runtime": runtime,
    }


def _discover_skilllearn(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    for task_path in sorted((root / "tasks").glob("*/*/task.toml")):
        instance = task_path.parent
        metadata = tomllib.loads(task_path.read_text(encoding="utf-8")).get("metadata", {})
        prompt_path = instance / "instruction.md"
        family = instance.parent.name
        yield _source_record(
            benchmark_id="skilllearnbench",
            revision=revision,
            source_task_id=instance.name,
            source_path=instance.relative_to(root).as_posix(),
            prompt=prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "",
            native_labels={
                "family": family,
                "category": metadata.get("category"),
                "tags": metadata.get("tags", []),
                "difficulty": metadata.get("difficulty"),
                "capability_id": family.replace("-", "_"),
            },
            runtime={
                "materializable": (instance / "tests" / "test.sh").is_file(),
                "runtime_profile_id": "skilllearn_task_v1",
                "verifier_kind": "native",
                "external_data_status": "bundled",
                "network": "disabled",
            },
        )


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    raw = text.split("---\n", 2)[1]
    try:
        import yaml  # type: ignore
        value = yaml.safe_load(raw)
        return value if isinstance(value, dict) else {}
    except ImportError:
        result: dict[str, Any] = {}
        current_list: str | None = None
        current_mapping: dict[str, Any] | None = None
        for line in raw.splitlines():
            if re.match(r"^[a-zA-Z_][\w-]*:", line):
                key, value = line.split(":", 1)
                current_list = key if not value.strip() else None
                if not value.strip():
                    current_mapping = {}
                    result[key] = current_mapping
                    current_list = None
                else:
                    current_mapping = None
                    result[key] = value.strip().strip("'\"")
            elif current_mapping is not None and re.match(r"^  [a-zA-Z_][\w-]*:", line):
                key, value = line.strip().split(":", 1)
                if value.strip():
                    current_mapping[key] = value.strip().strip("'\"")
                    current_list = None
                else:
                    current_mapping[key] = []
                    current_list = key
            elif current_mapping is not None and current_list and line.startswith("  -"):
                current_mapping[current_list].append(line.split("-", 1)[1].strip().strip("'\""))
            elif current_list and line.lstrip().startswith("-"):
                result[current_list].append(line.split("-", 1)[1].strip().strip("'\""))
        return result


def _discover_skillsbench(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    for task_md in sorted((root / "tasks").glob("*/task.md")):
        task_dir = task_md.parent
        labels = _frontmatter(task_md).get("metadata", {})
        text = task_md.read_text(encoding="utf-8")
        prompt = text.split("---\n", 2)[-1].strip()
        yield _source_record(
            benchmark_id="skillsbench",
            revision=revision,
            source_task_id=task_dir.name,
            source_path=task_dir.relative_to(root).as_posix(),
            prompt=prompt,
            native_labels=labels if isinstance(labels, dict) else {},
            runtime={
                "materializable": (task_dir / "verifier" / "test.sh").is_file(),
                "runtime_profile_id": "skillsbench_task_v1",
                "verifier_kind": "native",
                "external_data_status": "bundled",
                "network": "disabled",
            },
        )


def _discover_tau2(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    domain_root = root / "data" / "tau2" / "domains"
    for tasks_path in sorted(domain_root.glob("*/tasks.json")):
        domain = tasks_path.parent.name
        tasks = load_json(tasks_path)
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            instructions = task.get("user_scenario", {}).get("instructions", {})
            if isinstance(instructions, str):
                instruction_fields: dict[str, Any] = {"reason_for_call": instructions}
            elif isinstance(instructions, dict):
                instruction_fields = instructions
            else:
                instruction_fields = {}
            criteria = task.get("evaluation_criteria", {})
            if not isinstance(criteria, dict):
                criteria = {}
            actions = [
                item.get("name")
                for item in criteria.get("actions", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            task_id = str(task.get("id"))
            yield _source_record(
                benchmark_id="tau2_bench",
                revision=revision,
                source_task_id=f"{domain}:{task_id}",
                source_path=tasks_path.relative_to(root).as_posix() + f"#{task_id}",
                prompt=str(instruction_fields.get("reason_for_call") or ""),
                native_labels={
                    "domain": domain,
                    "purpose": task.get("description", {}).get("purpose"),
                    "reason_for_call": instruction_fields.get("reason_for_call"),
                    "actions": actions,
                    "documents": criteria.get("communicate_info", []),
                },
                runtime={
                    "materializable": True,
                    "runtime_profile_id": "tau2_tool_v1",
                    "verifier_kind": "native",
                    "external_data_status": "bundled",
                    "network": "disabled",
                },
                metadata={"task": task},
            )


def _discover_sr_agents(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    instances_root = root / "data" / "bench" / "instances"
    for data_path in sorted(instances_root.glob("*.json")):
        dataset = data_path.stem
        rows = load_json(data_path)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            task_id = str(row.get("instance_id"))
            eval_data = row.get("eval_data", {})
            yield _source_record(
                benchmark_id="sr_agents",
                revision=revision,
                source_task_id=task_id,
                source_path=data_path.relative_to(root).as_posix() + f"#{task_id}",
                prompt=str(row.get("question") or ""),
                native_labels={
                    "dataset": dataset,
                    "skill_annotations": row.get("skill_annotations", []),
                },
                runtime={
                    "materializable": dataset in {"bigcodebench", "logicbench", "theoremqa", "champ", "medcalcbench"},
                    "runtime_profile_id": "sr_agents_python_v1",
                    "verifier_kind": "native",
                    "external_data_status": "bundled" if dataset != "toolqa" else "missing",
                    "network": "disabled",
                },
                metadata={
                    "entry_point": eval_data.get("entry_point") if isinstance(eval_data, dict) else None,
                    "code_prompt": eval_data.get("code_prompt") if isinstance(eval_data, dict) else None,
                },
            )


def _discover_airs(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    for task_dir in sorted((root / "airsbench" / "tasks" / "rad").glob("*")):
        if task_dir.is_dir():
            yield _source_record(
                benchmark_id="airs_bench",
                revision=revision,
                source_task_id=task_dir.name,
                source_path=task_dir.relative_to(root).as_posix(),
                prompt=task_dir.name,
                native_labels={"task_name": task_dir.name},
                runtime={
                    "materializable": False,
                    "runtime_profile_id": "airs_research_v1",
                    "verifier_kind": "native",
                    "external_data_status": "missing",
                    "network": "disabled",
                },
            )


def _simple_yaml_fields(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-zA-Z_][\w-]*):\s*(.*)$", line)
        if match and match.group(2):
            result[match.group(1)] = match.group(2).strip().strip("'\"")
    return result


def _discover_mlgym(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    for config in sorted((root / "configs" / "tasks").glob("*.yaml")):
        fields = _simple_yaml_fields(config)
        upstream_id = str(fields.get("id") or config.stem)
        task_id = config.stem
        yield _source_record(
            benchmark_id="mlgym",
            revision=revision,
            source_task_id=task_id,
            source_path=config.relative_to(root).as_posix(),
            prompt=str(fields.get("description") or fields.get("name") or task_id),
            native_labels={"id": upstream_id, "name": fields.get("name"), "task_entrypoint": fields.get("task_entrypoint")},
            runtime={
                "materializable": False,
                "runtime_profile_id": "mlgym_research_v1",
                "verifier_kind": "native",
                "external_data_status": "requires_per_task_review",
                "network": "disabled",
            },
        )


def _discover_appworld(root: Path, revision: str) -> Iterator[dict[str, Any]]:
    data_dir = root / "data" / "tasks"
    if data_dir.is_dir():
        for task in sorted(data_dir.glob("*.json")):
            value = load_json(task)
            yield _source_record(
                benchmark_id="appworld",
                revision=revision,
                source_task_id=task.stem,
                source_path=task.relative_to(root).as_posix(),
                prompt=str(value.get("instruction") or ""),
                native_labels={key: value.get(key) for key in ("generator_id", "difficulty", "required_apps")},
                runtime={"materializable": True, "runtime_profile_id": "appworld_v1", "verifier_kind": "native", "external_data_status": "bundled", "network": "disabled"},
            )
    else:
        yield _source_record(
            benchmark_id="appworld",
            revision=revision,
            source_task_id="__inventory_only__",
            source_path="data/tasks",
            prompt="",
            native_labels={},
            runtime={"materializable": False, "runtime_profile_id": "appworld_v1", "verifier_kind": "native", "external_data_status": "missing_data_bundle", "network": "disabled"},
        )


ADAPTERS: dict[str, Callable[[Path, str], Iterator[dict[str, Any]]]] = {
    "skilllearnbench": _discover_skilllearn,
    "skillsbench": _discover_skillsbench,
    "tau2-bench": _discover_tau2,
    "sr-agents": _discover_sr_agents,
    "airs-bench": _discover_airs,
    "mlgym": _discover_mlgym,
    "appworld": _discover_appworld,
}


def discover(manifest_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    checkouts: list[dict[str, Any]] = []
    with manifest_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            name = row["name"]
            root = (REPO_ROOT / row["local_path"]).resolve()
            actual_revision, dirty = _git_checkout_state(root)
            if actual_revision != row["revision"]:
                raise ValueError(f"{name}: revision mismatch {actual_revision} != {row['revision']}")
            if dirty:
                raise ValueError(f"{name}: checkout is dirty")
            adapter = ADAPTERS.get(name)
            if adapter is None:
                raise ValueError(f"no discovery adapter for {name}")
            source_rows = list(adapter(root, actual_revision))
            records.extend(source_rows)
            checkouts.append({
                "benchmark_id": name.replace("-", "_"),
                "manifest_name": name,
                "revision": actual_revision,
                "clean": True,
                "task_count": len(source_rows),
                "license": row["license"],
                "notes": row["notes"],
            })
    return records, checkouts


def _alias_map(codebook: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for capability_id, spec in codebook.get("capabilities", {}).items():
        result[str(capability_id)] = str(capability_id)
        if isinstance(spec, dict):
            for alias in spec.get("aliases", []):
                result[str(alias)] = str(capability_id)
    return result


def classify_task(
    task: dict[str, Any],
    *,
    codebook: dict[str, Any],
    llm_fill: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any]:
    native = task.get("benchmark_native_labels", {})
    aliases = _alias_map(codebook)
    capabilities = codebook.get("capabilities", {})
    native_capability = native.get("capability_id") if isinstance(native, dict) else None
    capability_id = aliases.get(str(native_capability)) if native_capability else None
    label_source = "upstream_direct" if capability_id else "unclassified"
    rationale = "complete upstream taxonomy" if capability_id else "no controlled-vocabulary match"
    if capability_id is None and isinstance(native, dict):
        candidates = [
            f"{task.get('source', {}).get('benchmark_id')}:{task.get('source', {}).get('source_task_id')}",
            task.get("source", {}).get("source_task_id"),
            native.get("family"),
            native.get("subcategory"),
            native.get("category"),
            *(native.get("actions", [])[-1:] if isinstance(native.get("actions"), list) else []),
            *(native.get("skill_annotations", []) if isinstance(native.get("skill_annotations"), list) else []),
            native.get("dataset"),
        ]
        capability_id = next(
            (aliases[str(item)] for item in candidates if item is not None and str(item) in aliases),
            None,
        )
        if capability_id:
            label_source = "rule_normalized"
            rationale = "deterministic alias mapping from upstream taxonomy"
    llm_result: dict[str, Any] | None = None
    if capability_id is None and llm_fill is not None:
        llm_result = llm_fill(task, codebook)
        proposed = llm_result.get("capability_id") if isinstance(llm_result, dict) else None
        capability_id = str(proposed) if proposed in capabilities else None
        label_source = "llm_filled"
        rationale = str(llm_result.get("rationale", "LLM fill")) if isinstance(llm_result, dict) else "LLM fill"
    spec = capabilities.get(capability_id, {}) if capability_id else {}
    normalized = {
        "domain_id": spec.get("domain_id") if isinstance(spec, dict) else None,
        "task_type_ids": spec.get("task_type_ids", []) if isinstance(spec, dict) else [],
        "capability_id": capability_id,
        "modality_ids": native.get("modality", []) if isinstance(native, dict) else [],
        "interface_ids": native.get("interface", []) if isinstance(native, dict) else [],
        "label_source": label_source,
        "confidence": 1.0 if capability_id and label_source == "upstream_direct" else 0.9 if capability_id and label_source == "rule_normalized" else 0.7 if capability_id else 0.0,
        "needs_review": capability_id is None or bool(llm_result and llm_result.get("uncertainty_reason")),
        "rationale": rationale,
    }
    if llm_result:
        normalized["llm_audit"] = {
            "alternative_labels": llm_result.get("alternative_labels", []),
            "uncertainty_reason": llm_result.get("uncertainty_reason"),
        }
    return {**task, "normalized_taxonomy": normalized}


def _macro_f1(predicted: list[set[str]], expected: list[set[str]]) -> float:
    labels = sorted(set().union(*predicted, *expected))
    if not labels:
        return 1.0
    scores = []
    for label in labels:
        tp = sum(label in p and label in g for p, g in zip(predicted, expected, strict=True))
        fp = sum(label in p and label not in g for p, g in zip(predicted, expected, strict=True))
        fn = sum(label not in p and label in g for p, g in zip(predicted, expected, strict=True))
        scores.append(0.0 if 2 * tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn))
    return sum(scores) / len(scores)


def classification_metrics(
    predictions: list[dict[str, Any]],
    gold: list[dict[str, Any]],
) -> dict[str, Any]:
    gold_by_id = {str(row["source_id"]): row for row in gold}
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for prediction in predictions:
        expected = gold_by_id.get(str(prediction.get("source_id")))
        if expected is not None:
            grouped[str(prediction.get("label_source"))].append((prediction, expected))
    review_pairs = [
        (bool(prediction.get("needs_review")), bool(expected["needs_review"]))
        for prediction in predictions
        if (expected := gold_by_id.get(str(prediction.get("source_id")))) is not None
        and isinstance(expected.get("needs_review"), bool)
    ]
    review_tp = sum(predicted and expected for predicted, expected in review_pairs)
    review_fp = sum(predicted and not expected for predicted, expected in review_pairs)
    review_fn = sum(not predicted and expected for predicted, expected in review_pairs)
    precision = review_tp / (review_tp + review_fp) if review_tp + review_fp else None
    recall = review_tp / (review_tp + review_fn) if review_tp + review_fn else None
    result: dict[str, Any] = {
        "needs_review": {
            "sample_count": len(review_pairs),
            "positive_count": sum(expected for _, expected in review_pairs),
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall else None,
        },
        "by_label_source": {},
    }
    fields = {
        "domain": "domain_id",
        "task_type": "task_type_ids",
        "capability": "capability_id",
    }
    for source in LABEL_SOURCES:
        pairs = grouped.get(source, [])
        source_result: dict[str, Any] = {"sample_count": len(pairs)}
        for output_name, field in fields.items():
            predicted_sets = [
                set(map(str, pair[0].get(field, []))) if isinstance(pair[0].get(field), list) else {str(pair[0].get(field))}
                for pair in pairs
            ]
            expected_sets = [
                set(map(str, pair[1].get(field, []))) if isinstance(pair[1].get(field), list) else {str(pair[1].get(field))}
                for pair in pairs
            ]
            accuracy = (
                sum(p == g for p, g in zip(predicted_sets, expected_sets, strict=True)) / len(pairs)
                if pairs else None
            )
            source_result[output_name] = {
                "accuracy": accuracy,
                "macro_f1": _macro_f1(predicted_sets, expected_sets) if pairs else None,
            }
        result["by_label_source"][source] = source_result
    return result


def role_schedule(capability_ids: list[str], instances_per_capability: int) -> list[dict[str, Any]]:
    if len(capability_ids) != 3 or len(set(capability_ids)) != 3:
        raise ValueError("role schedule requires exactly three capabilities")
    if not 4 <= instances_per_capability <= 6:
        raise ValueError("instances_per_capability must be 4..6")
    schedule = []
    positions: dict[str, list[int]] = defaultdict(list)
    for round_index in range(instances_per_capability):
        for capability_id in capability_ids:
            role = "learning" if round_index == 0 else "probe" if round_index == instances_per_capability - 1 else "application"
            row = {
                "position": len(schedule) + 1,
                "capability_id": capability_id,
                "sequence_role": role,
            }
            if role == "probe":
                row.update({
                    "expected_transfer_positions": list(positions[capability_id]),
                    "interference_capability_ids": sorted(set(capability_ids) - {capability_id}),
                })
            positions[capability_id].append(len(schedule) + 1)
            schedule.append(row)
    return schedule


SOURCE_CHECKOUT_DIRS = {
    "skilllearnbench": "skilllearnbench",
    "skillsbench": "skillsbench",
    "sr_agents": "sr-agents",
    "tau2_bench": "tau2-bench",
}

_VERIFIER_CLI = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
from source_verifier import verify

turn_dir = Path(__file__).resolve().parent.parent
task = json.loads((turn_dir / "task.json").read_text(encoding="utf-8"))
verifier_id = task["evaluation"]["native_verifier"]["verifier_id"]
try:
    request = json.load(sys.stdin)
    if request.get("schema_version") != "l2_native_verifier_request/1.0":
        raise ValueError("unsupported request schema")
    binding = request.get("bindings", {}).get("submission", {})
    if binding.get("kind") == "inline":
        submission = binding.get("value")
    elif binding.get("kind") == "path":
        workspace = Path(os.environ.get("L2_VERIFIER_WORKSPACE", turn_dir.parent / "agent_workspace")).resolve()
        declared = task["evaluation"]["native_verifier"]["input_bindings"][0]["path"]
        if binding.get("path") != declared:
            raise ValueError("submission path does not match contract")
        submission = (workspace / Path(declared).relative_to("agent_workspace")).resolve()
        if submission != workspace and workspace not in submission.parents:
            raise ValueError("submission escapes workspace")
    else:
        raise ValueError("unsupported submission binding")
    passed, details = verify(submission, turn_dir)
    status = "pass" if passed else "fail"
    result = {"schema_version":"l2_native_verifier_result/1.0","verifier_id":verifier_id,
              "status":status,"score":1.0 if passed else 0.0,
              "checks":[{"check_id":"native_verifier_outcome","status":status,
                         "score":1.0 if passed else 0.0,"evidence":["source-native verifier completed"]}],
              "artifacts":[],"public_summary":{"result_category":status,
              "observable_deficiencies":[] if passed else ["The source-native verifier rejected the submission."]},
              "private_diagnostics":details,"error":None}
except Exception as exc:
    result = {"schema_version":"l2_native_verifier_result/1.0","verifier_id":verifier_id,
              "status":"error","score":None,"checks":[],"artifacts":[],
              "public_summary":{"result_category":"error","observable_deficiencies":["Native verification could not complete."]},
              "private_diagnostics":{},"error":f"{type(exc).__name__}: {exc}"}
json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True); sys.stdout.write("\\n")
'''

_SR_VERIFIER = '''import json, subprocess, sys, tempfile
from pathlib import Path
def verify(submission, turn_dir):
    if not isinstance(submission, str) or not submission.strip(): return False,{"reason":"empty_submission"}
    item=json.loads((turn_dir/"evaluation_assets/source_item.json").read_text())
    with tempfile.TemporaryDirectory(prefix="l2-sr-") as raw:
        root=Path(raw); (root/"candidate.py").write_text(submission)
        (root/"runner.py").write_text("import runpy\\nglobals().update(runpy.run_path('candidate.py'))\\n"+item["eval_data"]["test"]+"\\nimport unittest\\nunittest.main()\\n")
        run=subprocess.run([sys.executable,"runner.py"],cwd=root,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
    return run.returncode==0,{"carrier":"sr_exact_answer_native_tests/1.0","exit_code":run.returncode,"output_tail":run.stdout[-4000:]}
'''

_TAU2_VERIFIER = '''import json
def verify(submission, turn_dir):
    if not isinstance(submission,str): return False,{"reason":"inline_json_required"}
    value=json.loads(submission); actual=value.get("tool_calls") if isinstance(value,dict) else None
    expected=json.loads((turn_dir/"evaluation_assets/expected_actions.json").read_text())["actions"]
    normalized=[{"name":x.get("name"),"arguments":x.get("arguments",{})} for x in actual] if isinstance(actual,list) else []
    passed=normalized==expected
    return passed,{"carrier":"tau2_expected_action_list/1.0","ordered_exact_match":passed,"expected_action_count":len(expected),"actual_action_count":len(normalized)}
'''

_UPSTREAM_VERIFIER = '''import os, shutil, subprocess, sys
from pathlib import Path
def verify(submission, turn_dir):
    if not isinstance(submission,Path) or not submission.is_dir(): return False,{"reason":"workspace_required"}
    tests=turn_dir/"evaluation_assets/native_test_files/test_outputs.py"
    if not tests.is_file(): return False,{"reason":"upstream_test_outputs_missing"}
    # Upstream task images conventionally expose the task workspace at /root
    # or /app. The verifier container is disposable, so mirror only public
    # submission files into those compatibility locations before pytest.
    compatibility_root=Path(os.environ.get("L2_VERIFIER_COMPAT_ROOT","/"))
    root_dir=compatibility_root/"root"; app_dir=compatibility_root/"app"
    for destination in (root_dir,app_dir):
        destination.mkdir(parents=True,exist_ok=True)
        for child in submission.iterdir():
            target=destination/child.name
            if child.is_dir(): shutil.copytree(child,target,dirs_exist_ok=True)
            else: shutil.copy2(child,target)
    env=dict(os.environ); env.update({"HOME":str(root_dir),"L2_AGENT_WORKSPACE":str(submission)})
    run=subprocess.run([sys.executable,"-m","pytest","-q",str(tests)],cwd=app_dir,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900)
    return run.returncode==0,{"carrier":"upstream_pytest_native/1.0","exit_code":run.returncode,"output_tail":run.stdout[-4000:]}
'''


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _checkout(benchmark_id: str) -> Path:
    directory = SOURCE_CHECKOUT_DIRS.get(benchmark_id)
    if directory is None:
        raise ValueError(f"unsupported formal source benchmark: {benchmark_id}")
    return REPO_ROOT / "third_party" / "src" / directory


def _sr_item(row: dict[str, Any]) -> dict[str, Any]:
    metadata = _object(row.get("source_metadata", {}), "source_metadata")
    if isinstance(metadata.get("native_source_item"), dict):
        return metadata["native_source_item"]
    source = _object(row.get("source"), "source")
    relative, marker, selector = str(source.get("source_path", "")).partition("#")
    if not marker:
        raise ValueError(f"{row.get('source_id')}: SR source selector is missing")
    catalog = load_json(_checkout("sr_agents") / relative)
    matches = [item for item in catalog if isinstance(item, dict) and str(item.get("instance_id")) == selector]
    if len(matches) != 1 or selector != source.get("source_task_id"):
        raise ValueError(f"{row.get('source_id')}: SR source selector is not unique")
    return matches[0]


def _preflight_native_carrier(row: dict[str, Any]) -> dict[str, Any]:
    source = _object(row.get("source"), "source")
    source_id = str(row.get("source_id"))
    benchmark = source.get("benchmark_id")
    if benchmark == "sr_agents":
        item = _sr_item(row)
        oracle = _object(item.get("eval_data"), f"{source_id} native oracle")
        for field in ("answer", "code_prompt", "test", "entry_point"):
            if not isinstance(oracle.get(field), str) or not oracle[field]:
                raise ValueError(f"{source_id}: native oracle/native test field {field!r} is missing")
        return {"kind": "sr", "item": item}
    if benchmark == "tau2_bench":
        task = _object(_object(row.get("source_metadata"), source_id).get("task"), f"{source_id} task")
        actions = _object(task.get("evaluation_criteria"), f"{source_id} criteria").get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"{source_id}: native expected action list is missing")
        normalized = []
        for action in actions:
            action = _object(action, f"{source_id} action")
            if not isinstance(action.get("name"), str) or not isinstance(action.get("arguments", {}), dict):
                raise ValueError(f"{source_id}: invalid native expected action")
            normalized.append({"name": action["name"], "arguments": action.get("arguments", {})})
        return {"kind": "tau2", "task": task, "actions": normalized}
    if benchmark in {"skilllearnbench", "skillsbench"}:
        task_dir = _checkout(str(benchmark)) / str(source.get("source_path", ""))
        test_dir = task_dir / ("tests" if benchmark == "skilllearnbench" else "verifier")
        oracle_dir = task_dir / ("solution" if benchmark == "skilllearnbench" else "oracle")
        if not (test_dir / "test.sh").is_file():
            raise ValueError(f"{source_id}: upstream native test is missing")
        if not (oracle_dir / "solve.sh").is_file():
            raise ValueError(f"{source_id}: upstream native oracle is missing")
        return {"kind": "upstream", "task_dir": task_dir, "test_dir": test_dir, "oracle_dir": oracle_dir}
    raise ValueError(f"{source_id}: unsupported formal source benchmark {benchmark!r}")


def _prepare_compose(
    classified: list[dict[str, Any]],
    qualification: list[dict[str, Any]],
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if plan.get("schema_version") != "l2_stream_plan/1.0":
        raise ValueError("plan must declare l2_stream_plan/1.0")
    streams = plan.get("streams")
    if not isinstance(streams, list):
        raise ValueError("plan streams must be a list")
    expected = {
        "intra_benchmark_multi_capability": 5,
        "cross_benchmark_multi_capability": 5,
    }
    if Counter(item.get("pool_id") for item in streams if isinstance(item, dict)) != expected:
        raise ValueError("compose requires exactly 5 streams in each formal pool")
    source_map = {str(row.get("source_id")): row for row in classified}
    qualification_map = {str(row.get("source_id")): row for row in qualification}
    if len(source_map) != len(classified) or len(qualification_map) != len(qualification):
        raise ValueError("source_id values must be unique")
    profiles = plan.get("runtime_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("plan runtime_profiles must be non-empty")
    profile_map = {str(row.get("runtime_profile_id")): row for row in profiles if isinstance(row, dict)}
    if len(profile_map) != len(profiles):
        raise ValueError("runtime_profile_id values must be unique")
    for profile_id, profile in profile_map.items():
        for digest_field in ("task_sandbox_image_digest", "verifier_image_digest"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(profile.get(digest_field, ""))):
                raise ValueError(f"{profile_id}: {digest_field} must be pinned")
        if profile.get("network") != "disabled":
            raise ValueError(f"{profile_id}: network must be disabled")
    selected: set[str] = set()
    stream_ids: set[str] = set()
    carriers: dict[str, dict[str, Any]] = {}
    for raw_stream in streams:
        stream = _object(raw_stream, "stream")
        stream_id = stream.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id or stream_id in stream_ids:
            raise ValueError("stream_id values must be non-empty and unique")
        stream_ids.add(stream_id)
        capabilities = stream.get("capabilities")
        if not isinstance(capabilities, list) or len(capabilities) != 3:
            raise ValueError(f"{stream_id}: exactly three capabilities are required")
        capability_ids = [item.get("capability_id") for item in capabilities if isinstance(item, dict)]
        if len(capability_ids) != 3 or len(set(capability_ids)) != 3:
            raise ValueError(f"{stream_id}: capabilities must be distinct")
        benchmarks: set[str] = set()
        abis: set[str] = set()
        for raw_capability in capabilities:
            capability = _object(raw_capability, f"{stream_id} capability")
            source_ids = capability.get("source_ids")
            if not isinstance(source_ids, list) or len(source_ids) != 5:
                raise ValueError(f"{stream_id}/{capability.get('capability_id')}: exactly 5 source IDs are required")
            for source_id in source_ids:
                if not isinstance(source_id, str) or source_id not in source_map:
                    raise ValueError(f"{stream_id}: unknown source identity {source_id!r}")
                if source_id in selected:
                    raise ValueError(f"duplicate source identity in plan: {source_id}")
                selected.add(source_id)
                row = source_map[source_id]
                source = _object(row.get("source"), f"{source_id} source")
                taxonomy = _object(row.get("normalized_taxonomy"), f"{source_id} taxonomy")
                runtime = _object(row.get("runtime"), f"{source_id} runtime")
                qualified = qualification_map.get(source_id, {})
                if qualified.get("admitted") is not True:
                    raise ValueError(f"{source_id}: source is not qualified")
                if taxonomy.get("needs_review") or taxonomy.get("capability_id") != capability.get("capability_id"):
                    raise ValueError(f"{source_id}: taxonomy does not match the plan")
                if runtime.get("materializable") is not True or runtime.get("network") != "disabled":
                    raise ValueError(f"{source_id}: source is not materializable offline")
                profile_id = runtime.get("runtime_profile_id")
                if profile_id not in profile_map or qualified.get("runtime_profile_id") != profile_id:
                    raise ValueError(f"{source_id}: runtime profile binding is invalid")
                abi = profile_map[profile_id].get("learner_state_abi")
                if not isinstance(abi, str) or not abi:
                    raise ValueError(f"{profile_id}: learner-state ABI is missing")
                abis.add(abi)
                benchmarks.add(str(source.get("benchmark_id")))
                carriers[source_id] = _preflight_native_carrier(row)
        if len(abis) != 1:
            raise ValueError(f"{stream_id}: learner-state ABIs are incompatible")
        if stream.get("pool_type") == "same_benchmark" and len(benchmarks) != 1:
            raise ValueError(f"{stream_id}: intra stream must use one benchmark")
        if stream.get("pool_type") == "cross_benchmark" and len(benchmarks) != 3:
            raise ValueError(f"{stream_id}: cross stream must use exactly three benchmarks")
    return streams, source_map, carriers


def _copy_shell_script(source: Path, destination: Path) -> None:
    """Copy a UTF-8 shell script with POSIX line endings on every host."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = source.read_text(encoding="utf-8")
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    shutil.copystat(source, destination, follow_symlinks=False)


def _copy_native_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        if path.name == "Dockerfile":
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".sh":
            _copy_shell_script(path, target)
        else:
            shutil.copy2(path, target)


def _materialize_native_carrier(
    row: dict[str, Any], carrier: dict[str, Any], turn_dir: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    assets = turn_dir / "evaluation_assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "verifier_cli.py").write_text(_VERIFIER_CLI, encoding="utf-8")
    if carrier["kind"] == "sr":
        item = carrier["item"]
        write_json(assets / "source_item.json", item)
        (assets / "native_tests.py").write_text(item["eval_data"]["test"] + "\n", encoding="utf-8")
        (assets / "known_correct_response.py").write_text(
            item["eval_data"]["code_prompt"] + item["eval_data"]["answer"] + "\n", encoding="utf-8"
        )
        (assets / "known_broken_response.py").write_text(
            item["eval_data"]["code_prompt"] + "    return None\n", encoding="utf-8"
        )
        (assets / "source_verifier.py").write_text(_SR_VERIFIER, encoding="utf-8")
        return (
            {"mode": "final_response", "payload_kind": "python_source", "source_binding": "submission"},
            {"name": "submission", "source": "interaction_record", "path": "final_response"},
        )
    if carrier["kind"] == "tau2":
        write_json(assets / "source_item.json", carrier["task"])
        write_json(assets / "expected_actions.json", {"actions": carrier["actions"]})
        write_json(assets / "known_correct_response.json", {
            "schema_version": "tau2-action-trajectory/1.0",
            "assistant_message": "Completed.",
            "tool_calls": carrier["actions"],
        })
        write_json(assets / "known_broken_response.json", {
            "schema_version": "tau2-action-trajectory/1.0", "assistant_message": "", "tool_calls": [],
        })
        (assets / "source_verifier.py").write_text(_TAU2_VERIFIER, encoding="utf-8")
        return (
            {"mode": "final_response", "payload_kind": "json_trajectory", "source_binding": "submission"},
            {"name": "submission", "source": "interaction_record", "path": "final_response"},
        )
    _copy_shell_script(carrier["test_dir"] / "test.sh", assets / "native_test.sh")
    _copy_shell_script(carrier["oracle_dir"] / "solve.sh", assets / "oracle_solution.sh")
    _copy_native_tree(carrier["test_dir"], assets / "native_test_files")
    _copy_native_tree(carrier["oracle_dir"], assets / "native_oracle_files")
    _copy_native_tree(carrier["task_dir"] / "environment", turn_dir / "workspace_seed")
    (assets / "source_verifier.py").write_text(_UPSTREAM_VERIFIER, encoding="utf-8")
    return (
        {"mode": "workspace_artifact", "payload_kind": "directory", "source_binding": "submission"},
        {"name": "submission", "source": "workspace", "path": "agent_workspace"},
    )


def _evaluation(stream_id: str, turn_id: str, contract: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "llm_as_judge",
        "protocol": "l2_native_evidence_llm_judge_v1",
        "evaluation_profile": "general",
        "checks": [{
            "check_id": "task_completion", "dimension": "task_success", "weight": 1.0,
            "scoring": "ordinal", "instruction": "Use the source-native verifier as authoritative evidence.",
        }],
        "submission_contract": {
            **contract,
            "adapter": {"owner": "native_verifier", "type": "source_specific", "version": "v5_source_carrier/1.0"},
            "audit": {"retain_raw_submission": True, "retain_adapted_submission": True, "hash_algorithm": "sha256"},
            "failure_semantics": {"empty_or_invalid_submission": "task_failure", "adapter_error": "verifier_error"},
        },
        "native_verifier": {
            "required": True, "verifier_id": f"{stream_id}_{turn_id}_native_verifier",
            "entrypoint": "evaluation_assets/verifier_cli.py", "input_bindings": [binding],
            "request_schema": "l2_native_verifier_request/1.0",
            "result_schema": "l2_native_verifier_result/1.0", "timeout_seconds": 900,
            "network": "disabled", "authority": "objective_evidence",
        },
        "judge_config": {
            "prompt_version": "l2_judge/2.0",
            "judge_prompt": l2_judge_prompt_v2(),
            "temperature": 0, "output_schema": "l2_judge_output/1.0",
        },
        "success_policy": {
            "native_verifier_requirement": "valid_result_before_judge", "turn_score_threshold": 0.8,
            "judge_failure_blocks_success": True, "interaction_selection": "latest_only",
        },
        "feedback_policy": "bounded_public_failure_feedback_only",
    }


def _binding(path: Path, schema_version: str, relative: str) -> dict[str, str]:
    return {
        "schema_version": schema_version, "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def compose_package(
    classified_path: Path,
    qualification_path: Path,
    plan_path: Path,
    output_package: Path,
) -> dict[str, int]:
    """Compose the reviewed 10x15 V5 package; never infer or backfill selections."""
    classified = read_jsonl(classified_path)
    qualification = read_jsonl(qualification_path)
    plan = _object(load_json(plan_path), "plan")
    streams, source_map, carriers = _prepare_compose(classified, qualification, plan)
    output_package = output_package.resolve()
    output_package.parent.mkdir(parents=True, exist_ok=True)
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="l2-v5-compose-", dir=output_package.parent) as temporary:
        stage = Path(temporary) / output_package.name
        stage.mkdir()
        selected_ids = [
            source_id for stream in streams for capability in stream["capabilities"]
            for source_id in capability["source_ids"]
        ]
        selected = set(selected_ids)
        replace_jsonl(stage / "source_tasks.jsonl", (source_map[item] for item in selected_ids), key="source_id")
        replace_jsonl(
            stage / "source_qualification.jsonl",
            (row for row in qualification if row.get("source_id") in selected), key="source_id",
        )
        write_json(stage / "runtime_profiles.json", {
            "schema_version": "l2_runtime_profiles/1.0",
            "profiles": sorted(plan["runtime_profiles"], key=lambda row: row["runtime_profile_id"]),
        })
        shutil.copy2(plan_path, stage / "stream_plan.json")
        pool_streams: dict[str, list[str]] = defaultdict(list)
        pool_scopes: dict[str, set[str]] = defaultdict(set)
        taxonomy_specs: dict[str, dict[str, Any]] = {}
        manifest_streams = []
        for stream_plan in streams:
            stream_id = stream_plan["stream_id"]
            stream_dir = stage / "streams" / stream_id
            (stream_dir / "agent_workspace").mkdir(parents=True)
            (stream_dir / "agent_workspace" / "README.md").write_text("Task-local Workspace seed.\n", encoding="utf-8")
            capability_ids = [item["capability_id"] for item in stream_plan["capabilities"]]
            source_iters = {item["capability_id"]: iter(item["source_ids"]) for item in stream_plan["capabilities"]}
            capability_specs = []
            for capability in stream_plan["capabilities"]:
                first = source_map[capability["source_ids"][0]]
                taxonomy = first["normalized_taxonomy"]
                spec = {
                    "capability_id": capability["capability_id"], "domain_id": taxonomy["domain_id"],
                    "task_type_ids": taxonomy["task_type_ids"],
                    "source_benchmark": first["source"]["benchmark_id"],
                }
                capability_specs.append(spec)
                taxonomy_specs[capability["capability_id"]] = spec
            summaries: list[dict[str, Any]] = []
            selected_turns: list[dict[str, Any]] = []
            prior: dict[str, list[str]] = defaultdict(list)
            for item in role_schedule(capability_ids, 5):
                turn_index = item["position"]
                turn_id = f"turn{turn_index}"
                capability_id = item["capability_id"]
                source_id = next(source_iters[capability_id])
                row = source_map[source_id]
                source = row["source"]
                taxonomy = row["normalized_taxonomy"]
                profile_id = row["runtime"]["runtime_profile_id"]
                turn_dir = stream_dir / turn_id
                (turn_dir / "workspace_seed").mkdir(parents=True)
                submission, submission_binding = _materialize_native_carrier(row, carriers[source_id], turn_dir)
                parents = list(prior[capability_id])
                task: dict[str, Any] = {
                    "schema_version": "l2_taskstream/1.2", "stream_id": stream_id,
                    "turn_id": turn_id, "turn_index": turn_index,
                    "turn_type": "capability_application", "turn_origin": "source_task",
                    "sequence_role": item["sequence_role"], "interaction_arm": "llm_post_failure",
                    "source_basis": dict(source),
                    "taxonomy_ref": {
                        "taxonomy_version": plan["taxonomy_version"], "domain_id": taxonomy["domain_id"],
                        "task_type_ids": taxonomy["task_type_ids"], "capability_id": capability_id,
                        "native_labels": row.get("benchmark_native_labels", {}),
                    },
                    "selection_provenance": {
                        "plan_sha256": plan_sha256, "classifier_version": plan.get("classifier_version"),
                        "review_version": plan.get("review_version"), "seed": plan.get("selection_seed"),
                        "composition_policy": stream_plan["composition_policy"],
                    },
                    "relation_class": stream_plan["relation_class"], "runtime_profile_id": profile_id,
                    "capability_relation": {"capability_id": capability_id, "parent_turn_ids": parents},
                    "agent_input": {
                        "agent_workspace": "agent_workspace", "workspace_persistence": "task_local",
                        "messages": [{"role": "user", "content": row["prompt"]}],
                        "available_tools": [], "assets": [],
                    },
                    "workspace_transition": {
                        "mode": "task_local_seed",
                        "operations": [{"op": "copy", "source": "workspace_seed", "target": "agent_workspace/."}],
                        "preserve_unrelated_files": False,
                    },
                    "user_simulator": {
                        "type": "llm_post_failure", "trigger": "interaction_unsuccessful_and_budget_remaining",
                        "prompt_version": "l2_user_simulator/1.0",
                        "simulator_prompt": l2_user_simulator_prompt(),
                        "temperature": 0, "output_schema": "l2_user_simulator_output/1.0",
                        "max_generated_messages": 2,
                    },
                    "evaluation": _evaluation(stream_id, turn_id, submission, submission_binding),
                }
                if item["sequence_role"] == "probe":
                    task["probe_spec"] = {
                        "target_capability_id": capability_id,
                        "expected_transfer_from_turn_ids": parents,
                        "interference_capability_ids": item["interference_capability_ids"],
                        "claim_scope": "within_benchmark_retention" if stream_plan["pool_type"] == "same_benchmark"
                        else "cross_benchmark_shift_retention",
                        "held_out_source_task_id": source["source_task_id"],
                    }
                write_json(turn_dir / "task.json", task)
                summaries.append({
                    "turn_id": turn_id, "turn_index": turn_index,
                    "turn_type": "capability_application", "turn_origin": "source_task",
                    "sequence_role": item["sequence_role"], "source_task_id": source["source_task_id"],
                    "capability_id": capability_id, "runtime_profile_id": profile_id,
                })
                selected_turns.append({
                    "turn_id": turn_id, "turn_index": turn_index, "sequence_role": item["sequence_role"],
                    "source_id": source_id, "source_hash": source["source_hash"], "capability_id": capability_id,
                })
                prior[capability_id].append(turn_id)
            profile_ids = {summary["runtime_profile_id"] for summary in summaries}
            profile_rows = [row for row in plan["runtime_profiles"] if row["runtime_profile_id"] in profile_ids]
            stream_document = {
                "schema_version": "l2_taskstream/1.2", "stream_id": stream_id, "directory": stream_id,
                "profile": "L2 heterogeneous capability short stream",
                "theme": "Three source-native capabilities in a 15-Turn interleaved schedule.",
                "turn_count": 15, "task_layout": "turn_directories", "agent_workspace": "agent_workspace",
                "workspace_persistence": "task_local", "turn_type_counts": {"capability_application": 15},
                "fact_thread_count": 0,
                "stream_design": {
                    "pool_id": stream_plan["pool_id"], "pool_type": stream_plan["pool_type"],
                    "composition_policy": stream_plan["composition_policy"],
                },
                "capabilities": capability_specs,
                "ordering": {
                    "method": "fixed_round_robin", "seed": plan.get("selection_seed"),
                    "turn_order": [f"turn{index}" for index in range(1, 16)],
                    "note": "A-L,B-L,C-L then applications and delayed A-P,B-P,C-P.",
                },
                "execution_requirements": {
                    "network": "disabled", "path_scope": "agent_workspace", "max_interactions": 3,
                    "max_tool_calls_per_interaction": 20,
                    "turn_timeout_seconds": max(int(row.get("turn_timeout_seconds", 900)) for row in profile_rows),
                    "max_output_tokens": 12000,
                },
                "turns": summaries, "license_scope": stream_plan["license_scope"],
                "selection_provenance": {
                    "plan_schema_version": plan["schema_version"], "plan_sha256": plan_sha256,
                    "taxonomy_version": plan["taxonomy_version"],
                    "classifier_version": plan.get("classifier_version"),
                    "review_version": plan.get("review_version"), "seed": plan.get("selection_seed"),
                },
            }
            write_json(stream_dir / "stream.json", stream_document)
            write_json(stage / "selections" / f"{stream_id}.json", {
                "schema_version": "l2_selection_provenance/1.0", "stream_id": stream_id,
                "pool_id": stream_plan["pool_id"], "composition_policy": stream_plan["composition_policy"],
                "taxonomy_version": plan["taxonomy_version"], "classifier_version": plan.get("classifier_version"),
                "review_version": plan.get("review_version"), "seed": plan.get("selection_seed"),
                "selected_turns": selected_turns,
            })
            pool_streams[stream_plan["pool_id"]].append(stream_id)
            pool_scopes[stream_plan["pool_id"]].add(stream_plan["license_scope"])
            manifest_streams.append({
                "stream_id": stream_id, "directory": stream_id, "turn_count": 15,
                "path": f"streams/{stream_id}/stream.json",
            })
        pool_ids = ("intra_benchmark_multi_capability", "cross_benchmark_multi_capability")
        write_json(stage / "pools.json", {
            "schema_version": "l2_pools/1.0",
            "pools": [{
                "pool_id": pool_id,
                "pool_type": "same_benchmark" if pool_id.startswith("intra_") else "cross_benchmark",
                "stream_ids": pool_streams[pool_id],
                "composition_policy": "three_distinct_capabilities_one_benchmark" if pool_id.startswith("intra_")
                else "three_capabilities_three_benchmarks",
                "license_scopes": sorted(pool_scopes[pool_id]), "allows_cross_pool_aggregation": False,
            } for pool_id in pool_ids],
        })
        write_json(stage / "panels.json", {
            "schema_version": "l2_panels/1.0",
            "panels": [{
                "panel_id": f"{pool_id}_primary", "purpose": "primary", "pool_id": pool_id,
                "stream_ids": pool_streams[pool_id], "allows_cross_panel_aggregation": False,
            } for pool_id in pool_ids],
        })
        write_json(stage / "taxonomy.json", {
            "schema_version": "l2_taxonomy/1.0", "taxonomy_version": plan["taxonomy_version"],
            "capabilities": [taxonomy_specs[key] for key in sorted(taxonomy_specs)],
        })
        manifest = {
            "schema_version": "l2_taskstream/1.2", "benchmark": plan.get("package_id", "L2_Demo_V5"),
            "aggregation_profile": "l2_evolution_aggregation/1.2", "stream_count": 10,
            "total_turn_count": 150, "unique_source_task_count": 150, "turn_placement_count": 150,
            "streams": manifest_streams,
            "pool_manifest": _binding(stage / "pools.json", "l2_pools/1.0", "pools.json"),
            "panel_manifest": _binding(stage / "panels.json", "l2_panels/1.0", "panels.json"),
            "taxonomy_manifest": _binding(stage / "taxonomy.json", "l2_taxonomy/1.0", "taxonomy.json"),
            "runtime_profiles": _binding(stage / "runtime_profiles.json", "l2_runtime_profiles/1.0", "runtime_profiles.json"),
            "source_catalog": _binding(stage / "source_tasks.jsonl", "l2_source_catalog/1.0", "source_tasks.jsonl"),
            "selection_manifest": _binding(stage / "stream_plan.json", "l2_stream_plan/1.0", "stream_plan.json"),
        }
        write_json(stage / "manifest.json", manifest)
        if output_package.exists():
            generated = (
                "manifest.json", "pools.json", "panels.json", "taxonomy.json",
                "runtime_profiles.json", "stream_plan.json", "streams", "selections",
            )
            conflicts = [name for name in generated if (output_package / name).exists()]
            if conflicts:
                raise ValueError(f"output package already contains generated paths: {conflicts}")
            shutil.copytree(stage, output_package, dirs_exist_ok=True)
        else:
            shutil.copytree(stage, output_package)
    return {"stream_count": 10, "turn_count": 150, "unique_source_task_count": 150}


def _default_stream_plan() -> dict[str, Any]:
    return {
        "schema_version": "l2_v5_stream_plan/1.0",
        "seed": COMPOSITION_SEED,
        "instances_per_capability": 4,
        "streams": [
            {
                "stream_id": "intra_tau2_banking_ops_01",
                "pool_id": "intra_benchmark_multi_capability",
                "pool_type": "same_benchmark",
                "benchmark_capabilities": [
                    ["tau2_bench", "banking_product_application"],
                    ["tau2_bench", "retail_exchange_resolution"],
                    ["tau2_bench", "telecom_connectivity_troubleshooting"],
                ],
            },
            {
                "stream_id": "intra_skillsbench_ops_01",
                "pool_id": "intra_benchmark_multi_capability",
                "pool_type": "same_benchmark",
                "benchmark_capabilities": [
                    ["skillsbench", "security_code_repair"],
                    ["skillsbench", "office_document_automation"],
                    ["skillsbench", "constrained_operations_planning"],
                ],
            },
            {
                "stream_id": "intra_sr_python_data_01",
                "pool_id": "intra_benchmark_multi_capability",
                "pool_type": "same_benchmark",
                "benchmark_capabilities": [
                    ["sr_agents", "pandas_dataframe_manipulation"],
                    ["sr_agents", "numpy_numerical_computing"],
                    ["sr_agents", "filesystem_path_operations"],
                ],
            },
            {
                "stream_id": "intra_sr_python_visual_01",
                "pool_id": "intra_benchmark_multi_capability",
                "pool_type": "same_benchmark",
                "benchmark_capabilities": [
                    ["sr_agents", "matplotlib_visualization"],
                    ["sr_agents", "pandas_dataframe_manipulation"],
                    ["sr_agents", "filesystem_path_operations"],
                ],
            },
            {
                "stream_id": "intra_tau2_support_01",
                "pool_id": "intra_benchmark_multi_capability",
                "pool_type": "same_benchmark",
                "benchmark_capabilities": [
                    ["tau2_bench", "retail_exchange_resolution"],
                    ["tau2_bench", "airline_booking_change"],
                    ["tau2_bench", "telecom_connectivity_troubleshooting"],
                ],
            },
            {
                "stream_id": "cross_skilllearn_tau2_sr_01",
                "pool_id": "cross_benchmark_multi_capability",
                "pool_type": "cross_benchmark",
                "benchmark_capabilities": [
                    ["skilllearnbench", "dependency_vulnerability_audit"],
                    ["tau2_bench", "banking_product_application"],
                    ["sr_agents", "matplotlib_visualization"],
                ],
            },
            {
                "stream_id": "cross_skilllearn_tau2_sr_02",
                "pool_id": "cross_benchmark_multi_capability",
                "pool_type": "cross_benchmark",
                "benchmark_capabilities": [
                    ["skilllearnbench", "stock_data_visualization"],
                    ["tau2_bench", "retail_exchange_resolution"],
                    ["sr_agents", "numpy_numerical_computing"],
                ],
            },
            {
                "stream_id": "cross_skilllearn_tau2_sr_03",
                "pool_id": "cross_benchmark_multi_capability",
                "pool_type": "cross_benchmark",
                "benchmark_capabilities": [
                    ["skilllearnbench", "dbscan_parameter_tuning"],
                    ["tau2_bench", "telecom_connectivity_troubleshooting"],
                    ["sr_agents", "pandas_dataframe_manipulation"],
                ],
            },
            {
                "stream_id": "cross_skilllearn_tau2_sr_04",
                "pool_id": "cross_benchmark_multi_capability",
                "pool_type": "cross_benchmark",
                "benchmark_capabilities": [
                    ["skilllearnbench", "weighted_gdp_analysis"],
                    ["tau2_bench", "airline_booking_change"],
                    ["sr_agents", "filesystem_path_operations"],
                ],
            },
            {
                "stream_id": "cross_skilllearn_tau2_sr_05",
                "pool_id": "cross_benchmark_multi_capability",
                "pool_type": "cross_benchmark",
                "benchmark_capabilities": [
                    ["skilllearnbench", "court_form_filling"],
                    ["tau2_bench", "retail_exchange_resolution"],
                    ["sr_agents", "matplotlib_visualization"],
                ],
            },
        ],
    }


def _runtime_profile_id(source_profile_id: str) -> str:
    return {
        "skilllearn_task_v1": "skilllearn_python_file_v1",
        "skillsbench_task_v1": "skillsbench_python_task_v1",
        "sr_agents_python_v1": "sr_agents_python_v1",
        "tau2_tool_v1": "tau2_tool_v1",
    }.get(source_profile_id, source_profile_id)


def _runtime_profiles() -> dict[str, Any]:
    profiles = [
        ("skilllearn_python_file_v1", "python_stdlib_plus_local_assets", "terminal_filesystem_v1", 900),
        ("skillsbench_python_task_v1", "python_native_task_assets", "terminal_filesystem_v1", 1200),
        ("sr_agents_python_v1", "python_code_generation", "terminal_filesystem_v1", 900),
        ("tau2_tool_v1", "tau2_policy_tool_simulator", "terminal_json_tools_v1", 900),
    ]
    return {
        "schema_version": "l2_runtime_profiles/1.0",
        "profiles": [
            {
                "runtime_profile_id": profile_id,
                "task_sandbox_image_digest": DEFAULT_LOCAL_L2_IMAGE_DIGEST,
                "verifier_image_digest": DEFAULT_LOCAL_L2_IMAGE_DIGEST,
                "verifier_image_class": "python",
                "tool_profile_id": tool_profile,
                "dependency_class": dependency_class,
                "network": "disabled",
                "turn_timeout_seconds": timeout,
                "learner_state_mount_profile": "baseline_explicit_v1",
                "learner_state_abi": "baseline_state/1.0",
                "serialization_version": "1",
                "mount_path": "/baseline_state",
                "owner": "baseline",
                "permission": "rw",
            }
            for profile_id, dependency_class, tool_profile, timeout in profiles
        ],
    }


def _copy_contract(v4_contract: Path, package: Path) -> None:
    destination = package / "contract"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(v4_contract, destination)


def _selected_task_groups(
    classified: list[dict[str, Any]],
    qualification: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    admitted = {
        str(row["source_id"])
        for row in qualification
        if row.get("admitted") is True
    }
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in classified:
        taxonomy = row.get("normalized_taxonomy", {})
        source = row.get("source", {})
        if (
            row.get("source_id") in admitted
            and taxonomy.get("capability_id")
            and not taxonomy.get("needs_review")
            and row.get("runtime", {}).get("network") == "disabled"
        ):
            groups[
                (str(source.get("benchmark_id")), str(taxonomy["capability_id"]))
            ].append(row)
    for key in list(groups):
        groups[key] = sorted(groups[key], key=lambda item: str(item["source_id"]))
    return groups


def _capability_spec(codebook: dict[str, Any], capability_id: str) -> dict[str, Any]:
    spec = codebook.get("capabilities", {}).get(capability_id)
    if not isinstance(spec, dict):
        raise ValueError(f"capability {capability_id!r} is not in the codebook")
    return spec


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _compose_turn_task(
    *,
    stream_id: str,
    turn_id: str,
    turn_index: int,
    role: str,
    row: dict[str, Any],
    codebook: dict[str, Any],
    parent_turn_ids: list[str],
    probe_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    source = row["source"]
    taxonomy = row["normalized_taxonomy"]
    capability_id = str(taxonomy["capability_id"])
    source_profile_id = str(row.get("runtime", {}).get("runtime_profile_id"))
    spec = _capability_spec(codebook, capability_id)
    return {
        "schema_version": "l2_taskstream/1.2",
        "stream_id": stream_id,
        "turn_id": turn_id,
        "turn_index": turn_index,
        "sequence_role": role,
        "source_basis": {
            "benchmark_id": str(source["benchmark_id"]),
            "benchmark_revision": str(source["benchmark_revision"]),
            "source_task_id": str(source["source_task_id"]),
            "source_path": str(source["source_path"]),
            "source_hash": str(source["source_hash"]),
        },
        "taxonomy_ref": {
            "taxonomy_version": TAXONOMY_VERSION,
            "domain_id": str(taxonomy["domain_id"]),
            "task_type_ids": list(taxonomy.get("task_type_ids", [])),
            "capability_id": capability_id,
            "native_labels": row.get("benchmark_native_labels", {}),
            "label_source": taxonomy.get("label_source"),
        },
        "relation_class": (
            "native_family_sibling"
            if taxonomy.get("label_source") in {"upstream_direct", "rule_normalized"}
            else "reviewed_shared_procedure"
        ),
        "runtime_profile_id": _runtime_profile_id(source_profile_id),
        "capability_relation": {
            "capability_id": capability_id,
            "parent_turn_ids": parent_turn_ids,
            "relation_rationale": spec.get("inclusion", ""),
        },
        "agent_input": {
            "messages": [{
                "role": "user",
                "content": (
                    "Solve the source benchmark task below. Produce the requested artifact "
                    "inside the task workspace and do not use network access.\n\n"
                    f"Source benchmark: {source['benchmark_id']}\n"
                    f"Source task id: {source['source_task_id']}\n\n"
                    f"{row.get('prompt', '')}"
                ),
            }],
            "available_tools": ["terminal", "filesystem"],
            "assets": [{"path": "source_task.json", "visibility": "agent_visible"}],
        },
        "workspace_transition": {
            "mode": "task_local_seed",
            "operations": [{
                "op": "copy",
                "source": "assets/source_task.json",
                "target": f"agent_workspace/source/{turn_id}.json",
            }],
        },
        "evaluation": {
            "type": "native_verifier_only",
            "evaluation_profile": "general",
            "submission_contract": {
                "schema_version": "l2_submission_contract/1.0",
                "carrier": "interaction_record",
                "source_binding": "final_response",
            },
            "native_verifier": {
                "required": True,
                "entrypoint": "verifier.py",
                "timeout_seconds": 60,
                "input_bindings": [{
                    "name": "final_response",
                    "source": "interaction_record",
                    "path": "final_response",
                }],
                "request_schema_version": "l2_native_verifier_request/1.0",
                "result_schema_version": "l2_native_verifier_result/1.0",
                "source_benchmark": source["benchmark_id"],
                "source_task_id": source["source_task_id"],
            },
            "success_policy": {"turn_score_threshold": 0.8},
        },
        **({"probe_spec": probe_spec} if probe_spec is not None else {}),
    }


def _hash_binding(path: Path, schema_version: str) -> dict[str, str]:
    return {
        "schema_version": schema_version,
        "path": path.name,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _materialize_native_assets(row: dict[str, Any], destination: Path | None) -> None:
    source = row["source"]
    benchmark = str(source["benchmark_id"])
    source_path = str(source["source_path"]).split("#", 1)[0]
    if benchmark == "sr_agents":
        source_item = row.get("source_metadata", {}).get("native_source_item")
        eval_data = source_item.get("eval_data") if isinstance(source_item, dict) else None
        native_test = eval_data.get("test") if isinstance(eval_data, dict) else None
        if not isinstance(native_test, str) or not native_test.strip():
            raise ValueError(f"{row['source_id']}: missing native test oracle")
        if destination is None:
            return
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "source_item.json", source_item)
        _write_text(destination / "native_tests.py", native_test)
        return
    if benchmark == "tau2_bench":
        task = row.get("source_metadata", {}).get("task")
        criteria = task.get("evaluation_criteria") if isinstance(task, dict) else None
        actions = criteria.get("actions") if isinstance(criteria, dict) else None
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"{row['source_id']}: missing native oracle actions")
        if destination is None:
            return
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "expected_actions.json", {"actions": actions})
        write_json(destination / "source_task.json", task)
        return
    upstream = REPO_ROOT / "third_party" / "src" / benchmark / source_path
    native_test = upstream / ("tests/test.sh" if benchmark == "skilllearnbench" else "verifier/test.sh")
    oracle = upstream / ("solution/solve.sh" if benchmark == "skilllearnbench" else "oracle/solve.sh")
    if not native_test.is_file() or not oracle.is_file():
        raise ValueError(f"{row['source_id']}: missing native oracle files")
    if destination is None:
        return
    destination.mkdir(parents=True, exist_ok=True)
    _copy_shell_script(native_test, destination / "native_test.sh")
    _copy_shell_script(oracle, destination / "oracle_solution.sh")


def _legacy_compose_package(
    classified_path: Path,
    qualification_path: Path,
    plan_path: Path,
    output: Path,
) -> dict[str, int]:
    classified = {row["source_id"]: row for row in read_jsonl(classified_path)}
    admitted = {
        row["source_id"]
        for row in read_jsonl(qualification_path)
        if row.get("admitted") is True
    }
    plan = load_json(plan_path)
    streams = plan.get("streams")
    if not isinstance(streams, list):
        raise ValueError("plan streams must be a list")
    by_pool = Counter(str(stream.get("pool_id")) for stream in streams if isinstance(stream, dict))
    if by_pool.get("intra_benchmark_multi_capability") != 5 or by_pool.get("cross_benchmark_multi_capability") != 5:
        raise ValueError("compose plan must contain exactly 5 streams per pool")

    placements: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    used: set[str] = set()
    for stream in streams:
        selected_rows: list[dict[str, Any]] = []
        capabilities = stream.get("capabilities")
        if not isinstance(capabilities, list) or len(capabilities) != 3:
            raise ValueError(f"{stream.get('stream_id')}: expected exactly three capabilities")
        for capability in capabilities:
            source_ids = capability.get("source_ids") if isinstance(capability, dict) else None
            if not isinstance(source_ids, list) or len(source_ids) not in {4, 5, 6}:
                raise ValueError(f"{stream.get('stream_id')}: each capability needs 4..6 source ids")
            for source_id in source_ids:
                if source_id in used:
                    raise ValueError(f"duplicate source identity: {source_id}")
                if source_id not in admitted or source_id not in classified:
                    raise ValueError(f"{source_id}: not qualified for composition")
                row = classified[str(source_id)]
                if row.get("normalized_taxonomy", {}).get("capability_id") != capability.get("capability_id"):
                    raise ValueError(f"{source_id}: taxonomy capability mismatch")
                _materialize_native_assets(row, None)
                selected_rows.append(row)
                used.add(str(source_id))
        placements.append((stream, selected_rows))

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    runtime_profiles = plan.get("runtime_profiles", [])
    for profile in runtime_profiles:
        if isinstance(profile, dict) and "verifier_image_digest" not in profile:
            profile["verifier_image_digest"] = "sha256:" + hashlib.sha256(
                f"verifier:{profile.get('runtime_profile_id')}".encode("utf-8")
            ).hexdigest()
    write_json(output / "runtime_profiles.json", {
        "schema_version": "l2_runtime_profiles/1.0",
        "profiles": runtime_profiles,
    })

    stream_entries = []
    pool_streams: dict[str, list[str]] = defaultdict(list)
    total_turns = 0
    for stream, selected_rows in placements:
        stream_id = str(stream["stream_id"])
        stream_dir = output / "streams" / stream_id
        (stream_dir / "agent_workspace").mkdir(parents=True)
        _write_text(stream_dir / "agent_workspace" / "README.md", "Task-local seed workspace.\n")
        capability_ids = [str(item["capability_id"]) for item in stream["capabilities"]]
        per_capability = len(stream["capabilities"][0]["source_ids"])
        schedule = role_schedule(capability_ids, per_capability)
        rows_by_capability: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in selected_rows:
            rows_by_capability[str(row["normalized_taxonomy"]["capability_id"])].append(row)
        consumed: dict[str, int] = defaultdict(int)
        parents: dict[str, list[str]] = defaultdict(list)
        summaries = []
        turn_ids = []
        for item in schedule:
            capability_id = str(item["capability_id"])
            row = rows_by_capability[capability_id][consumed[capability_id]]
            consumed[capability_id] += 1
            turn_id = f"turn{item['position']}"
            turn_ids.append(turn_id)
            runtime_profile_id = _runtime_profile_id(str(row["runtime"]["runtime_profile_id"]))
            taxonomy = row["normalized_taxonomy"]
            source = row["source"]
            probe_spec = None
            if item["sequence_role"] == "probe":
                probe_spec = {
                    "target_capability_id": capability_id,
                    "expected_transfer_from_turn_ids": list(parents[capability_id]),
                    "interference_capability_ids": list(item["interference_capability_ids"]),
                    "claim_scope": "within_benchmark_retention" if stream["pool_type"] == "same_benchmark" else "cross_benchmark_retention_under_runtime_shift",
                    "held_out_source_task_id": source["source_task_id"],
                }
            task = {
                "schema_version": "l2_taskstream/1.2",
                "stream_id": stream_id,
                "turn_id": turn_id,
                "turn_index": int(item["position"]),
                "sequence_role": item["sequence_role"],
                "source_basis": source,
                "taxonomy_ref": {
                    "taxonomy_version": plan.get("taxonomy_version", TAXONOMY_VERSION),
                    "domain_id": taxonomy["domain_id"],
                    "task_type_ids": taxonomy["task_type_ids"],
                    "capability_id": capability_id,
                    "native_labels": row.get("benchmark_native_labels", {}),
                },
                "relation_class": stream.get("relation_class", "native_family_sibling"),
                "runtime_profile_id": runtime_profile_id,
                "capability_relation": {
                    "capability_id": capability_id,
                    "parent_turn_ids": list(parents[capability_id]),
                },
                "agent_input": {"messages": [{"role": "user", "content": row.get("prompt", "")}]},
                "workspace_transition": {"mode": "task_local_seed", "operations": []},
                "evaluation": {
                    "type": "native_verifier_only",
                    "submission_contract": {
                        "schema_version": "l2_submission_contract/1.0",
                        "carrier": "interaction_record",
                        "source_binding": "final_response",
                    },
                    "native_verifier": {
                        "required": True,
                        "entrypoint": "verifier.py",
                        "timeout_seconds": 60,
                        "input_bindings": [{
                            "name": "final_response",
                            "source": "interaction_record",
                            "path": "final_response",
                        }],
                    },
                },
                **({"probe_spec": probe_spec} if probe_spec else {}),
            }
            turn_dir = stream_dir / turn_id
            _materialize_native_assets(row, turn_dir / "evaluation_assets")
            _write_text(turn_dir / "verifier.py", GENERIC_V5_VERIFIER)
            write_json(turn_dir / "task.json", task)
            parents[capability_id].append(turn_id)
            summaries.append({
                "turn_id": turn_id,
                "turn_index": int(item["position"]),
                "sequence_role": item["sequence_role"],
                "capability_id": capability_id,
                "runtime_profile_id": runtime_profile_id,
            })
        capabilities = []
        benchmarks = set()
        for capability in stream["capabilities"]:
            first = classified[capability["source_ids"][0]]
            taxonomy = first["normalized_taxonomy"]
            benchmark = first["source"]["benchmark_id"]
            benchmarks.add(benchmark)
            capabilities.append({
                "capability_id": capability["capability_id"],
                "domain_id": taxonomy["domain_id"],
                "task_type_ids": taxonomy["task_type_ids"],
                "source_benchmark": benchmark,
            })
        if stream["pool_type"] == "same_benchmark" and len(benchmarks) != 1:
            raise ValueError(f"{stream_id}: intra stream has multiple benchmarks")
        if stream["pool_type"] == "cross_benchmark" and len(benchmarks) != 3:
            raise ValueError(f"{stream_id}: cross stream must use three benchmarks")
        write_json(stream_dir / "stream.json", {
            "schema_version": "l2_taskstream/1.2",
            "stream_id": stream_id,
            "directory": stream_id,
            "turn_count": len(turn_ids),
            "agent_workspace": "agent_workspace",
            "workspace_persistence": "task_local",
            "stream_design": {
                "pool_id": stream["pool_id"],
                "pool_type": stream["pool_type"],
                "composition_policy": stream["composition_policy"],
            },
            "capabilities": capabilities,
            "ordering": {"method": "round_robin", "seed": plan.get("selection_seed"), "turn_order": turn_ids},
            "execution_requirements": {
                "network": "disabled",
                "path_scope": "agent_workspace",
                "max_interactions": 3,
                "max_tool_calls_per_interaction": 20,
                "turn_timeout_seconds": 1200,
                "max_output_tokens": 12000,
            },
            "turns": summaries,
            "selection_provenance": {
                "selector": "tools/l2_data_pipeline.py compose_package",
                "seed": plan.get("selection_seed"),
                "taxonomy_version": plan.get("taxonomy_version"),
            },
        })
        pool_streams[str(stream["pool_id"])].append(stream_id)
        stream_entries.append({"stream_id": stream_id, "directory": stream_id, "turn_count": len(turn_ids), "path": f"streams/{stream_id}/stream.json"})
        total_turns += len(turn_ids)

    write_json(output / "pools.json", {
        "schema_version": "l2_pools/1.0",
        "pools": [
            {"pool_id": "intra_benchmark_multi_capability", "pool_type": "same_benchmark", "stream_ids": pool_streams["intra_benchmark_multi_capability"], "composition_policy": "three_distinct_capabilities_one_benchmark", "license_scopes": ["permissive"], "allows_cross_pool_aggregation": False},
            {"pool_id": "cross_benchmark_multi_capability", "pool_type": "cross_benchmark", "stream_ids": pool_streams["cross_benchmark_multi_capability"], "composition_policy": "three_capabilities_three_benchmarks", "license_scopes": ["permissive"], "allows_cross_pool_aggregation": False},
        ],
    })
    write_json(output / "panels.json", {
        "schema_version": "l2_panels/1.0",
        "panels": [
            {"panel_id": "intra_primary", "pool_id": "intra_benchmark_multi_capability", "stream_ids": pool_streams["intra_benchmark_multi_capability"], "allows_cross_panel_aggregation": False},
            {"panel_id": "cross_primary", "pool_id": "cross_benchmark_multi_capability", "stream_ids": pool_streams["cross_benchmark_multi_capability"], "allows_cross_panel_aggregation": False},
        ],
    })
    write_json(output / "taxonomy.json", {"schema_version": "l2_taxonomy/1.0", "taxonomy_version": plan.get("taxonomy_version", TAXONOMY_VERSION)})
    write_json(output / "manifest.json", {
        "schema_version": "l2_taskstream/1.2",
        "aggregation_profile": "l2_evolution_aggregation/1.2",
        "stream_count": len(stream_entries),
        "total_turn_count": total_turns,
        "unique_source_task_count": len(used),
        "turn_placement_count": total_turns,
        "streams": stream_entries,
        "pool_manifest": _hash_binding(output / "pools.json", "l2_pools/1.0"),
        "panel_manifest": _hash_binding(output / "panels.json", "l2_panels/1.0"),
        "taxonomy_manifest": _hash_binding(output / "taxonomy.json", "l2_taxonomy/1.0"),
        "runtime_profiles": _hash_binding(output / "runtime_profiles.json", "l2_runtime_profiles/1.0"),
    })
    return {"stream_count": len(stream_entries), "turn_count": total_turns, "unique_source_task_count": len(used)}


def _deepseek_fill_factory(
    *,
    cache_dir: Path,
    model: str,
    api_base: str | None,
    dry_run: bool,
) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)

    def fill(task: dict[str, Any], codebook: dict[str, Any]) -> dict[str, Any]:
        source_hash = str(task.get("source", {}).get("source_hash"))
        key = classification_cache_key(source_hash, TAXONOMY_VERSION, PROMPT_VERSION, model)
        cache_path = cache_dir / f"{key}.json"
        if cache_path.is_file():
            return load_json(cache_path)
        if dry_run:
            return {"capability_id": None, "rationale": "dry-run", "uncertainty_reason": "LLM call skipped"}
        from trievo_bench.model import TokenBudget, client_from_config
        budget = TokenBudget(limit=20_000)
        client = client_from_config({
            "model": model,
            **({"base_url": api_base} if api_base else {}),
            "temperature": 0,
            "max_tokens": 800,
        }, budget)
        prompt = {
            "task": {"prompt": task.get("prompt"), "native_labels": task.get("benchmark_native_labels")},
            "allowed_capabilities": codebook.get("capabilities", {}),
            "instruction": "Choose one allowed capability_id. Return JSON with capability_id, rationale, alternative_labels, uncertainty_reason.",
        }
        text = client.complete(
            [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            max_tokens=800,
            json_mode=True,
        )
        result = json.loads(text[text.find("{"): text.rfind("}") + 1])
        result["provider_audit"] = {
            "base_url": client.base_url,
            "model": client.model,
            "temperature": 0,
            "prompt_version": PROMPT_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "reported_or_estimated_tokens": budget.used,
            "calls": budget.calls,
            "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        write_json(cache_path, result)
        return result

    return fill


def _cmd_discover(args: argparse.Namespace) -> int:
    records, checkouts = discover(Path(args.manifest))
    output = Path(args.output)
    replace_jsonl(output, records, key="source_id")
    write_json(Path(args.checkout_report), {"schema_version": "l2_source_checkouts/1.0", "checkouts": checkouts})
    print(json.dumps({"tasks": len(records), "output": str(output)}, ensure_ascii=False))
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    tasks = read_jsonl(Path(args.input))
    codebook = load_json(Path(args.codebook))
    fill = _deepseek_fill_factory(
        cache_dir=Path(args.cache_dir), model=args.model, api_base=args.api_base, dry_run=args.dry_run
    )
    allowed_llm_ids = (
        {
            line.strip()
            for line in Path(args.llm_source_ids).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        if args.llm_source_ids
        else None
    )
    results = []
    llm_count = 0
    for task in tasks:
        result = classify_task(task, codebook=codebook, llm_fill=None)
        llm_eligible = (
            result["normalized_taxonomy"]["capability_id"] is None
            and (allowed_llm_ids is None or task["source_id"] in allowed_llm_ids)
            and (args.max_items is None or llm_count < args.max_items)
        )
        if llm_eligible:
            result = classify_task(task, codebook=codebook, llm_fill=fill)
            llm_count += 1
        results.append(result)
    write_jsonl(Path(args.output), results, key="source_id")
    print(json.dumps({"classified": len(results), "llm_filled": llm_count}, ensure_ascii=False))
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    rows = read_jsonl(Path(args.input))
    queue = [row for row in rows if row.get("normalized_taxonomy", {}).get("needs_review")]
    write_jsonl(Path(args.output), queue, key="source_id")
    gold = read_jsonl(Path(args.gold))
    predictions = [
        {"source_id": row["source_id"], **row.get("normalized_taxonomy", {})}
        for row in rows
    ]
    metrics = classification_metrics(predictions, gold)
    write_json(Path(args.metrics), metrics)
    print(json.dumps({"review_queue": len(queue), "gold": len(gold)}, ensure_ascii=False))
    return 0


def _cmd_qualify(args: argparse.Namespace) -> int:
    results = []
    for row in read_jsonl(Path(args.input)):
        runtime = row.get("runtime", {})
        taxonomy = row.get("normalized_taxonomy", {})
        admitted = bool(runtime.get("materializable")) and not bool(taxonomy.get("needs_review"))
        results.append({
            "source_id": row["source_id"],
            "admitted": admitted,
            "failure_reasons": [
                reason
                for failed, reason in (
                    (not runtime.get("materializable"), f"external_data:{runtime.get('external_data_status', 'unknown')}"),
                    (taxonomy.get("needs_review"), "taxonomy_needs_review"),
                    (runtime.get("network") != "disabled", "network_not_disabled"),
                )
                if failed
            ],
            "runtime_profile_id": runtime.get("runtime_profile_id"),
        })
    write_jsonl(Path(args.output), results, key="source_id")
    print(json.dumps({"qualified": sum(row["admitted"] for row in results), "total": len(results)}))
    return 0


def _cmd_compose(args: argparse.Namespace) -> int:
    classified = read_jsonl(Path(args.classified))
    qualification = read_jsonl(Path(args.qualification))
    output = Path(args.output_package)
    plan_path = Path(args.plan)
    plan = load_json(plan_path) if plan_path.is_file() else _default_stream_plan()
    codebook = load_json(Path(args.codebook))
    groups = _selected_task_groups(classified, qualification)
    used_source_ids: set[str] = set()
    selected_by_stream: dict[str, list[dict[str, Any]]] = {}
    instances = int(plan.get("instances_per_capability", 4))
    for stream in plan.get("streams", []):
        stream_id = str(stream["stream_id"])
        rows: list[dict[str, Any]] = []
        for benchmark_id, capability_id in stream["benchmark_capabilities"]:
            available = [
                row for row in groups[(str(benchmark_id), str(capability_id))]
                if row["source_id"] not in used_source_ids
            ]
            if len(available) < instances:
                raise ValueError(
                    f"{stream_id}: not enough tasks for {benchmark_id}/{capability_id}: "
                    f"{len(available)} < {instances}"
                )
            chosen = available[:instances]
            used_source_ids.update(str(row["source_id"]) for row in chosen)
            rows.extend(chosen)
        selected_by_stream[stream_id] = rows

    if output.exists():
        for child in ("streams", "selections", "contract"):
            target = output / child
            if target.exists():
                shutil.rmtree(target)
        for child in (
            "manifest.json", "pools.json", "panels.json", "taxonomy.json",
            "runtime_profiles.json", "source_tasks.jsonl", "source_qualification.jsonl",
            "stream_plan.json", "README.md", "RUNTIME_HANDOFF.md",
        ):
            target = output / child
            if target.exists():
                target.unlink()
    output.mkdir(parents=True, exist_ok=True)
    _copy_contract(REPO_ROOT / "benchmarks/current/L2_Demo_V4/contract", output)
    write_json(output / "stream_plan.json", plan)
    replace_jsonl(output / "source_tasks.jsonl", classified, key="source_id")
    replace_jsonl(output / "source_qualification.jsonl", qualification, key="source_id")

    runtime_profiles = _runtime_profiles()
    write_json(output / "runtime_profiles.json", runtime_profiles)
    taxonomy = {
        "schema_version": "l2_taxonomy/1.0",
        "taxonomy_version": TAXONOMY_VERSION,
        "codebook_path": "capability_codebook.json",
        "codebook_sha256": _sha256(output / "capability_codebook.json") if (output / "capability_codebook.json").is_file() else None,
        "capabilities": codebook.get("capabilities", {}),
    }
    write_json(output / "taxonomy.json", taxonomy)

    pool_streams: dict[str, list[str]] = defaultdict(list)
    stream_entries = []
    total_turns = 0
    for stream in plan["streams"]:
        stream_id = str(stream["stream_id"])
        stream_dir = output / "streams" / stream_id
        (stream_dir / "agent_workspace").mkdir(parents=True, exist_ok=True)
        _write_text(
            stream_dir / "agent_workspace" / "README.md",
            "Task-local seed workspace for Tri-EvoBench L2_Demo_V5.\n",
        )
        capability_ids = [str(item[1]) for item in stream["benchmark_capabilities"]]
        schedule = role_schedule(capability_ids, instances)
        by_capability: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in selected_by_stream[stream_id]:
            by_capability[str(row["normalized_taxonomy"]["capability_id"])].append(row)
        consumed: dict[str, int] = defaultdict(int)
        parent_turn_ids: dict[str, list[str]] = defaultdict(list)
        turn_ids: list[str] = []
        summaries = []
        for item in schedule:
            capability_id = str(item["capability_id"])
            row = by_capability[capability_id][consumed[capability_id]]
            consumed[capability_id] += 1
            turn_id = f"turn{int(item['position'])}"
            turn_ids.append(turn_id)
            role = str(item["sequence_role"])
            probe_spec = None
            if role == "probe":
                probe_spec = {
                    "target_capability_id": capability_id,
                    "expected_transfer_from_turn_ids": list(parent_turn_ids[capability_id]),
                    "interference_capability_ids": list(item["interference_capability_ids"]),
                    "claim_scope": (
                        "within_benchmark_retention"
                        if stream["pool_type"] == "same_benchmark"
                        else "cross_benchmark_retention_under_runtime_shift"
                    ),
                    "held_out_source_task_id": row["source"]["source_task_id"],
                }
            task = _compose_turn_task(
                stream_id=stream_id,
                turn_id=turn_id,
                turn_index=int(item["position"]),
                role=role,
                row=row,
                codebook=codebook,
                parent_turn_ids=list(parent_turn_ids[capability_id]),
                probe_spec=probe_spec,
            )
            turn_dir = stream_dir / turn_id
            write_json(turn_dir / "assets" / "source_task.json", {
                "source_id": row["source_id"],
                "source": row["source"],
                "benchmark_native_labels": row.get("benchmark_native_labels", {}),
                "normalized_taxonomy": row.get("normalized_taxonomy", {}),
                "prompt_sha256": hashlib.sha256(str(row.get("prompt", "")).encode("utf-8")).hexdigest(),
            })
            _write_text(turn_dir / "verifier.py", GENERIC_V5_VERIFIER)
            write_json(turn_dir / "task.json", task)
            parent_turn_ids[capability_id].append(turn_id)
            summaries.append({
                "turn_id": turn_id,
                "turn_index": int(item["position"]),
                "sequence_role": role,
                "capability_id": capability_id,
                "runtime_profile_id": task["runtime_profile_id"],
                "source_id": row["source_id"],
            })
        capabilities = []
        for benchmark_id, capability_id in stream["benchmark_capabilities"]:
            spec = _capability_spec(codebook, str(capability_id))
            capabilities.append({
                "capability_id": str(capability_id),
                "domain_id": str(spec["domain_id"]),
                "task_type_ids": list(spec.get("task_type_ids", [])),
                "source_benchmark": str(benchmark_id),
            })
        stream_manifest = {
            "schema_version": "l2_taskstream/1.2",
            "stream_id": stream_id,
            "directory": stream_id,
            "profile": "v5_heterogeneous_short_stream",
            "theme": "heterogeneous capability retention",
            "turn_count": len(turn_ids),
            "task_layout": "turn_directories",
            "agent_workspace": "agent_workspace",
            "workspace_persistence": "task_local",
            "turn_type_counts": dict(Counter(item["sequence_role"] for item in schedule)),
            "fact_thread_count": 0,
            "stream_design": {
                "pool_id": stream["pool_id"],
                "pool_type": stream["pool_type"],
                "composition_policy": (
                    "three_distinct_capabilities_one_benchmark"
                    if stream["pool_type"] == "same_benchmark"
                    else "three_capabilities_three_benchmarks"
                ),
            },
            "capabilities": capabilities,
            "ordering": {
                "method": "round_robin_capability_interleave",
                "seed": int(plan.get("seed", COMPOSITION_SEED)),
                "turn_order": turn_ids,
                "note": "A-L,B-L,C-L then applications then delayed probes.",
            },
            "execution_requirements": {
                "network": "disabled",
                "path_scope": "agent_workspace",
                "max_interactions": 3,
                "max_tool_calls_per_interaction": 20,
                "turn_timeout_seconds": 1200,
                "max_output_tokens": 12000,
            },
            "turns": summaries,
            "selection_provenance": {
                "selector": "tools/l2_data_pipeline.py compose",
                "selector_version": "l2-v5-compose/1.0",
                "seed": int(plan.get("seed", COMPOSITION_SEED)),
                "taxonomy_version": TAXONOMY_VERSION,
                "prompt_version": PROMPT_VERSION,
                "plan_path": str(plan_path),
                "selection_policy": "qualified unique source ids, fixed plan, deterministic source_id order",
            },
        }
        write_json(stream_dir / "stream.json", stream_manifest)
        write_json(output / "selections" / f"{stream_id}.json", {
            "schema_version": "l2_selection/1.0",
            "stream_id": stream_id,
            "pool_id": stream["pool_id"],
            "source_ids": [row["source_id"] for row in selected_by_stream[stream_id]],
            "source_hashes": [row["source"]["source_hash"] for row in selected_by_stream[stream_id]],
            "seed": int(plan.get("seed", COMPOSITION_SEED)),
        })
        pool_streams[str(stream["pool_id"])].append(stream_id)
        stream_entries.append({
            "stream_id": stream_id,
            "directory": stream_id,
            "turn_count": len(turn_ids),
            "path": f"streams/{stream_id}/stream.json",
            "pool_id": stream["pool_id"],
        })
        total_turns += len(turn_ids)

    pools = {
        "schema_version": "l2_pools/1.0",
        "pools": [
            {
                "pool_id": "intra_benchmark_multi_capability",
                "pool_type": "same_benchmark",
                "stream_ids": pool_streams["intra_benchmark_multi_capability"],
                "composition_policy": "three_distinct_capabilities_one_benchmark",
                "license_scopes": ["permissive"],
                "allows_cross_pool_aggregation": False,
            },
            {
                "pool_id": "cross_benchmark_multi_capability",
                "pool_type": "cross_benchmark",
                "stream_ids": pool_streams["cross_benchmark_multi_capability"],
                "composition_policy": "three_capabilities_three_benchmarks",
                "license_scopes": ["permissive"],
                "allows_cross_pool_aggregation": False,
            },
        ],
    }
    panels = {
        "schema_version": "l2_panels/1.0",
        "panels": [
            {
                "panel_id": "intra_primary_permissive",
                "purpose": "primary",
                "pool_id": "intra_benchmark_multi_capability",
                "license_scope": "permissive",
                "stream_ids": pool_streams["intra_benchmark_multi_capability"],
                "allows_cross_panel_aggregation": False,
            },
            {
                "panel_id": "cross_primary_permissive",
                "purpose": "primary",
                "pool_id": "cross_benchmark_multi_capability",
                "license_scope": "permissive",
                "stream_ids": pool_streams["cross_benchmark_multi_capability"],
                "allows_cross_panel_aggregation": False,
            },
        ],
    }
    write_json(output / "pools.json", pools)
    write_json(output / "panels.json", panels)
    manifest = {
        "schema_version": "l2_taskstream/1.2",
        "benchmark": "Tri-EvoBench L2 Demo V5 heterogeneous short streams",
        "aggregation_profile": "l2_evolution_aggregation/1.2",
        "baseline_sha": subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip(),
        "stream_count": len(stream_entries),
        "total_turn_count": total_turns,
        "unique_source_task_count": len(used_source_ids),
        "turn_placement_count": total_turns,
        "streams": stream_entries,
        "pool_manifest": _hash_binding(output / "pools.json", "l2_pools/1.0"),
        "panel_manifest": _hash_binding(output / "panels.json", "l2_panels/1.0"),
        "taxonomy_manifest": _hash_binding(output / "taxonomy.json", "l2_taxonomy/1.0"),
        "runtime_profiles": _hash_binding(output / "runtime_profiles.json", "l2_runtime_profiles/1.0"),
        "source_catalog": _hash_binding(output / "source_tasks.jsonl", "jsonl/source_tasks"),
        "source_qualification": _hash_binding(output / "source_qualification.jsonl", "jsonl/source_qualification"),
        "selection_manifest": _hash_binding(output / "stream_plan.json", "l2_v5_stream_plan/1.0"),
    }
    write_json(output / "manifest.json", manifest)
    _write_text(output / "README.md", "Tri-EvoBench L2_Demo_V5 candidate package.\n")
    _write_text(output / "RUNTIME_HANDOFF.md", "Use l2_compat inspect before running any smoke or matrix.\n")
    print(json.dumps({
        "package": str(output),
        "streams": len(stream_entries),
        "turns": total_turns,
        "unique_source_task_count": len(used_source_ids),
    }, ensure_ascii=False))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from trievo_bench.l2_compat import inspect_l2_package
    inventory = inspect_l2_package(Path(args.package))
    report = {
        "schema_version": "l2_v5_validation/1.0",
        "stream_count": len(inventory),
        "turn_count": sum(item.episode_count for item in inventory),
        "streams": [item.__dict__ for item in inventory],
    }
    write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _cmd_compose_semantic(args: argparse.Namespace) -> int:
    result = compose_package(
        Path(args.classified),
        Path(args.qualification),
        Path(args.plan),
        Path(args.output_package),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    discover_parser.add_argument("--output", default="$build/l2_v5/source_tasks.jsonl")
    discover_parser.add_argument("--checkout-report", default="$build/l2_v5/source_checkouts.json")
    discover_parser.set_defaults(func=_cmd_discover)

    classify_parser = subparsers.add_parser("classify")
    classify_parser.add_argument("--input", required=True)
    classify_parser.add_argument("--codebook", required=True)
    classify_parser.add_argument("--output", required=True)
    classify_parser.add_argument("--cache-dir", required=True)
    classify_parser.add_argument("--model", default="deepseek-chat")
    classify_parser.add_argument("--api-base")
    classify_parser.add_argument("--dry-run", action="store_true")
    classify_parser.add_argument("--max-items", type=int)
    classify_parser.add_argument("--llm-source-ids")
    classify_parser.set_defaults(func=_cmd_classify)

    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("--input", required=True)
    review_parser.add_argument("--gold", required=True)
    review_parser.add_argument("--output", required=True)
    review_parser.add_argument("--metrics", required=True)
    review_parser.set_defaults(func=_cmd_review)

    qualify_parser = subparsers.add_parser("qualify")
    qualify_parser.add_argument("--input", required=True)
    qualify_parser.add_argument("--output", required=True)
    qualify_parser.set_defaults(func=_cmd_qualify)

    compose_parser = subparsers.add_parser("compose")
    compose_parser.add_argument("--classified", required=True)
    compose_parser.add_argument("--qualification", required=True)
    compose_parser.add_argument("--plan", required=True)
    compose_parser.add_argument("--output-package", required=True)
    compose_parser.add_argument("--codebook")
    compose_parser.set_defaults(func=_cmd_compose_semantic)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--package", required=True)
    validate_parser.add_argument("--output", required=True)
    validate_parser.set_defaults(func=_cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
