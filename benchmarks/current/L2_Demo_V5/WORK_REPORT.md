# L2 Demo V5 Final Work Report

## Goal and boundary

This candidate implements the 2026-09-19 PRD for heterogeneous L2 short
streams. It does not overwrite V3/V4, does not run a formal Reset/Frozen/Evolving
matrix, and does not claim causal self-evolution gain.

## Candidate

| Item | Final value |
|---|---|
| Package | 10 Streams, 150 Turns, 150 unique source tasks |
| Pools | 5 intra-benchmark Streams; 5 cross-benchmark Streams |
| Stream shape | 15 Turns; 3 capabilities; learning/application/delayed probe for each |
| Manifest | `016e7bf086f7f8f61f0c4699f86cd86f29770f3fac5085c8c551f1a5210c4f37` |
| Pools / panels | `5df1f227cc379e4e3f9c9df3062620295ffdc7944c6779b6ed8c8758e92b1521` / `af0a1a0267ccb89a6ecfd9e93e4d564b1ed4ab625e5893239a620a0bb788dbad` |
| Runtime profiles | `1b23773e81323dd292a813bae19e8e05efb1e3b739a5718460cc9cd94ea54e74` |
| Runtime image | `sha256:aef1eb316941f620ab1241b34b271edec73b83e1a8a743dd8595abe6697dc9fb` |

## Classification and construction

`tools/l2_data_pipeline.py` provides discover, classify, review, qualify,
compose, and validate. Upstream taxonomy is preferred; rule normalization and
DeepSeek filling are versioned and reviewable. The frozen codebook and
`adjudicated_gold.jsonl` retain native labels and disagreement notes. On the
fixed 25-row gold sample, upstream-direct (5), rule-normalized (5), and
LLM-filled (10) strata each report 1.0 field accuracy and macro-F1 for domain,
task type, and capability. These are qualification-sample metrics, not
population estimates. The review queue still contains unresolved inventory
records and is not silently treated as qualified data.

The intra pool keeps one benchmark per Stream and varies capabilities within
that benchmark. The cross pool uses three benchmarks per Stream. Every probe
declares its target capability, prior learning/application Turn IDs,
interference capabilities, and held-out source task. AIRS, AppWorld, and MLGym
remain inventory-only because required external data was unavailable locally.

## Contract and runtime decisions

V5 uses `l2_taskstream/1.2`, source and selection provenance, explicit pool and
panel bindings, per-Turn runtime profiles, task-local workspaces, and an
explicit `baseline_state/1.0` learner-state ABI. Task and verifier containers
run with network disabled. Only the declared learner-state volume persists
across Turns; workspace and private evaluator assets are recreated per Turn.
The V5 panel schema accepts pooled panel documents while retaining V4 legacy
panel-document compatibility. Native verifier request/result and submission
contracts remain unchanged.

## Verification

| Check | Result | Boundary |
|---|---|---|
| V5 schema / pipeline validate / inspect | Pass: 174 V5 objects; 10 Streams / 150 Turns | Panels validated with pooled schema; V4 schema and pilot also pass |
| Native verifier qualification | 13/13 capabilities: known-correct pass and deliberate broken fail | Bound to the final immutable image |
| Deterministic intra E2E | `intra_tau2_support_02`, 15/15; stopped after Turn 7 and resumed at Turn 8 | Scripted adapter and native-score fixture; runtime/isolation evidence only |
| Deterministic cross E2E | `cross_offer_retail_filesystem_01`, 15/15; stopped after Turn 7 and resumed at Turn 8 | Same limitation; not model-quality evidence |
| DeepSeek smoke | 4 authorized samples, 1 native pass / 3 model failures | Saved responses replayed offline against final hashes; no new calls or retries |
| Tests | Full suite: 337 passed | Includes V4 regression and V5 field-level failures |

DeepSeek samples use `deepseek-chat`, temperature 0, and retain response,
historical token usage, submission, verifier result, checkpoint, and failure
classification. Replay records explicitly state `live_api_calls: 0`.

## Risks and claims not made

No formal model matrix, baseline ranking, retention delta, negative-transfer
rate, or causal self-evolution result was run. Deterministic fixture success is
not an agent-ability result. Missing external AIRS/AppWorld/MLGym data was not
downloaded. The two requested independent review agents did not remain
read-only and their responses are not used as independent review evidence;
their unauthorized commit was audited, and the final package was revalidated
after the remaining schema/test fixes.

## Reproducibility

Run `tools/l2_data_pipeline.py validate` and
`python -m trievo_bench.l2_compat --package benchmarks/current/L2_Demo_V5
--inspect` before any runtime smoke. Keep pool, panel, manifest, runtime
profile, and image bindings fixed; do not merge pool denominators.
