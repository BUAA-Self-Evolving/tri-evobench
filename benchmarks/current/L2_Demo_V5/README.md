## L2_Demo_V5

L2 Demo V5 is the `l2_taskstream/1.2` release of the L2 TaskStream benchmark:
18 Streams, 252 Turns, 181 distinct source tasks from six upstream benchmarks.
Every Turn is a native upstream task scored by its native verifier, except for
three derived preparation stages in the explicit-dependency Stream (see below).
It replaces the September heterogeneous V5 candidate, which is kept unchanged in
`benchmarks/archive/L2_Demo_V5_heterogeneous_candidate`.

### Where the package is

This directory is the package descriptor: documentation, the contract, and every
package-level catalog (`manifest.json`, `pools.json`, `panels.json`,
`runtime_profiles.json`, `taxonomy.json`, `capability_codebook.json`,
`stream_plan.json`, `source_tasks.jsonl`, `selections/`). The full package with
`streams/` (2.9 GB: workspace seeds, private evaluation assets, and fixtures) is
too large for Git; several AutoResearch data files exceed GitHub's 100 MB file
limit. It is published as a dataset artifact:

| Artifact | Location (PHAI) |
|---|---|
| Package directory | `/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/L2_Demo_V5/` |
| Archive | `.../_datasets/tri-evobench/L2_Demo_V5.tar` (SHA-256 in `L2_Demo_V5.tar.sha256` and in `benchmarks/README.md`) |
| Per-file digests | `PACKAGE_FILES.sha256` in this directory |

The catalogs here are byte-identical to the package copies, so the manifest
bindings below verify both.

### Composition

Each Stream combines three capabilities. Ordinary Streams place five distinct
source tasks per capability (15 Turns); the AutoResearch and explicit-dependency
Streams place three (9 Turns). The last task of each capability is its probe;
probe and application differ only in position.

| Pool | Streams | Turns | Rule |
|---|---:|---:|---|
| `intra_benchmark_multi_capability` | 10 | 150 | three capabilities of one benchmark (SRA, tau2, SkillLearnBench, SkillsBench) |
| `cross_benchmark_multi_capability` | 5 | 75 | three capabilities from three benchmarks; two Streams use cross-benchmark transfer probes |
| `auto_research_cpu` | 1 | 9 | AIRS-Bench and MLGym tasks that are feasible on CPU |
| `auto_research_cpu_gpu` | 1 | 9 | GPU-class AIRS-Bench and MLGym tasks plus one CPU task |
| `explicit_dependency` | 1 | 9 | staged native tasks whose later stage uses the earlier stage's artifacts |

Pools are aggregated separately. A source task appears at most once per pool.

Schedules (probe gap = Turns between the probe and the previous Turn of the same
capability):

| Schedule | Order | Probe gaps |
|---|---|---|
| blocked | A1–A5, B1–B5, C1–C5 | 0 / 0 / 0 |
| interleaved | A1 B1 C1 A2 B2 C2 … | 2 / 2 / 2 |
| delayed | A1–A4, B1–B4, C1–C4, C5, B5, A5 | 0 / 5 / 10 |

| Stream | Schedule | Turns | Capabilities |
|---|---|---:|---|
| `intra_sra_blocked_01` | blocked | 15 | sra.data_visualization, sra.filesystem_and_io, sra.text_processing |
| `intra_sra_interleaved_01` | interleaved | 15 | sra.dataframe_analysis, sra.numerical_and_ml, sra.web_and_network |
| `intra_sra_delayed_01` | delayed | 15 | sra.data_visualization, sra.dataframe_analysis, sra.encoding_and_hashing |
| `intra_tau2_blocked_01` | blocked | 15 | tau2.telecom.mobile_data_troubleshooting, tau2.retail.returns_exchanges, tau2.airline.policy_refusal |
| `intra_tau2_interleaved_01` | interleaved | 15 | tau2.telecom.mms_troubleshooting, tau2.retail.order_modification, tau2.banking.knowledge_procedures |
| `intra_tau2_delayed_01` | delayed | 15 | tau2.telecom.service_troubleshooting, tau2.airline.reservation_changes, tau2.retail.policy_refusal |
| `intra_skilllearn_interleaved_01` | interleaved | 15 | skilllearn.court-form-filling, skilllearn.dbscan-parameter-tuning, skilllearn.stock-data-visualization |
| `intra_skilllearn_delayed_01` | delayed | 15 | skilllearn.offer-letter-generator, skilllearn.video-object-counting, skilllearn.weighted-gdp-calculation |
| `intra_skillsbench_blocked_01` | blocked | 15 | skillsbench.office-white-collar, skillsbench.industrial-physical-systems, skillsbench.natural-science |
| `intra_skillsbench_delayed_01` | delayed | 15 | skillsbench.finance-economics, skillsbench.software-engineering, skillsbench.office-white-collar |
| `cross_blocked_01` | blocked | 15 | skillsbench.office-white-collar, sra.datetime_and_time, tau2.airline.booking_cancellation |
| `cross_interleaved_01` | interleaved | 15 | skilllearn.stock-data-visualization, tau2.telecom.mobile_data_troubleshooting, sra.web_and_network |
| `cross_delayed_01` | delayed | 15 | skilllearn.court-form-filling, sra.numerical_and_ml, tau2.retail.order_modification |
| `cross_transfer_interleaved_01` | interleaved | 15 | sra.dataframe_analysis → skillsbench.finance-economics probe; tau2.banking.knowledge_procedures; skillsbench.software-engineering → sra.text_processing probe |
| `cross_transfer_delayed_01` | delayed | 15 | skilllearn.offer-letter-generator → skillsbench.office-white-collar probe; sra.data_visualization → skilllearn.weighted-gdp-calculation probe; tau2.telecom.mms_troubleshooting |
| `auto_research_cpu_interleaved_01` | interleaved | 9 | ar.tabular_and_text_prediction, ar.sequence_and_math_reasoning, ar.decision_and_game_strategy |
| `auto_research_cpu_gpu_interleaved_01` | interleaved | 9 | ar.tabular_and_text_prediction, ar.vision_and_deep_models, ar.decision_and_game_strategy |
| `dependency_autoresearch_cpu_interleaved_01` | interleaved | 9 | ar.tabular_and_text_prediction, ar.sequence_and_math_reasoning, ar.decision_and_game_strategy |

Turn roles: 99 learning, 99 application, 54 probe. Placements by source:
tau2-bench 70, SR-Agents BigCodeBench 69, SkillLearnBench 45, SkillsBench 41,
AIRS-Bench 14, MLGym 13. The codebook (`capability_codebook.json`) has 38
capabilities and four cross-benchmark transfer groups.

### Workspace

All Streams use `workspace_persistence: stream`. One Workspace persists for the
whole Stream; each Turn's seed is copied to `agent_workspace/tasks/<turn_id>/`
(`continue_with_patch`) without touching earlier Turns' files, and the prompt
starts by naming that directory. Native prompts are otherwise verbatim; the few
unavoidable edits (for example an offline-data sentence where upstream downloads
from the Hugging Face hub) are recorded in each source's `qualification.notes`
in `source_tasks.jsonl`.

### Explicit dependency

`dependency_autoresearch_cpu_interleaved_01` splits three native tasks into two
stages each, followed by a held-out native probe per capability:

| Stage 1 (derived, Turns 1–3) | Stage 2 (native, Turns 4–6) | Probe (Turns 7–9) |
|---|---|---|
| HousePrice: numeric feature tables | train on those tables, `submission.csv` | AIRS SICK accuracy |
| Rideshare: impute missing values without changing observed ones | forecast from the cleaned series | AIRS SolarWeekly MAE |
| Blotto: payoff-estimation harness, checked against 200k-game references | stronger `strategy.py` | MLGym Prisoner's Dilemma |

Stage-1 Turns have `turn_origin: derived_evolution_turn` and a verifier written
for V5 (recorded in `source_basis.derivation`). Stage-2 Turns use the unchanged
native verifier but start with an empty directory: the data, code, and stage-1
artifact exist only in the stage-1 Turn directory, which
`capability_relation.artifact_dependency_turn_ids` names. These Turns cannot
run under the `reset` condition (it rebuilds the Workspace every Turn); V5's
main condition is `evolving`.

### Evaluation

The native verifier runs first; a verifier error is an evaluation error, not an
agent failure. All Turns use `l2_judge/2.0`. General Turns keep V4's three checks
(`task_completion` 0.6, `requirement_and_output_compliance` 0.25,
`evidence_and_side_effect_integrity` 0.15). AutoResearch Turns use one ordinal
check with the AutoResearch domain rubric and an AIRS-Bench or MLGym benchmark
rubric (`prompts/rubrics/`). Their verifier score is the upstream AIRS
normalized score; bands are $a + 0.5(1-a)$ (partial) and $a + 0.8(1-a)$ (full),
where $a$ is the normalized score of the shuffled-gold fixture for AIRS and 0
for MLGym. tau2 Turns submit a JSON trajectory that the verifier replays in the
real tau2 environment and scores with the native DB-state and assertion reward.

### Images

| Image | ID | Used by |
|---|---|---|
| `trievo-l2-v5:0.7-native` | `sha256:7305c4e84e529f05b5e9e472c892aff6d6077b1fff0e95ecd0d820b3c85d9aef` | SRA, SkillLearnBench, SkillsBench (task and verifier) |
| `trievo-l2-v5-tau2:0.1` | `sha256:430e741560dbc48d7d47c6754bd9fa12a2d73b1ad4a3ef24c9b79977eff81e20` | tau2 (task and verifier) |
| `trievo-l2-v5-ar-cpu:0.1` | `sha256:c24b1f0fd2a529efe1479f5adadd735b67641fae820faaf9aa930f90bd069547` | AutoResearch CPU tasks; verifier for all AutoResearch tasks |
| `trievo-l2-v5-ar-gpu:0.1` | `sha256:9c7c2879446a20b2dc9a3b6bd3adc78aa8953062b5e5a8e91c3d7121576f1e5f` | AutoResearch GPU task sandbox |

Dockerfiles are in `sandbox/l2-v5/`. Task and verifier containers run with
network disabled.

### Validation

```bash
export TRIEVO_L2_V5_PACKAGE=/path/to/L2_Demo_V5    # full package
python -m trievo_bench.l2_compat --package "$TRIEVO_L2_V5_PACKAGE" --inspect
python tools/l2_v5_qualify_package.py --package "$TRIEVO_L2_V5_PACKAGE" \
  --scratch /tmp/l2v5-qual --output qual.jsonl
```

Qualification result (2026-10-10, PHAI): all 252 Turns pass red/green; in its
declared verifier image with network disabled, each Turn's `known_correct`
fixture returns `pass` and its `known_broken` fixture returns `fail`. The
DeepSeek smoke runs are listed in `WORK_REPORT.md`.

How the package was built, the decisions behind it, and the known limits are in
`WORK_REPORT.md`; runtime requirements are in `RUNTIME_HANDOFF.md`.
