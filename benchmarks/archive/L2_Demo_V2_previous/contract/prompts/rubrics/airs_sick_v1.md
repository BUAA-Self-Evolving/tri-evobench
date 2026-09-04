[BENCHMARK RUBRIC: AIRS_SICK]

Apply this refinement after the AutoResearch domain rubric.

1. The required final artifact is the declared Workspace `submission.csv`; code in the final response is not a submission.
2. The artifact is valid only when it contains exactly one `label` column, has one prediction per hidden SICK test row, and uses the declared label space.
3. Treat raw Accuracy as immutable native evidence. Use only the verifier-provided AIRS normalized score for performance-band selection; do not recompute the AIRS transform in the Judge.
4. Reproducibility evidence should show that a Workspace script or equivalent recorded tool trajectory produced the submitted artifact from the Agent-visible train, validation, and test files.
5. A schema-valid low-accuracy artifact is a completed Interaction with its earned band; only a missing, malformed, or unverifiable artifact may request another Interaction.
