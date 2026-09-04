# SkillsBench data-to-d3 L2 pilot

This three-Episode Stream materializes native item `data-to-d3` plus
deterministic JSON and TSV input-format variants from SkillsBench revision
`9a1f4dd5f7659f75707435da3ce854b6e48321d1`.

## Package contract

- E001 retains the native 51-file CSV fixture; E002/E003 hold deterministic
  Episode-namespaced JSON/TSV transforms of that same fixture.
- `workspace/.agents/skills/d3-visualization/` materializes the native Agent
  skill rather than pointing into the upstream checkout.
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
cd l2_demo/streams/skillsbench_data_to_d3_001
uv run --with pytest --with playwright playwright install chromium
uv run --with pytest --with playwright python episodes/E001/evaluation_assets/smoke_test.py
```

Verified on 2026-08-19: native oracle `PASS`/`1.0`; empty output directory
`FAIL`/`0.0`.

## Boundary

This proves package-local provenance, inputs, Agent skill, scoring, path
rewriting, and a real browser verifier. It does not prove the shared code-only
runner can drive a terminal/browser Agent, does not create a container Sandbox,
and does not make an L2 learning claim from one Episode. Oracle setup uses npm
to install pinned D3 6.7.0. See `SOURCE_DATA_LICENSES.md` before external
redistribution.

`data-to-d3` and SkillLearnBench `stock-data-visualization-1` have identical
oracle scripts and input fixtures and nearly identical tests. Keep them as two
source-adapter demonstrations, not evidence of two independent task families.
