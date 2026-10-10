# L2 V5 rebuild: task pool format

The rebuilt V5 package is composed from a task pool. Each source builder
(SkillLearnBench/SkillsBench, AutoResearch, tau2, SR-Agents) writes one
directory per admitted native task; `tools/l2_v5_compose.py` copies pool tasks
into self-contained stream directories. The runtime only sees the composed
package.

Pool root on PHAI: `/data_storage/yl_test/lgx/data-1/code/_datasets/tri-evobench/l2v5_pool/`
(one subdirectory per builder, then one directory per task, named by the
source id with `:` and `/` replaced by `__`).

## Directory layout

```
<pool>/<builder>/<task_dir>/
  pool_task.json          # metadata below
  workspace_seed/         # agent-visible files; composer copies them to
                          # /workspace/tasks/<turn_id>/ (may be empty)
  evaluation_assets/      # private; never shown to the agent
    source_verifier.py    # def verify(submission, turn_dir) -> dict (see below)
    ...                   # native tests, gold labels, evaluate scripts
  fixtures/known_correct/ # a submission that must pass (directory, file, or response.txt)
  fixtures/known_broken/  # a submission that must fail or be invalid
```

## pool_task.json

```json
{
  "schema_version": "l2_v5_pool_task/1.0",
  "source_id": "<benchmark_id>:<source_task_id>",
  "source": {"benchmark_id": "...", "benchmark_revision": "<git sha>",
             "source_task_id": "...", "source_path": "...",
             "source_hash": "<sha256 of canonical native task content>"},
  "prompt": "<agent-visible instruction: upstream text, verbatim except path mapping>",
  "benchmark_native_labels": {"...": "upstream labels, unchanged"},
  "capability_hint": {"capability_id": "...", "domain_id": "...", "task_type_ids": ["..."],
                      "rationale": "..."},
  "runtime": {"runtime_profile_id": "...", "requires_gpu": false,
              "turn_timeout_seconds": 1800, "network": "disabled",
              "task_image": "<docker tag>", "task_image_digest": "sha256:..."},
  "submission": {"mode": "workspace_artifact | final_response",
                 "payload_kind": "directory | file | python_source | json_trajectory",
                 "relative_path": "<path inside the task directory, or null for final_response>"},
  "evaluation_profile": "general | auto_research",
  "metric_profile": null,
  "qualification": {"image_digest": "sha256:...",
                    "known_correct": {"status": "pass", "score": 1.0},
                    "known_broken": {"status": "fail", "score": 0.0},
                    "admitted": true, "notes": []}
}
```

For `auto_research`, `metric_profile` is:

```json
{"metric_name": "...", "raw_direction": "maximize | minimize",
 "normalization": {"method": "...", "worst": 0.0, "reference": 0.0, "optimal": 0.0,
                   "source": "upstream metadata path"},
 "performance_bands": {"partial_min_inclusive": 0.0, "full_min_inclusive": 0.0},
 "band_source": "how the bands derive from upstream baseline/SOTA"}
```

## Path mapping

Inside the runtime the persistent Workspace is mounted at `/workspace`; each
Turn's files live in `/workspace/tasks/<turn_id>/`. The composer prepends one
sentence to the native prompt that maps upstream absolute paths (`/root`,
`/app`, `./data`) to that directory. Builders keep the native prompt otherwise
verbatim and record any unavoidable edit in `qualification.notes`.

## source_verifier.verify(submission, turn_dir)

* `submission`: `pathlib.Path` of the Turn task directory (workspace artifact),
  or the final-response string.
* `turn_dir`: the composed Turn directory (`turn_dir / "evaluation_assets"`).
* Return a dict: `{"status": "pass" | "fail", "score": float, "raw_metric": float | null,
  "details": {...}}`. For `general` tasks `score` is 1.0/0.0 from the native
  check. For `auto_research`, `status=pass` means a valid submission was scored,
  `raw_metric` is the native metric, and `score` is the upstream-normalized
  value in [0, 1]; an invalid or missing artifact returns `status=fail, score=0`.
* Runs offline inside the declared task image as root with the network
  disabled; may copy files to `/root`, `/app`, `/tests` for upstream compatibility.

## Explicit-dependency records

`tools/l2_v5_dependency.py` writes `<pool>/dependency/`, whose records add:

* `pool_scope: "explicit_dependency_only"`: the planner uses the record only
  when a stream spec pins it.
* `turn_origin`: `derived_evolution_turn` for a stage-1 preparation task (its
  `source` also has `parent_source_task_ids` and `derivation`; its
  `source_task_id` is `<parent>#stage1_<stage>`), `source_task` for stage 2.
* `dependency` (stage 2 only): `{"requires_source_id": "<stage-1 source_id>",
  "placeholder": "{{dependency_dir}}"}`. The composer replaces the placeholder
  (and `{{turn_dir}}`) with the actual Turn directories and records the stage-1
  Turn in `capability_relation.artifact_dependency_turn_ids`. Stage-2 seeds are
  empty: their inputs exist only in the stage-1 Turn directory.

## Qualification

Builders run `known_correct` and `known_broken` through `source_verifier`
inside the declared image (network none) and record the result. Only tasks with
correct=pass and broken=fail are admitted. Docker on PHAI is rootless: collect
results through bind-mounted files, because attached stdout is unreliable.
After composing, `tools/l2_v5_qualify_package.py` repeats the red/green check
for every Turn of the package through its `verifier_cli.py`, one fresh
container per fixture.
