[BENCHMARK RUBRIC: AIRS_BENCH]

Apply this refinement after the AutoResearch domain rubric.

1. The required final artifact is the Workspace file named in the task description (for AIRS tasks, the prediction file the upstream `evaluate.py` reads, written inside this Turn's task directory). Code or numbers in the final response are not a submission.
2. The artifact is valid only when the upstream AIRS evaluator accepts it: the expected columns or fields, one prediction per hidden test item, and values in the declared label or output space.
3. Treat the raw upstream metric and the verifier-provided AIRS normalized score (anchors in `reference_context.normalization`) as immutable native evidence. They do not affect validity.
4. Reproducibility evidence should show that a Workspace script or recorded tool trajectory produced the artifact from the Agent-visible training, validation, and test inputs within the Turn's compute limits.
5. A schema-valid low-scoring artifact is valid; only a missing, malformed, or unverifiable artifact may request another Interaction.
