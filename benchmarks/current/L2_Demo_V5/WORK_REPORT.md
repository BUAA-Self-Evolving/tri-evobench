## L2 Demo V5 work report (2026-10-10)

### What changed from the September V5 candidate

| Topic | September candidate | This release |
|---|---|---|
| Workspace | fresh Workspace per Turn (`task_local`) | one Workspace per Stream; each Turn adds `tasks/<turn_id>/` |
| Sources | 13 capabilities, partly derived variants | native upstream tasks from a qualified pool; derived Turns only in the dependency Stream |
| SkillLearnBench / SkillsBench | V4-derived format and record variants | rebuilt from upstream task directories with upstream oracles and tests |
| tau2 | tool-call sequence compared with the reference | native replay: DB-state and assertion reward in the real tau2 environment |
| AutoResearch | 2 native tasks split into 40 hidden-test folds | 28 distinct native tasks with full test sets; CPU and CPU+GPU Streams |
| Schedule | fixed A→B→C rotation (probe gap always 2) | blocked, interleaved, and delayed schedules (gaps 0, 2, 0/5/10) |
| Transfer | none | two Streams whose probe comes from another benchmark in the same transfer group |
| Explicit dependency | none | one Stream of staged tasks |
| Judge | `l2_judge/1.0` for General Turns | `l2_judge/2.0` for all Turns; General keeps V4's three checks |
| Stream length | 12–20 | 9–20 |

### Build pipeline

All code is under `tools/` on branch `claude/l2-v5-rebuild`. The pool format is
in `docs/l2-v5-rebuild/POOL_FORMAT.md`; pool root on PHAI:
`/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_pool/`.

| Step | Tool | Output |
|---|---|---|
| SRA pool | `l2_v5_sra.py` | 987 of 1140 BigCodeBench tasks admitted |
| tau2 pool | `l2_v5_tau2.py`, `l2_v5_tau2_replay.py`, `l2_v5_tau2_source_verifier.py` | 368 of 369 tasks admitted |
| SkillLearnBench / SkillsBench | `l2_v5_native_sources.py` (prepare, oracle replay), `l2_v5_native_pool.py` | 80 of 187 tasks admitted |
| AutoResearch pool | `l2_v5_autoresearch.py`, `l2_v5_autoresearch_verifier.py` | 28 of 39 tasks admitted (11 CPU, 17 GPU-class) |
| Explicit-dependency stages | `l2_v5_dependency.py` | 3 derived stage-1 tasks, 3 stage-2 records |
| Codebook | `l2_v5_codebook.py` → `l2_v5_codebook.json` | 38 capabilities, 4 transfer groups |
| Plan and compose | `l2_v5_compose.py plan|compose` with `l2_v5_stream_spec.json` | this package |
| Qualification | `l2_v5_qualify_package.py` | 252 / 252 Turns red/green |

Admission requires the upstream oracle or gold submission to pass the native
verifier and a broken submission to fail, both in the pinned image with network
disabled. Main exclusion reasons:

- SRA: the canonical solution fails its own tests in the pinned image (153).
- SkillLearnBench / SkillsBench: no upstream oracle (26), oracle replay failed
  (26), oracle fails the native tests (25), workspace seed over 300 MB (7, all
  `financial-analysis`), tasks that need the provided skill modules
  (`travel-planning`, 5).
- AutoResearch: Titanic (gold labels equal the female→survived rule), Kaggle
  web traffic (degenerate normalization), APPS (verifier dependency unavailable
  on Python 3.11), 3SAT (dataset missing upstream), COCO/FineWeb/HousingPrice
  (data not in the upstream repo), and three variants of included tasks.

Upstream skill libraries are deliberately not given to the agent: the
benchmark measures what the agent learns along the Stream.

### Decisions recorded during the build

- **Explicit dependency scope.** One Stream, three AutoResearch tasks split into
  a derived preparation stage and the native task. Each line ends with a
  held-out native probe of the same capability, so the probe keeps its V5
  meaning. Stage-1 verifiers are written for V5 and labelled derived.
- **Blotto stage 1.** The target opponent plays uniform random allocations, so
  an "opponent profile" would be uninformative (every field averages 20). Stage
  1 instead asks for a payoff-estimation harness, checked against 200,000-game
  reference values (tolerance 0.03). The upstream baseline score is stable at
  about −0.248 ± 0.002 across runs.
- **AutoResearch bands.** Partial $= a + 0.5(1-a)$ and full $= a + 0.8(1-a)$ on
  the AIRS normalized score; $a$ is the shuffled-gold score for AIRS and 0 for
  MLGym (whose worst anchor is the upstream baseline). With the network disabled,
  the agent has no pretrained models, so the bands may be strict.
- **GPU image.** Built on the CPU image, with only torch/torchvision (CUDA 12.4),
  jax (CUDA 12), and xgboost swapped. A full re-resolution of the package set had
  pip backtracking for more than 30 minutes. `JAX_PLATFORMS` is reset to auto,
  because the CPU base pins it to `cpu`.
- **Package delivery.** The full package is a dataset artifact, not a Git
  directory; see `README.md`.

### Bugs found and fixed while qualifying the package

- Workspace submissions were bound to the artifact file (for example
  `submission/trajectory.json`), but tau2 and MLGym RL verifiers expect the Turn
  directory. 73 Turns failed red/green until the composer bound the directory.
- AutoResearch Judge rubrics were not copied into the package; the runtime would
  have failed on the first AutoResearch Judge call. The composer now always ships
  `prompts/rubrics/` and fails if a routed rubric is missing.
- The source catalog carried extra source keys (license) and was keyed by pool
  ID instead of source identity, so `l2_compat --inspect` rejected the
  AutoResearch and dependency Turns.
- The first qualifier ran both fixtures in one container; native verifiers copy
  the submission into `/root`, so `known_broken` inherited `known_correct`'s
  files and passed. Each fixture now runs in a fresh container.

### DeepSeek smoke runs

`deepseek-flash`, thinking disabled, Hermes baseline, `evolving`, rootless
Docker socket. These check that Turns run end to end; they are not model-quality
results.

| Stream | Turns run | Result |
|---|---|---|
| `intra_sra_*` (earlier package build) | 2 | native verifier and Judge ran; scores recorded |
| `intra_skilllearn_interleaved_01` | turn1–turn2 | turn1 0.0 (agent hit `max_tool_calls`, an agent failure); turn2 1.0 |
| `intra_tau2_interleaved_01` | turn1–turn2 | 0.2 and 0.275; stopped at the 3M-token budget (3.4M used) |
| `dependency_autoresearch_cpu_interleaved_01` | turn1–turn4 | turn1 pass (236 feature columns); turn2 0 (agent hit its work limit); turn3 pass (payoff estimates within 0.006 of the references); turn4 first attempt stopped at the 3M-token budget, then resumed from the turn3 checkpoint with an 8M budget: the agent read `tasks/turn1/features/*.csv`, wrote `tasks/turn4/submission.csv`, native R² 0.894 (normalized 0.218, below the 0.5 partial band), Turn score 0, 131k tokens |

### Known limits

- `auto_research_cpu_gpu_interleaved_01` needs GPU passthrough, which the
  runtime does not provide yet. Its GPU tasks were qualified with CPU verifiers
  but have not been solved on a GPU.
- Task containers are capped at 4 GB RAM and 2 CPUs; some AutoResearch CPU tasks
  are slow at that size.
- MLGym game scores come from 10,000 Monte-Carlo games against an unseeded
  opponent, so repeated evaluations differ by about ±0.003.
- The paper census and Section 4.2 have not been updated for this release.
