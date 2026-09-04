# SkillLearnBench stock visualization L2 pilot

This three-Episode Stream materializes native siblings
`stock-data-visualization-1/2/3` from SkillLearnBench revision
`a0da045a8bf64b8a8ff20730c4d6ef10dc4e2c5b`, covering CSV, JSON, and TSV
input fixtures in one persistent Workspace.

## Package contract

- E001 uses `workspace/data/`; E002/E003 use Episode-namespaced input fixtures
  under `workspace/episodes/` so their artifacts remain distinguishable.
- The adapted public request rewrites `/root/data` and `/root/output` to
  Workspace-relative `data/` and `output/`.
- Native tests and the oracle stay private under `evaluation_assets/`.
- Every Interaction writes to the same Workspace. At most three are allowed;
  the terminal Episode result is the latest Interaction and success stops the
  loop early.

The package-local adapter runs the complete upstream Playwright/pytest suite.
It changes only the output/log paths and allocates a per-run HTTP port. Run its
red/green smoke with:

```bash
cd l2_demo/streams/skilllearn_stock_data_visualization_001
uv run --with pytest --with playwright playwright install chromium
uv run --with pytest --with playwright python episodes/E001/evaluation_assets/smoke_test.py
```

Verified on 2026-08-19: native oracle `PASS`/`1.0`; empty output directory
`FAIL`/`0.0`.

## Boundary

This proves package-local provenance, inputs, scoring, path rewriting, and a
real browser verifier. It does not prove the shared code-only runner can drive a
terminal/browser Agent, does not create a container Sandbox, and does not make
an L2 learning claim from one Episode. Oracle setup uses npm to install pinned
D3 6.7.0. See `SOURCE_DATA_LICENSES.md` before external redistribution.
