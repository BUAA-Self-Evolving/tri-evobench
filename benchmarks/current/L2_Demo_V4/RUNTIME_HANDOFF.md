# Runtime handoff notes

This package is the only Runtime handoff tree. Do not consume the repository's tracked migration-source `l2_demo/streams/` tree.

## Integration entry points

1. Load `manifest.json`, then follow each `streams[].path`; do not discover Streams or Turns by directory sorting.
2. Load `tools.json` as the declared tool catalog. Concrete tool RPCs remain Runtime-owned.
3. For each Stream, create one Workspace from its `agent_workspace/`, then execute `stream.json.ordering.turn_order` in order.
4. For each Turn, load `turnN/task.json`, apply its `workspace_transition`, expose only `agent_input`, and keep `evaluation` plus `evaluation_assets/` evaluator-private.
5. Freeze the submission declared by `evaluation.submission_contract`, run the required native verifier, then route the declared Judge Prompt. Invoke User Simulator only when the declared continuation condition is satisfied and Interaction budget remains.

The normative lifecycle is in `contract/L2_BENCHMARK_SPEC_zh.md`; evaluation authority, aggregation, retry, and error semantics are in `contract/L2_EVALUATION_SPEC_zh.md`.

## Fixed Contract V1 Pilot semantics

1. Benchmark data semantics give one Stream one persistent `agent_workspace`. `frozen` and `evolving` preserve that Workspace and apply only the current Turn's `workspace_transition`; the explicit `reset` control reconstructs a clean copy from the frozen Stream seed before every Turn.
2. General Tasks use Judge Prompt V1. AutoResearch Tasks use Judge Prompt V2 with the declared Domain and Benchmark Rubric routes. General + Judge V2 is outside this release.
3. `max_generated_messages=2` is cumulative across one Turn. Each User Simulator invocation returns exactly one `next_user_message`, so at most two retry messages follow the initial Interaction.
4. Evaluator/control-plane errors never become Agent failures or scores. After the Runtime's configured retry/repair budget is exhausted, record `evaluation_error`, leave `turn_score=null`, do not invoke User Simulator, and retain raw evidence. Whether the Run stops or continues is a Runtime run-configuration decision.
5. τ² uses `submission_contract.mode=final_response` with `payload_kind=json_tool_trajectory`. The final response is a declared trajectory artifact parsed and deterministically replayed by the package-local native adapter; it is not a Runtime-captured real-tool trace.
6. Native verifier evidence is mandatory and precedes Judge. Runtime must retain native raw/normalized metrics where present. Native evidence is not arithmetically weighted with Judge scores.
7. AutoResearch performance bands are Pilot configuration and require calibration after a real Runtime run; the raw metric and normalization evidence must remain available for recalibration.
8. Reference conditions for experiment aggregation are `reset`, `frozen`, and `evolving`. In L2, `reset` clears SUT persistent state and rebuilds the Workspace from the Stream initial `agent_workspace` plus only the current Turn's `workspace_transition`; `frozen` keeps the Stream Workspace but disallows SUT updates; `evolving` keeps the Stream Workspace and allows SUT updates.
9. Learning curve aggregation uses complete 10-Turn bins. Formal gain requires matched, non-null terminal scores for all three conditions: `mean(evolving) - mean(reset)` is the L1-aligned primary gain, and `mean(evolving) - mean(frozen)` isolates the additional effect of SUT updates while preserving the evolving Workspace.

## Runtime-owned choices intentionally left open

Runtime/Baselines owns Sandbox/container selection, model selection, concrete tool RPC implementations, feedback projection, evaluator retry counts, Run stop/continue policy after `evaluation_error`, scheduling, and execution of experiment aggregation. Those operational choices must be frozen in each run configuration and logs; aggregation results must follow the package's `l2_evolution_aggregation/1.1` semantics.

Some evaluator-private source fixtures retain upstream absolute-path literals as provenance or source semantics. They are not public Runtime path bindings. Runtime paths are only the package-relative bindings declared by each Task and verifier contract.

## Minimum integration smoke

Before broader baseline execution, load at least one General Turn and one AutoResearch Turn end to end. Confirm one persistent Workspace per Stream, package-relative asset binding, native-verifier-before-Judge ordering, private evaluation isolation, and `latest_only` terminal Interaction selection. This smoke demonstrates interface compatibility, not a learning-curve result.
