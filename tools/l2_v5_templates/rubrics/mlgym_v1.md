[BENCHMARK RUBRIC: MLGYM]

Apply this refinement after the AutoResearch domain rubric.

1. The required final artifact is the Workspace output named in the task description (a prediction file, a trained-model checkpoint, or a strategy/policy source file, as the upstream MLGym task defines), written inside this Turn's task directory. Code in the final response is not a submission.
2. The artifact is valid only when the upstream MLGym evaluation script accepts it and produces its native metric.
3. Treat the raw upstream metric as immutable native evidence. The verifier-provided score is the declared normalization in `reference_context.normalization`, anchored on the upstream baseline score; use only that value with `metric_profile.performance_bands`.
4. Reproducibility evidence should show that Workspace code or a recorded tool trajectory produced the artifact from the Agent-visible starter code and data within the Turn's compute limits.
5. A valid low-scoring artifact is a completed Interaction with its earned band; only a missing, malformed, or unverifiable artifact may request another Interaction.
