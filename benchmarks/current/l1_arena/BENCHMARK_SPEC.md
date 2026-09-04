# L1-Arena data specification

## Runtime unit

One ClawArena-real scenario maps to one isolated stream. One source round maps to one source `turnK/task.json`. Two deterministic derived distractors are inserted per stream after source rounds 5 and 10, so the release has 377 source turns plus 46 derived turns. Published stream lengths are 16--20 turns.

## Task fields

Every task contains `schema_version`, `stream_id`, `turn_id`, `turn_index`, `turn_type`, `turn_origin`, `sequence_role`, `interaction_arm`, `tag`, `source_basis`, `agent_input`, `user_simulator`, and `evaluation`.

- Source `turn_type` is `workspace_exec` or `dynamic_update_exec`; inserted noise uses `distractor`.
- Source `turn_origin` is `source_task`; noise uses `derived_evolution_turn` and a null source task ID with parent source IDs.
- `source_basis` embeds immutable source identifiers, descriptions, commit provenance, and hashes; it contains no absolute paths.
- `agent_input.messages` projects the source standing system context into an L1-compatible user message, keeps assistant seed context, and ends with the current round question; only `user|assistant` roles and `role/content` fields are emitted.
- `agent_input.agent_workspace` is `agent_workspace/workspace`.

## Persistence

`agent_workspace/workspace` and `agent_workspace/session_store` persist across all turns in a stream. Different streams never share state. A fresh run restores both directories from the published seed content and resets `state/current_state.json` plus `tool_state/current_state.json` from their seed counterparts.

## Dynamic updates

Update payloads are sealed under `evaluation/updates`. `environment_updates` lists only updates introduced at the current turn; `visible_update_ids` and `reset_environment_updates` provide the cumulative exogenous state required after restoring a Reset seed. Workspace updates target the workspace; session updates target the session store. A new session must also be registered in `sessions.json` with its channel and inactive status. Supported actions are `new`, `modify`, `add`, `append`, and `delete`.

Session append updates preserve the target session's existing channel from `sessions.json`; they must not replace it with a generic channel. New sessions use the source-declared channel or a deterministic filename inference only when no indexed value exists.

## Visibility

Agent-visible: `agent_input`, exact pinned Native tool schemas, the `/workspace` mount, and SessionHistory results. The Agent process must not mount the stream parent. Sealed: all of `evaluation`, `facts.json`, checker scripts and feedback, expected effects, update payload locations, GT, and judge output.

## Self-containment

All initial workspaces, initial session stores, referenced update payloads, and verifier scripts are physically included in the stream. Task files do not point back to the downloaded source directory.

## Distractors

Every stream inserts `unrelated_exact_qa` after source round 5 and `one_turn_format_override` after source round 10. Distractors expose no tools, have deterministic exact responses, and require no workspace/session/persistent learner mutation. Their `visible_update_ids` and Reset update set match the surrounding stream state, but they introduce no update. The one-turn override must not become a lasting preference.

## User Simulator

`user_simulator` is required and non-null on all 423 turns. It uses `type=llm_post_failure`, trigger `interaction_unsuccessful_and_budget_remaining`, embedded `l1_user_simulator/1.1`, four public inputs, temperature 0, output schema `l1_user_simulator_output/1.0`, and at most two retry messages after the initial Interaction. All Arena clarification policies are explicitly disabled. Success stops immediately; evaluator errors score null and do not trigger retries; official selection is `latest_only`. Retry text is public transcript but not durable state by default, and one-turn distractor instructions must not persist.

## Bilingual domain tags

Every task has a required `tag` object with taxonomy version, English/Chinese domain, English/Chinese subtag, and `is_noise`. Source tags use `{scenario_topic}.{turn_intent}`; noise uses `unrelated_noise.exact_arithmetic` or `unrelated_noise.one_turn_format_override` with `is_noise=true`. English IDs are stable snake_case; Chinese values are human-readable labels. See `TAG_TAXONOMY_zh.md`.

Source intents are loaded from the frozen 377-entry `taxonomy/tag_intent_gold.json`, not inferred by a release-time regex. The manifest records its SHA-256, and validation requires exact coverage of all source task IDs.

## Interaction workspace transition

Every task declares `workspace_transition.mode=continue_with_patch`: apply environment updates once before Interaction 1; carry the latest workspace, session store, and public transcript into retry Interactions; evaluate state hygiene against the Turn-start snapshot; use the latest valid Interaction workspace at terminal success/failure; discard the Turn copy on evaluator error.
