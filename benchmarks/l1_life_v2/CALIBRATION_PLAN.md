# L1-Life calibration and go/no-go plan

L1-Life supports the Agent self-evolution claim only if persistent updates cause measurable future benefit without excessive stale-memory or intrusion errors. Dataset integrity alone is not sufficient.

## Required pilot

Run at least three frozen task-Agent backbones on all 10 stream seeds under Reset/Frozen, Raw-Replay, Evolving, Matched-Search, and Oracle-Update. Keep model, tool permissions, per-turn rollout budget, context policy, and judge configuration identical across conditions.

## Go/no-go gates

1. Reset/Frozen mean turn success must lie between 0.20 and 0.80; otherwise revise floor/ceiling tasks before adding data.
2. Oracle-Update must improve at least 0.15 absolute over Reset on memory-dependent turns (`preference_application`, `fact_qa`, `fact_dependent_tool`, and `long_delay_replay`).
3. Evolving must show positive mean gain over Reset on at least 7 of 10 streams, not only on the pooled mean.
4. Long-delay replay gain must be positive, while distractor intrusion error must not increase by more than 0.05 absolute.
5. At least 95% of generated judge requests must fit the declared reference judge context; the full-context reference track records every overflow as zero and must report it.
6. State-chain audits must show that each persistent turn's `state_before` equals the preceding `state_after`; Reset/Frozen exceptions must match the declared baseline.

## LLM-judge audit

- Double-score a fixed stratified 10% sample covering every stream and turn type.
- Human-review at least 10% of turns plus every source-rubric conflict marked `NA`.
- Report exact agreement on ordinal check scores, mean absolute turn-score difference, `NA` rate, parse-failure rate, and alternate-judge sensitivity.
- A data freeze requires at least 90% exact ordinal agreement or mean absolute turn-score difference no greater than 0.05 on the double-scored sample.
- Judge prompts, model/version, temperature, max output tokens, retry policy, and the audited sample IDs must be frozen in the release manifest.

## Claim boundary

The main paper should report the full learning curve, per-stream bootstrap confidence intervals, worst-stream result, replay retention, stale and intrusion error, state size, and cost. A positive pooled score alone is insufficient evidence of self-evolution. If Oracle-Update fails the 0.15 gate, the task relation or fact renderer must be revised before claiming learnability; if Evolving improves task score but sharply raises intrusion, report it as unstable evolution rather than success.
