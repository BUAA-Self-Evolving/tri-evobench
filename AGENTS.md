# Repository Guidelines

## Project Structure & Module Organization

`trievo_bench/` contains the Python runtime, model transport, sandbox logic, L1/L2 compatibility layers, and baseline registry. External-agent integrations live under `baseline/`; keep adapter-specific Dockerfiles in `baseline/adapters/<agent>/`. Benchmark packages belong in `benchmarks/`, JSON examples in `examples/`, schemas in `schemas/`, and operational utilities in `tools/`. Add automated checks to `tests/`; reusable test doubles belong in `tests/fixtures/`. Treat `runs/`, `output/`, `$build/`, and `.tmp/` as generated artifacts unless a result is intentionally being published.

## Build, Test, and Development Commands

Use Python 3.11 or newer:

```bash
python -m venv .venv
python -m pip install -e .
python -m pytest -q
python -m pytest tests/test_l2_compat.py -q
```

The editable install exposes the `trievo-bench` command. Inspect a benchmark before launching a costly run, for example `python -m trievo_bench.l2_compat --package <path> --inspect`. Build the standard sandboxes with:

```bash
docker build -t trievo-ubuntu:22.04 sandbox/ubuntu
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
```

Run focused tests before the full suite. Real evaluations must use Docker and retain summaries, traces, checkpoints, and audit metadata under `runs/`.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation, explicit imports, type hints on public interfaces, and small functions with clear failure messages. Use `snake_case` for modules, functions, variables, and test files; use `PascalCase` for classes and `UPPER_SNAKE_CASE` for constants. Keep protocol and manifest fields backward-compatible. Format JSON with two-space indentation and preserve UTF-8 text.

## Testing Guidelines

Tests use `pytest` and follow `tests/test_<feature>.py` with functions named `test_<behavior>`. Cover success, resume/checkpoint, malformed protocol, and Docker export paths when changing runtime behavior. Never substitute a smoke run for required full-matrix validation. Avoid live API calls in unit tests; use fixtures or fake adapters.

## Commit & Pull Request Guidelines

History uses short imperative subjects such as `Support current L1 Arena...` and `Fix DeepSeek proxy...`. Keep each commit scoped and include tests with behavior changes. Pull requests should describe affected levels/baselines, commands run, dataset or schema changes, resume compatibility, and representative trace paths. Include screenshots only for report or UI changes.

## Security & Agent-Specific Instructions

Never commit API keys or machine-specific absolute paths; use environment variables and portable overrides. Before operating on SSH host `graph`, read `AGENT.md` and `runs/mx24/MONITOR_HANDOFF.md`. Every `sudo` command requires explicit user approval of the exact command list; prefer unprivileged operations and do not use Docker to bypass this rule.
