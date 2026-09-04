# skillsbench_data_to_d3_001

L2 Contract V1 demo Stream adapted from SkillsBench. It contains 10 ordered Turns sharing one persistent `agent_workspace`.

Capability relation: One native task plus nine controlled input-format variants require the same D3 stock visualization workflow over CSV, JSON, and TSV inputs while reusing one materialized skill.

Every Turn embeds the canonical L2 Judge and post-failure User Simulator prompts and declares a package-local native verifier. Native results are authoritative Judge evidence rather than separate score components. The package-local verifier executes a browser artifact and requires Runtime/Baselines browser isolation.

This is a data handoff package. Harness, Sandbox, models, tools, scheduling, and experiment execution remain Runtime/Baselines responsibilities.
