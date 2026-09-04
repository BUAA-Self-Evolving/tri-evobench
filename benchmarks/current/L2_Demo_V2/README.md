# L2 Demo V2 — Contract V1 Runtime handoff

This data-only package migrates all six currently deliverable L2 Demo Streams:

- `airs_sick_textual_entailment_accuracy_001`: 2 ordered Turns;
- `mlgym_titanic_tabular_classification_001`: 2 ordered Turns;
- `skilllearn_stock_data_visualization_001`: 10 ordered Turns;
- `skillsbench_data_to_d3_001`: 10 ordered Turns;
- `sra_bigcodebench_pandas_dataframe_viz_001`: 100 ordered Turns;
- `tau2_telecom_mobile_data_roaming_001`: 10 ordered Turns;

The package is self-contained: the bundled Contract V1 documents, schemas, prompts, and Judge reference are under `contract/`. It does not depend on sibling `l2_demo/streams/` or an upstream checkout at runtime. Each Turn carries a package-local native verifier contract and red/green evidence. Future-Turn input files are held under that Turn's `assets/` and introduced only by its declared `workspace_transition`.

The common Judge does all ordinal scoring. Native verifier results are authoritative Judge evidence, not independently weighted score components. General-profile Turns use the demo's `0.6/0.25/0.15` checks and `0.8` threshold. AutoResearch Turns use one `auto_research_overall` check and profile-local performance bands, yielding the same discrete `0/0.5/1` scale without native/Judge arithmetic weighting.

Sequence roles are explicitly labeled for downstream aggregation. Every Stream starts with `learning`; ordinary follow-up Turns are `application`; SRA uses every 10th unique native item as a periodic `probe`, and each shorter Stream uses a small source-specific set of held-out siblings or controlled variants as probes. `replay` and `noise` are schema-valid but intentionally unused in this release because forgetting/retention is not yet a formal metric.

SRA is a response-code benchmark: the Agent submits Python in its final response, so the native items intentionally declare no tools or input files. τ² also uses a final-response carrier whose payload is a declared JSON tool trajectory replayed by its package-local native adapter; it is not a Runtime-captured tool trace. AIRS/MLGym use terminal/filesystem tools to produce Workspace `submission.csv` artifacts. SkillLearnBench/SkillsBench produce browser artifacts. Production runs require Runtime/Baselines security isolation; local red/green smokes use repository-controlled fixtures and do not prove production sandboxing.

Model selection, Harness implementation, Sandbox implementation, tool implementations, feedback projection, and experiment execution remain Runtime/Baselines responsibilities. See `RUNTIME_HANDOFF.md` for the fixed carrier, retry-budget, evaluation-error, lifecycle, and Prompt-routing semantics used by this package.

Validate after extracting the ZIP and entering the package root. The default check validates package hashes, all schemas, Prompt bindings, transitions, and all 134 Tasks; it does not require source-specific benchmark dependencies:

```bash
python3 -m venv .validation-venv
.validation-venv/bin/pip install -r validation-requirements.txt
.validation-venv/bin/python validate_pilot.py
```

The full 280-fixture native smoke is optional for package consumers because its Python/browser dependencies are Runtime-owned. Run it with a prepared verifier environment:

```bash
L2_PILOT_VERIFIER_PYTHON=/path/to/runtime-verifier-python .validation-venv/bin/python validate_pilot.py --native-smoke
```
