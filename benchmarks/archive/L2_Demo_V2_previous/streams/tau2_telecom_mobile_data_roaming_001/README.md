# tau2_telecom_mobile_data_roaming_001

L2 Contract V1 demo Stream adapted from tau2-bench Telecom. It contains 10 ordered Turns sharing one persistent `agent_workspace`.

Capability relation: Ten native Telecom troubleshooting tasks share mobile-data/no-service diagnosis and repair procedures while varying phone-side and carrier-side state.

Every Turn embeds the canonical L2 Judge and post-failure User Simulator prompts and declares a package-local native verifier. Native results are authoritative Judge evidence rather than separate score components. The package-local native verifier deterministically replays the declared trajectory against materialized Telecom state and does not require the upstream checkout.

This is a data handoff package. Harness, Sandbox, models, tools, scheduling, and experiment execution remain Runtime/Baselines responsibilities.
