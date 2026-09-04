# sra_bigcodebench_pandas_dataframe_viz_001

L2 Contract V1 demo Stream adapted from SRA-Bench / SR-Agents. It contains 100 ordered Turns sharing one persistent `agent_workspace`.

Capability relation: One hundred selected native BigCodeBench items share pandas skill bigcodebench_094 plus a Matplotlib or Seaborn visualization skill and require a local, self-contained DataFrame visualization workflow.

Every Turn embeds the canonical L2 Judge and post-failure User Simulator prompts and declares a package-local native verifier. Native results are authoritative Judge evidence rather than separate score components. The package-local native verifier executes generated Python. Production runs therefore require the Runtime/Baselines security sandbox; the repository red/green smoke runs only trusted fixtures. The SRA Turns intentionally have no declared tools or input assets because the native task is final-response code generation, not Workspace artifact creation.

This is a data handoff package. Harness, Sandbox, models, tools, scheduling, and experiment execution remain Runtime/Baselines responsibilities.
