# L2 Data Demo

This directory contains self-contained data packages and a minimal executable runtime for benchmark-team handoff. The runtime is intentionally a pilot, not a production harness.

## Quick start

Requirements: Python 3.10–3.12. Browser smoke additionally needs Node.js/npm
and Chromium. Run all commands from the extracted `l2_demo/` directory.
[uv](https://docs.astral.sh/uv/) is recommended because it uses the included
lockfile automatically:

```bash
python demo.py list       # list Streams and whether they use API or browser smoke
python demo.py check      # release/contract/credential scan; no API call
python demo.py test       # uv installs locked dependencies and runs local tests
```

Without uv, create a virtual environment and install the compatibility
requirements file. It delegates to `runtime/pyproject.toml`, which remains the
single dependency source of truth:

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python demo.py test
```

Run one API-capable Stream after exporting credentials without putting the key
on the command line:

```bash
export L2_API_BASE='https://provider.example/v1'
read -rsp 'L2 API key: ' L2_API_KEY && echo && export L2_API_KEY

python demo.py run sra_bigcodebench_pandas_dataframe_viz_001 \
  --model gpt-5.6-terra \
  --reasoning-effort medium \
  --allow-unsafe-code-execution
```

Run a browser/file-artifact verifier or rebuild the release ZIP:

```bash
python demo.py smoke skilllearn_stock_data_visualization_001
python demo.py package   # writes ../release/l2_demo-<version>.zip + .sha256
```

`demo.py` is the only supported command entry point. Read [RELEASE.md](RELEASE.md)
before distributing or executing the package; it records the non-commercial,
data-lineage, AppWorld, and unsafe-local-execution boundaries. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) for errors already observed in real
runs.

Lifecycle:

- one directory under `streams/` is one Stream;
- one Stream owns one persistent `workspace/`;
- Episodes in `stream.json.ordering.episode_order` reuse that Workspace;
- a different `stream_id` starts a different Workspace;
- `episode.json` is Agent-visible data;
- `evaluation.json` and `evaluation_assets/` are benchmark-private.

The current packages materialize representative native items from six sources:

| Stream | Native item | Executable evidence |
|---|---|---|
| `sra_bigcodebench_pandas_dataframe_viz_001` | two BigCodeBench items sharing gold skill `bigcodebench_094` | real API Flow passed both Episodes |
| `tau2_telecom_mobile_data_roaming_001` | three pinned Telecom roaming/mobile-data siblings | terra and 5.4-low real API PASS |
| `skilllearn_stock_data_visualization_001` | three stock-data-visualization siblings | native Playwright oracle/broken smoke per Episode |
| `skillsbench_data_to_d3_001` | native data-to-d3 plus deterministic JSON/TSV input variants | native Playwright oracle/broken smoke per Episode |
| `airs_sick_textual_entailment_accuracy_001` | native SICK task plus two deterministic hidden-test-slice variants | terra and 5.4-low real API PASS |
| `mlgym_titanic_tabular_classification_001` | native Titanic task plus two deterministic held-out-row variants | terra and 5.4-low real API PASS |

Each package copies the complete adapted request, redistributable inputs, license
lineage, and private evaluation assets; upstream paths remain provenance only.
The SRA pilot covers end-task application score rather than retrieval-stage
metrics. SkillLearnBench and SkillsBench are highly lineage-similar adapters,
not evidence of two independent task distributions.

`runtime/` supplies the Stream runner, OpenAI-compatible model client, bounded three-Interaction loop, deterministic retry messages, learner-memory update, persistent `.l2_state` protocol, and project-owned verifier dispatch. It inlines every declared public request, tool schema, and text asset into the model input; a package-relative asset is never only an unusable pointer. State under `workspace/.l2_state/` persists across Episodes in one Stream. Generated code executes locally only after an explicit unsafe opt-in; there is no security-grade container Sandbox in this demo.

The API adapter distinguishes transport/provider retries from benchmark
Interactions. It retries only explicit transient statuses and DragTokens'
`400 Upstream request failed` signature with bounded backoff; ordinary 400
contract errors fail immediately. Successful model-response JSON records
`api_retry_count`, while `report.json` is flushed at `running`, `completed`, and
`error` lifecycle points so an interrupted run remains diagnosable.

AppWorld is intentionally not represented by a pointer-only or plaintext package.
Its official protected bundle requires public redistributions and derivatives to
remain encrypted; the project owner must explicitly authorize any future encrypted
adapter. No AppWorld protected plaintext is included here.

## Run isolation and retention

Every invocation creates `runs/<stream_id>/<timestamp>-<uuid>/`, copies the
immutable Stream `workspace/` into that directory, and initializes a new
`.l2_state/`. It never resumes or copies a previous run. Episodes in that one
run share the copied Workspace and learner memory; a different invocation gets
a different copy.

Run directories and JSON reports are retained intentionally for audit and are
not deleted automatically. Source-specific generated-code verifiers use an
additional temporary Workspace per Interaction; that temporary directory is
deleted when verification finishes, so generated-code filesystem side effects
do not currently persist into the next Interaction. This pilot therefore proves
persistent declared learner state, but it is not yet a general terminal-agent
Workspace whose arbitrary filesystem mutations persist across Interactions.
