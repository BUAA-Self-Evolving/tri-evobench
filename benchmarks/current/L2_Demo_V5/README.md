## L2_Demo_V5

This candidate contains two independent pools of heterogeneous short streams:

| Pool | Streams | Turns | Composition |
|---|---:|---:|---|
| `intra_benchmark_multi_capability` | 5 | 75 | three capabilities from one benchmark |
| `cross_benchmark_multi_capability` | 5 | 75 | three capabilities from three benchmarks |

Each stream has 15 Turns, three learning/application/probe tracks, unique
source-task identities, task-local workspaces, and explicit probe bindings.
The package is `l2_taskstream/1.2`; V4 `1.1` and `1.0` packages remain
unchanged.

### Provenance and validation

`stream_plan.json` is the source-bound selection record. Its SHA-256 is in
`manifest.json`; `source_tasks.jsonl`, `source_qualification.jsonl`,
`taxonomy.json`, `pools.json`, `panels.json`, and `runtime_profiles.json` are
cross-bound by the manifest. The four admitted source families are
SkillLearnBench, SkillsBench, SR-Agents BigCodeBench, and tau2-bench.
The frozen 20-row adjudicated sample reports 1.0 accuracy and macro-F1 for
domain, task type, and capability in each of the upstream-direct (5),
rule-normalized (5), and LLM-filled (10) strata. These are small qualification
strata, not population-level classifier estimates.

Run the following before a runtime smoke:

```bash
python tools/l2_data_pipeline.py validate \
  --package benchmarks/current/L2_Demo_V5 \
  --output $build/L2_Demo_V5_validation.json
python -m trievo_bench.l2_compat \
  --package benchmarks/current/L2_Demo_V5 --inspect
```

The source-native verifier assets are private evaluation inputs. Network is
disabled for task and verifier containers; only the explicitly mounted
learner-state volume may cross Turn boundaries.

The pinned V5 runtime image is
`sha256:aef1eb316941f620ab1241b34b271edec73b83e1a8a743dd8595abe6697dc9fb`.
Package bindings at candidate qualification time were:

- manifest: `016e7bf086f7f8f61f0c4699f86cd86f29770f3fac5085c8c551f1a5210c4f37`
- pools: `5df1f227cc379e4e3f9c9df3062620295ffdc7944c6779b6ed8c8758e92b1521`
- panels: `af0a1a0267ccb89a6ecfd9e93e4d564b1ed4ab625e5893239a620a0bb788dbad`
- runtime profiles: `1b23773e81323dd292a813bae19e8e05efb1e3b739a5718460cc9cd94ea54e74`

Offline red/green qualification covered all 13 admitted capabilities with one
known-correct pass and one deliberately broken fail each. A four-sample
`deepseek-chat` semantic smoke (two Streams per pool, temperature 0) produced
one native-verifier pass and three model failures. Those failures are retained
as smoke evidence; they are not infrastructure failures and were not retried.

AIRS, AppWorld, and MLGym are not admitted because their large or external
datasets/images were unavailable locally. No missing external data was
downloaded for this candidate.

This is a candidate package, not a formal Reset/Frozen/Evolving matrix result.
It does not establish causal self-evolution gain.
