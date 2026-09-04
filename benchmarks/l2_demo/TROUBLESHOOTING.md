# Troubleshooting

Start with:

```bash
python demo.py check
python demo.py test
```

## Common errors

### `ensurepip is not available` while creating `.venv`

Install the OS virtual-environment package (for example,
`sudo apt install python3-venv` on Ubuntu), or use the recommended uv startup
path instead. This is a Python installation issue, not a Stream failure.

### `L2_API_BASE` or `L2_API_KEY` is required

Only API Stream runs need credentials. Export them in the current shell; never
put the key in a file, command argument, report, or ZIP.

### `400 Upstream request failed`

The compatible provider has returned this message for transient upstream
availability failures. The client retries this exact signature with bounded
backoff. Ordinary malformed-request 400 errors are not retried. If all retries
fail, the active Episode is recorded as `error` in `report.json`; rerun the
whole Stream later to get a fresh Workspace.

### `APIConnectionError`, `Network is unreachable`, or timeout

Check network and Base URL first. Transport/API retries happen inside one
model request and do **not** consume the Episode's three-Interaction budget. A
model answer that fails or times out in the verifier does consume an
Interaction and receives bounded retry feedback.

### `add --allow-unsafe-code-execution`

Four Streams execute model-generated Python locally. Read `RELEASE.md`, use a
trusted disposable machine, and add the flag explicitly. This demo is not a
container Sandbox.

### `uses the native browser smoke`

SkillLearnBench and SkillsBench are file/browser verifier packages, not full
text-model terminal-Agent Flows. Use:

```bash
python demo.py smoke skilllearn_stock_data_visualization_001
```

If Chromium is missing in a pip environment, run:

```bash
python -m playwright install chromium
```

If the oracle reports `npm: command not found`, install Node.js/npm and rerun
the smoke. npm is used only to build the browser fixture.

### scikit-learn argument or solver errors

Earlier low-reasoning AIRS answers used deprecated `LogisticRegression`
arguments or an incompatible solver. The Episode request now states the
compatibility constraint. Do not reintroduce deprecated `multi_class`
arguments; choose a solver that supports the number of classes.

### `report.json` says `success: false` while an Episode is `running`

That is an in-progress report, not a final failure. Final Episodes become
`completed` or `error`, and `finished_at` is written when the run exits.

## Exit codes

- `0`: command or Stream succeeded.
- `1`: release check/test/run failed, or a Stream finished below threshold.
- `2`: invalid command-line usage from Python `argparse`.

Run directories are retained for diagnosis. A new invocation always creates a
new UUID Workspace and never resumes an older run.
