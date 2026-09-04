[BENCHMARK RUBRIC: MLGYM_TITANIC]

Apply this refinement after the AutoResearch domain rubric.

1. The required final artifact is the declared Workspace `submission.csv`; code in the final response is not a submission.
2. The artifact is valid only when it contains `PassengerId` and `Survived`, covers the declared hidden cohort exactly, and can be joined to the private labels by `PassengerId` without missing or duplicate predictions.
3. Treat native Accuracy as immutable evidence and use the verifier-provided normalized score for performance-band selection. For this task the declared normalization is identity on `[0,1]`.
4. Reproducibility evidence should show that a Workspace script or equivalent recorded tool trajectory produced the artifact from the Agent-visible tabular inputs.
5. A schema-valid low-accuracy artifact is a completed Interaction with its earned band; only a missing, malformed, or unverifiable artifact may request another Interaction.
