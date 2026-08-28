# L1-Life: self-contained Agent self-evolution FactStream

L1-Life contains **10 isolated persistent streams × 60 ordered turns = 600 turns**. It directly uses 380 of the 405 CL-bench-Life tasks (93.8% coverage); 25 tasks are frozen as a reserve. Each selected source task maps to exactly one `turn_origin=source_task` turn. Another 220 correction, delayed-question, and replay turns derive from earlier source turns without consuming a new source task.

The published dataset is self-contained. It has no `source_records/` directory and no source-file refs. Every turn lives at `stream-N/turnK/task.json` and directly embeds its Agent messages, source and behavioral rubrics, complete structured ground truth, a complete English `evaluation.judge_config.judge_prompt`, expected state, tool call, and workspace effect.

## Scale

| Item | Count |
|---|---:|
| Streams | 10 |
| Turns per stream | 60 |
| Total turns | 600 |
| One-to-one source-task turns | 380 |
| Derived evolution turns | 220 |
| Versioned fact threads | 60 |
| Used source tasks | 380 / 405 |
| Reserved source tasks | 25 |

Per stream: 6 fact disclosures, 6 corrections, 12 preference applications, 12 delayed fact questions, 12 fact-dependent tool turns, 8 distractors, and 4 long-delay replay turns.

## Layout

```text
l1_life_v2/
├── README.md / README_zh.md
├── BENCHMARK_SPEC.md / BENCHMARK_SPEC_zh.md
├── EVALUATION_SPEC.md / EVALUATION_SPEC_zh.md
├── CALIBRATION_PLAN.md
├── LICENSE.md
├── manifest.json
├── tools.json
└── stream-N/
    ├── stream.json
    ├── facts.json
    ├── agent_workspace/
    ├── turn1/task.json
    ├── ...
    └── turn60/task.json
```

## Visibility boundary

The Agent receives only `agent_input.messages`, schemas for `available_tools`, grounded user-simulator replies, and its stream's writable `agent_workspace`. It must not receive `evaluation`, `facts.json`, source rubrics, expected state/tool/workspace values, check weights, or hidden judge output.

Agent-visible messages contain normal task language. They contain neither the source benchmark name nor raw `<|TASK|>` delimiters.

## Persistent workspace

Each stream owns exactly one workspace. Its 60 turns reuse that workspace; workspaces are never shared across streams. Seed state is immutable. Evolving-like conditions carry current state forward, Reset restores seed state at its declared boundary, and Frozen rejects persistent writes.

File turns form a real dependency chain: create, append, localized edit, append, rename, change the default workspace, create in the new workspace, and edit using the latest project label. Evaluation checks actual tool calls and workspace effects rather than textual claims.

## Canonical specifications

- `BENCHMARK_SPEC.md`: data schema, fields, visibility, workspace, and invariants.
- `EVALUATION_SPEC.md`: LLM-as-judge scoring and aggregation.
- `BASELINE_EVALUATION_GUIDE_zh.md`: end-to-end implementation guide for baseline/runtime engineers.
- `CALIBRATION_PLAN.md`: pilot gates required before making the self-evolution claim.

Build and validate with `scripts/build_l1_life_v2.py --check`.
