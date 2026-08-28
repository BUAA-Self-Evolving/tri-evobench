# L1-Life data specification

This document is the canonical machine-semantics contract for L1-Life. The Chinese counterpart is `BENCHMARK_SPEC_zh.md`.

## 1. Runtime unit and layout

- Ten independent streams, sixty ordered turns per stream.
- State, tool effects, artifacts, and learner updates may persist only within a stream.
- Different streams begin from independent empty snapshots and never share a workspace.
- Each turn is stored at `stream-N/turnK/task.json`; there is no consolidated `tasks.json` and no `source_records/` directory.

```text
stream-N/
├── stream.json
├── facts.json
├── agent_workspace/
├── turn1/task.json
├── ...
└── turn60/task.json
```

## 2. Root manifest

| Field | Type | Contract |
|---|---|---|
| `schema_version` | string | `l1_life/2.0` |
| `benchmark` | string | `L1-Life` |
| `source_dataset` | string | Provenance only; never an Agent message |
| `source_dataset_record_count` | integer | 405 |
| `used_source_task_count` | integer | 380 |
| `source_coverage_ratio` | number | 380 / 405 |
| `reserved_source_task_count` | integer | 25 |
| `reserved_source_task_ids` | string[] | Frozen reserve IDs |
| `stream_count` | integer | 10 |
| `turns_per_stream` | integer | 60 |
| `total_turn_count` | integer | 600 |
| `source_task_turn_count` | integer | 380 |
| `derived_evolution_turn_count` | integer | 220 |
| `judge_prompt_version` | string | Embedded judge prompt version |
| `judge_prompt_language` | string | English |
| `gt_version` | string | Structured GT schema version |
| `source_task_ids` | string[] | 380 unique IDs |
| `streams` | object[] | Stream summaries |

Required arithmetic invariants: used + reserved = source total; source turns + derived turns = total turns; streams × turns per stream = total turns.

## 3. Stream manifest

`stream-N/stream.json` contains `schema_version`, `stream_id`, `directory`, `profile`, `theme`, `turn_count`, `task_layout`, `agent_workspace`, `turn_type_counts`, `fact_thread_count`, and an ordered lightweight `turns` index. Every indexed turn must match its directory and task contents.

## 4. Top-level task fields

| Field | Type | Contract |
|---|---|---|
| `schema_version` | string | `l1_life/2.0` |
| `stream_id` | string | Matches stream/facts/workspace |
| `turn_id` | string | `turn1`…`turn60`, matches directory |
| `turn_index` | integer | One-based directory index |
| `turn_type` | string enum | disclosure/correction/application/QA/tool/distractor/replay |
| `turn_origin` | string enum | `source_task` or `derived_evolution_turn` |
| `sequence_role` | string enum | learning/application/probe/noise/replay |
| `interaction_arm` | string | disclosed/interactive/correction/standard |
| `source_basis` | object | Inline provenance; no path refs |
| `agent_input` | object | The sole Agent-visible task entry point |
| `user_simulator` | object/null | Frozen grounded disclosure policy |
| `evaluation` | object | Sealed checks and expected evidence |

## 5. Source basis

For a source turn, `source_task_id` is non-null, `parent_source_task_ids=[]`, and the ID appears as a primary source exactly once. It also stores `source_jsonl_line`, `source_sha256`, and `source_metadata`.

For a derived turn, `source_task_id=null`; `parent_source_task_ids` identifies earlier source turns. A derived turn does not consume another source task.

The published dataset forbids `source_record_refs`, `source_rubrics_from`, absolute source paths, and opaque external source dependencies.

## 6. Agent input and messages

`agent_input` fields:

| Field | Type | Agent-visible | Contract |
|---|---|---|---|
| `agent_workspace` | string | yes | `agent_workspace` |
| `workspace_persistence` | string | yes | Describes within-stream persistence |
| `messages` | message[] | yes | Complete current-turn conversation |
| `available_tools` | string[] | yes | Tool names allowed this turn |

A message contains `role` (`user` or `assistant`) and non-empty UTF-8 `content`. Source turns embed complete source messages and merge the stream overlay into the terminal user message. Memory-only turns contain only the current request. Messages must not leak the source benchmark name, raw `<|TASK|>` delimiters, source refs, rubrics, or expected values.

## 7. User simulator

`user_simulator` is null or contains `policy`, `scripted_reply`, and `grounded_fact_id`. Interactive facts are revealed only after the relevant clarification. The simulator may express sealed truth but may not invent environment facts or judge success.

## 8. Evaluation fields

| Field | Type | Contract |
|---|---|---|
| `type` | string | `llm_as_judge` |
| `protocol` | string | `l1_life_llm_judge_v1` |
| `judge_config` | object | Embedded English prompt and machine contract on every turn |
| `rubrics` | object | Source and behavioral rubric collections |
| `gt` | object | Complete structured ground truth on every turn |
| `checks` | object[] | Weights sum to 1 |
| `expected_state` | object | Target fact/state constraints |
| `expected_tool_call` | object/null | Tool, arguments, call-count rule |
| `expected_workspace_effect` | object/null | Required file/calendar/artifact end state |
| `reference_context` | object/null | Sealed historical messages |
| `feedback_policy` | string | Non-leaking local-feedback rule |

`reference_context` contains `visibility=sealed_evaluator_only`, `messages` (only when historical context is unavailable to the Agent), and `message_source`. Rubrics live in `evaluation.rubrics`.

Every turn embeds `judge_config` with prompt version, reference model, temperature, output budget, literal template engine, score scale, English `judge_prompt`, required inputs, strict JSON output contract, deterministic aggregation, and failure policy. The prompt includes placeholders for turn identity, Agent input/output, transcript, tools/results, state/workspace evidence, reference context, rubrics, checks, and expected evidence.

`evaluation.rubrics.source_rubrics` contains original source rubrics when the turn scores a source answer; otherwise it is empty. `behavioral_rubrics` is present on every turn and mirrors checks exactly (`rubric_id=check_id`, `criterion=instruction`).

Every turn embeds `evaluation.gt` with `gt_version`, status, `answer_gt`, a copy of rubric GT, state GT, tool GT, workspace GT, negative constraints, and notes. Fact-QA and correction turns provide a compact canonical response plus acceptable exact values. Open-ended turns set `canonical_response=null` and provide a non-empty placeholder reason; their answer truth is fully defined by rubrics and structured effects rather than fabricated prose.

Each check contains unique `check_id`, one primary `dimension`, positive `weight`, `scoring` (`ordinal` or `mean_source_rubrics`), and an evidence-based `instruction`. A mean-source check requires non-empty `evaluation.rubrics.source_rubrics`.

## 9. Expected evidence

`expected_state.target_facts[]` may contain `fact_id`, `key`, `value`, `scope`, `supersedes`, and `stale_value`.

`expected_tool_call` contains `tool`, `arguments`, and `must_call_exactly_once`. Tool names must exist in `tools.json` and be exposed in `agent_input.available_tools`.

`expected_workspace_effect` is operation-specific. Common fields include `operation`, `path`, `old_path`, `new_path`, `must_exist_after`, `old_must_exist_after`, `content_must_be_unchanged`, `prior_content_must_be_preserved`, `preserve_unrelated_files`, and `former_workspace_files_must_be_preserved`.

## 10. Fact truth

`facts.json` is sealed evaluator/user-simulator truth. It contains six threads per stream: two preferences, two work facts, and two tool facts. A thread contains stable ID/key/label/kind/scope, v1, v2 with `supersedes`, `exposure_turn`, and later `correction_turn`. It must never be mounted as Agent memory.

## 11. Agent workspace

Each stream has exactly one workspace with:

- `state/seed_state.json` and writable `state/current_state.json`;
- `tool_state/seed_state.json` and writable `tool_state/current_state.json`;
- `docs/`, `artifacts/manifest.json`, and `iterations/manifest.json`.

Seed/current files are identical before a run. Seed is immutable. Tool side effects must be observable in current tool state, real files, or artifact manifests. Writes are confined to the current stream.

## 12. Cross-file invariants

1. Exactly 10 streams and 60 task files per stream.
2. Directory K, `turn_id=turnK`, and `turn_index=K` agree.
3. Stream IDs agree in task, stream, facts, and workspace files.
4. Exactly 380 unique primary source IDs and 220 derived turns with null primary IDs.
5. Every parent source precedes its probe/replay.
6. Exposure precedes correction; correction precedes probes requiring v2.
7. Every check-weight sum equals 1.
8. Every tool exists in `tools.json`.
9. Agent messages contain no sealed data, source refs, dataset name, or raw delimiter.
10. Manifest counts equal a filesystem scan.

## 13. Versioning

Changes to field meaning, visibility, indexing, provenance cardinality, workspace semantics, or score interpretation require a schema-version bump. Documentation-only clarification may retain `l1_life/2.0`, but release metadata should record the revision.
