# Tri-EvoBench independent review brief

Use this brief for changes to the shared L1/L2 Runtime, baseline adapters,
matrix driver, or evaluation protocol. Review the exact PR head and look for
reasons it should not merge. Do not treat a green unit test, image build, or
package inspection as evidence for a model-quality claim.

## Context index

- `README.md`: supported packages, entry points, and operator-facing contract.
- `docs/current-matrix.md`: current L1/L2 execution matrix, conditions, limits,
  outputs, recovery behavior, and documented isolation boundary.
- `docs/baseline-runtime.md`: baseline process protocol, source revisions,
  lifecycle barriers, state ownership, and update semantics.
- `benchmarks/README.md`: package placement, visibility, and data handoff rules.
- `trievo_bench/l2_compat.py`: package inspection and L2 CLI routing.
- `trievo_bench/l2_agent.py`: TaskStream lifecycle, verifier/Judge handoff,
  retry policy, workspace transitions, checkpoints, and report schema.
- `trievo_bench/baselines/`: shared baseline protocol and concrete adapters.
- `tools/run_new_package_matrix.py`: job identity, resume matching, run
  conditions, report acceptance, and matched aggregation emission.
- `.github/workflows/test.yml` and `pyproject.toml`: enforced test command and
  dependency lock boundary.

## Mandatory review dimensions

1. Preserve existing L1 behavior unless the PR explicitly versions a shared
   interface. Check all shared helpers and defaults for L1 regressions.
2. Verify `reset`, `frozen`, and `evolving` against workspace and SUT-state
   ownership; a label in the report is not proof that the behavior occurred.
3. Keep evaluator/runtime errors distinct from scored Agent failures. Confirm
   retry and User Simulator behavior cannot convert infrastructure errors into
   scores.
4. Preserve private evaluator data, API-key handling, bounded feedback, path
   containment, process limits, and network restrictions.
5. Check that V2 and V3 are selected by schema/content rather than directory
   names, and that old invocations retain their documented defaults.
6. Check baseline lifecycle barriers, state rollback, resource accounting,
   source-revision validation, and Docker/local process parity.
7. Check matched aggregation identity, role alignment, complete-bin rules,
   null/error rejection, condition-aware checkpoints, and output separation.
8. Separate source/code evidence, package validation, image/runtime smoke, and
   real model results. Report any unrun layer explicitly.

## Verdict format

List findings by `P0` through `P3`, each with a concrete file/line and failure
scenario. State the exact reviewed commit, tests or runtime evidence inspected,
remaining unverified layers, and one verdict: `PASS` or `CHANGES_REQUIRED`.
Review is dry only when the latest round finds no new issues.
