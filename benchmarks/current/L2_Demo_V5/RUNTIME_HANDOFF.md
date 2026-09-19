## Runtime handoff

1. Resolve `pool_id` and `panel_id` explicitly. Do not combine the two pool
   denominators.
2. Verify every manifest SHA-256 and the fixed image digest in
   `runtime_profiles.json` before starting a run.
3. Keep the learner process alive inside a Stream, but create and destroy a
   fresh task sandbox and Workspace for every Turn. Set `--network none` for
   both task and verifier containers.
4. Mount only the declared learner-state volume across Turns. Validate the
   `baseline_state/1.0` ABI, serialization version, owner, permissions, and
   checkpoint identity on every resume.
5. Run the native verifier before any LLM Judge. A verifier error is an
   evaluation error, not an agent failure.

V5 forces daemon-owned Docker volume transport. The baseline workspace, Turn
sandbox, and native verifier share that volume through explicit `--mount`
specifications; host synchronization happens only at controlled runtime
boundaries. This avoids nested-Docker bind remapping that can otherwise make a
successful tool call invisible to verification.

The SkillOpt terminal/Python route was qualified with a real offline container:
the adapter executed through `docker exec -w /workspace` in the current Turn
sandbox and the marker synchronized back to the host workspace. Other
baselines still require their own adapter-specific route qualification.

The scripted adapter and deterministic native-score Judge fixture provide
runtime/checkpoint/isolation E2E evidence only. Both
`intra_tau2_support_02` and `cross_offer_retail_filesystem_01` stopped after
Turn 7, resumed from the bound post-Turn checkpoint at Turn 8, and completed
15/15 Turns with the pinned image and Docker-volume Workspace transport.
These runs do not measure model quality or causal evolution gain.

Use `python -m trievo_bench.l2_compat --package benchmarks/current/L2_Demo_V5
--inspect` and the pipeline `validate` command before any model smoke. Formal
Reset/Frozen/Evolving matrices are intentionally outside this candidate.
