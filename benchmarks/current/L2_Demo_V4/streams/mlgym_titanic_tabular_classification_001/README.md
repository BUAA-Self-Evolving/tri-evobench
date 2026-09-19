# mlgym_titanic_tabular_classification_001

L2 Contract V1 demo Stream adapted from MLGym / Titanic. It contains 20 ordered Turns sharing one persistent `agent_workspace`.

Capability relation: One native full Titanic task plus nineteen declared deterministic PassengerId cohorts share the same tabular binary-classification workflow, Workspace artifact contract, and Accuracy verifier.

Every Turn embeds the canonical L2 Judge and post-failure User Simulator prompts and declares a package-local native verifier. Native results are authoritative Judge evidence rather than separate score components. The Task Agent is expected to execute research code in the Workspace; production runs therefore require the Runtime/Baselines security sandbox. The package-local verifier only reads the final CSV artifact.

This is a data handoff package. Harness, Sandbox, models, tools, scheduling, and experiment execution remain Runtime/Baselines responsibilities.
