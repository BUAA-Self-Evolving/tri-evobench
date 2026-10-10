[DOMAIN RUBRIC: AUTO_RESEARCH]

Apply this extension together with the common L2 Judge rules. The common rules have higher authority if any text conflicts.

1. The native verifier has already computed the task's objective continuous metric and its upstream AIRS normalized score. The framework, not the Judge, turns that score into the Turn score: a valid submission scores the normalized value clipped to [0, 1]; an invalid one scores 0.
2. Never estimate, rewrite, rescale, or ignore the native metric, and do not grade how good it is. A low normalized score is not evidence of invalidity.
3. Verify that the required research artifact exists, has the declared schema, was produced in the allowed Workspace, and is supported by observable execution evidence. A fluent report cannot replace a missing or invalid artifact.
4. Score the single `auto_research_validity` check using only `0` or `1` (never `0.5`):
   - `1`: the native verifier accepted the submission, the artifact was produced by observable Workspace execution from Agent-visible inputs, and no material constraint was violated (for example sealed evaluation data, hard-coded test labels, or edits to protected evaluation files);
   - `0`: the submission is missing or invalid, the result is not reproducible from the recorded execution, or it materially violates constraints.
5. Benchmark-specific rubric text may refine artifact validity or reproducibility evidence, but it may not change the output scale, override the native metric, disclose sealed evaluation data, or weaken the common authority rules.
6. A valid low-performing submission ends the Turn with its earned score. Low metric performance alone is not a retry condition and must not trigger another Interaction or User Simulator message.
7. Native verifier errors are evaluation errors, not evidence that the Task Agent earned `0`. Keep verifier/runtime failure distinct from an invalid Agent submission.
