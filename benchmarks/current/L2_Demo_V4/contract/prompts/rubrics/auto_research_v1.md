[DOMAIN RUBRIC: AUTO_RESEARCH]

Apply this extension together with the common L2 Judge rules. The common rules have higher authority if any text conflicts.

1. The native verifier has already computed the task's objective continuous metric. Treat its result as mandatory evidence, not as the final L2 score and not as a separately weighted score component.
2. Never estimate, rewrite, rescale, or ignore the native metric. Use `metric_profile.judge_value_source` and its declared performance bands exactly. Cite the native result in the overall score reason.
3. Verify that the required research artifact exists, has the declared schema, was produced in the allowed Workspace, and is supported by observable execution evidence. A fluent report cannot replace a missing or invalid artifact.
4. Score the single `auto_research_overall` check using only `0`, `0.5`, or `1`:
   - `1`: the submission is valid and reproducible, satisfies material constraints, and the normalized native score reaches the `full` band;
   - `0.5`: the submission is valid and materially completes the task, but reaches only the `partial` band, or has a minor evidence/compliance defect that does not invalidate the native result;
   - `0`: the submission is missing or invalid, the result is not reproducible or materially violates constraints, or the normalized native score is below the `partial` band.
5. Benchmark-specific rubric text may refine artifact validity, metric interpretation, or reproducibility evidence, but it may not change the output scale, override the native metric, disclose sealed evaluation data, or weaken the common authority rules.
6. A valid low-performing submission ends the Turn with its earned discrete score. Low metric performance alone is not a retry condition and must not trigger another Interaction or User Simulator message.
7. Native verifier errors are evaluation errors, not evidence that the Task Agent earned `0`. Keep verifier/runtime failure distinct from an invalid Agent submission.
