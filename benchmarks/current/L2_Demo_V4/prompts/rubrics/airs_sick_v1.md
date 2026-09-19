[BENCHMARK RUBRIC: AIRS_SICK]

Apply this refinement after the AutoResearch domain rubric.

1. The required final artifact is the declared Workspace `submission.csv`; code in the final response is not a submission.
2. The artifact is valid only when it contains exactly one `label` column, has one prediction per hidden SICK test row, and uses the declared label space.
3. Treat raw Accuracy as immutable native evidence. Use only the verifier-provided value selected by `metric_profile.judge_value_source` for performance-band selection. For the V3 derived folds this value uses declared identity normalization on `[0,1]`; do not apply or claim the official AIRS task-level nonlinear worst/SOTA/optimal transform to a synthetic fold.
4. Reproducibility evidence should show that a Workspace script or equivalent recorded tool trajectory produced the submitted artifact from the Agent-visible train, validation, and test files.
5. A schema-valid low-accuracy artifact is a completed Interaction with its earned band; only a missing, malformed, or unverifiable artifact may request another Interaction.
