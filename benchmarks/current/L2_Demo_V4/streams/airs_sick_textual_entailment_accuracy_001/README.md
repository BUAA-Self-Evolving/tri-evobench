# airs_sick_textual_entailment_accuracy_001

L2 Contract V1 demo Stream adapted from AIRS-Bench / SICK. It contains 20 ordered Turns sharing one persistent `agent_workspace`.

Capability relation: Twenty declared deterministic, non-overlapping SICK hidden-test folds share the same label space, Accuracy metric, Workspace artifact contract, and native verifier.

Every Turn embeds the canonical L2 Judge and post-failure User Simulator prompts and declares a package-local native verifier. Native results are authoritative Judge evidence rather than separate score components. The Task Agent is expected to execute research code in the Workspace; production runs therefore require the Runtime/Baselines security sandbox. The package-local verifier only reads the final CSV artifact.

This is a data handoff package. Harness, Sandbox, models, tools, scheduling, and experiment execution remain Runtime/Baselines responsibilities.
