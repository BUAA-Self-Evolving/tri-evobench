# L1-Life LLM-as-judge specification

L1-Life uses LLM-as-judge for semantic scoring. The judge produces evidence-based component decisions; the evaluator deterministically recomputes totals from those components.

Every turn embeds its own English `evaluation.judge_config.judge_prompt`. The prompt consumes `evaluation.rubrics.source_rubrics`, `evaluation.rubrics.behavioral_rubrics`, and `evaluation.gt`; no external prompt file is required.

## Reference configuration

The reference track freezes judge prompt, exact model/version, temperature 0, context limit, max output tokens, timeout, retry policy, and parse-failure handling. The current reference model identifier is `gpt-5.1`. Alternate judges must be reported as separate tracks.

## Evidence available to the judge

The judge may inspect sealed checks, final response, complete interaction transcript, tool calls/results, state before/after/diff, expected state, expected tool call, expected workspace effect, sealed historical messages when needed, and inline source rubrics.

A textual claim that memory, a file, or a tool was updated receives no execution credit without matching state, tool, or workspace evidence.

Structured GT defines the verification target, not proof of execution. Fact-QA and correction turns include canonical responses. Open-ended tasks use rubric-defined truth plus exact state/tool/workspace effects instead of fabricated reference prose.

## Score scale

Every check and source rubric receives exactly `1`, `0.5`, `0`, or `null`:

- `1`: fully satisfied with sufficient evidence;
- `0.5`: materially but incompletely satisfied;
- `0`: failed, contradicted, or unsupported;
- `null`: logically inapplicable, with a concrete explanation.

Difficulty or missing evidence is not grounds for null; it scores zero. If source rubrics explicitly conflict, only the conflicting items may be null.

## Source-rubric scoring

For `scoring=mean_source_rubrics`, score each item in `evaluation.rubrics.source_rubrics` separately. The component score is the mean of non-null rubric scores. Do not discard the entire turn because two rubric items conflict. Every turn also has behavioral rubrics mirroring its weighted checks.

## Weighted total

Checks in one turn have weights summing to one. Recompute:

```text
turn_score = Σ(weight_i × score_i) / Σ(non-NA weight_i)
```

Never trust a judge-supplied aggregate over recomputed component scores.

## Turn-type focus

- `fact_disclosure`: source-task success, precise state acquisition, proper clarification, state hygiene.
- `fact_correction`: unique active v2, superseded v1 history, no stale confirmation.
- `preference_application`: source-task success, compatible latest preference, explicit-turn override, no stale use.
- `fact_qa`: exact active value, demonstrated activation, no competing/stale value.
- `fact_dependent_tool`: source answer, exact tool/arguments/count, workspace end state, no collateral effects.
- `distractor`: source answer and one-turn instruction, with no persistent write.
- `long_delay_replay`: source-answer recovery, latest fact retention, stale/intrusion control.

## Judgment output

```json
{
  "stream_id": "l1_life_stream_1",
  "turn_id": "turn5",
  "check_scores": [
    {"check_id": "tool_execution", "score": 1, "reason": "observable evidence"}
  ],
  "source_rubric_scores": [
    {"rubric_index": 1, "score": 0.5, "reason": "observable evidence"}
  ],
  "rationale": "brief overall diagnosis"
}
```

`check_scores` covers all ordinal checks exactly once. `source_rubric_scores` is empty when unused and otherwise covers every one-based rubric index. IDs, duplicates, missing items, and allowed scores must be validated before aggregation.

## Feedback boundary

The learner may receive only failed dimension names and a short observable deficiency. It must not receive expected values, source rubrics, weights, gold facts/tool arguments, state patches, full judge reasoning, or future-turn information.

## Full-context policy

Reference evaluation does not truncate, summarize, reorder, or selectively drop messages. Context overflow is recorded and scores zero while remaining in the denominator. Truncated-context experiments are separately labeled ablations.

## Reporting

Report overall mean, stream macro mean, each stream, each turn type, 10-turn learning bins, replay retention, stale/intrusion error, judge failures, context overflows, and token/tool/wall-clock/state-size cost. The primary gain is `mean(Evolving) - mean(Reset)`, accompanied by per-stream gain, worst stream, bootstrap confidence intervals, and side effects.

## Judge audit

Double-score a fixed stratified 10% sample spanning every stream and turn type; human-review at least 10% plus all rubric-conflict NAs. Report exact ordinal agreement, turn-score MAE, NA rate, parse-failure rate, and alternate-judge sensitivity. Recommended freeze gate: ≥90% exact ordinal agreement or ≤0.05 turn-score MAE.
