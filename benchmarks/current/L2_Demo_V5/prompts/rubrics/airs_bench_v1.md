[BENCHMARK RUBRIC: AIRS_BENCH]

Apply this refinement after the AutoResearch domain rubric.

1. The required final artifact is the Workspace file named in the task description (for AIRS tasks, the prediction file the upstream `evaluate.py` reads, written inside this Turn's task directory). Code or numbers in the final response are not a submission.
2. The artifact is valid only when the upstream AIRS evaluator accepts it: the expected columns or fields, one prediction per hidden test item, and values in the declared label or output space.
3. Treat the raw upstream metric as immutable native evidence. The verifier-provided score is the upstream AIRS normalization of that metric (the declared worst, SOTA, and optimal anchors in `reference_context.normalization`); use only that value with `metric_profile.performance_bands`.
4. Reproducibility evidence should show that a Workspace script or recorded tool trajectory produced the artifact from the Agent-visible training, validation, and test inputs within the Turn's compute limits.
5. A schema-valid low-scoring artifact is a completed Interaction with its earned band; only a missing, malformed, or unverifiable artifact may request another Interaction.
