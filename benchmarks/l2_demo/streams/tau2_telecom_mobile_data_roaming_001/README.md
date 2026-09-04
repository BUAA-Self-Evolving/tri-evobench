# tau2 Telecom mobile-data/roaming Stream

This three-Episode pilot materializes the first three Telecom roaming siblings
from `data/tau2/domains/telecom/tasks_small.json#/0`, `#/1`, and `#/2` at
revision `a2c024725189473d2d7cea3a5cfdbcc67478e41f`. Their personas and initial
roaming states differ, while all three remain inside the narrow “Mobile
Data/Slow Internet Issues” workflow.

Each Episode preserves its own native initialization actions and terminal
assertions. The native score uses only `ENV_ASSERTION`; reference actions are
not independently scored because `ACTION` is absent from `reward_basis`. The
adapter therefore evaluates replayed terminal state rather than checking
whether a particular action name merely appears.

The common `TextModel` returns text, while the upstream benchmark normally obtains tool calls from an interactive half-duplex dialogue. This pilot uses a strict `tau2-telecom-trajectory-0.1` JSON response containing an assistant message and ordered user-tool calls. The package-local verifier reconstructs the selected item's relevant initial state, replays the calls with native toggle/read semantics, then applies the native environment assertions. This makes the item deterministic and self-contained without depending on `third_party/src/tau2-bench` at runtime.

Agent-visible content stays in `episode.json.agent_view`. The exact native item, initialization actions, assertions, and known-correct trajectory stay under `evaluation_assets/` and must not be mounted into the Agent Workspace. The Stream owns one persistent Workspace, uses at most three Interactions, stops on success, and takes the latest Interaction as the terminal Episode result.

The adapter deliberately implements only the state and user tools reachable and score-relevant for this item. It does not reproduce tau2's live LLM User Simulator, voice path, full Telecom database, or unrelated tools, and it does not invent `ACTION` or `COMMUNICATE` scoring. A future production integration can replace the serialized trajectory with the native orchestrator while retaining the same item provenance and terminal reward semantics.

The upstream repository and copied task material are MIT-licensed; `SOURCE_LICENSE.txt` preserves its license notice.
