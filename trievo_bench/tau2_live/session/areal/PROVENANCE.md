# Provenance

Copied from the AReaL τ² integration on PHAI:

- Source: `/data_storage/yl_test/lgx/data-1/code/_worktrees/AReaL/codex-sao-math/examples/tau2/`
- Commit: `979f4221a06ce040a36b7bd1dadd5f8a91324add` (branch `codex/sao-math`)
- License: Apache-2.0 (AReaL)
- Pinned upstream: `sierra-research/tau2-bench@b7ea9074c1cba482b30687fecdb5c8425fd6f619`
  (`OFFICIAL_TAU2_REVISION` in `contracts.py`)

| File | Copied from | Change |
|---|---|---|
| `user_simulator.py` | `user_simulator.py` | `from areal.utils import logging` → `import logging` |
| `benchmark_variant.py` | `benchmark_variant.py` | none |
| `benchmark_fixes.json` | `benchmark_fixes.json` | none |
| `contracts.py` | `contracts.py` | none |
| `runner.py` | `agent.py` (`Tau2Runner` and its helpers) | see the module docstring |

The AReaL decisions that define these semantics are ADR 0001 (official harness),
ADR 0009 (official runner semantics) and ADR 0010 (official and fixed variants) in
that repository's `docs/decisions/`.
