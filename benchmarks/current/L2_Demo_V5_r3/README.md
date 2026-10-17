# L2 Demo V5 r3 descriptor

## Release

Full package: GitHub Release [`l2-demo-v5-r3`](https://github.com/BUAA-Self-Evolving/tri-evobench/releases/tag/l2-demo-v5-r3).

| Asset | Size | SHA-256 |
|---|---:|---|
| `L2_Demo_V5_r3.tar.zst` | 948,976,124 B (unpacks to about 17 GB, 6,912 files) | `29ef75a622771930159b815335c4f57da5bd9142735a8fac3fa7cdc0366f87a5` |
| `PACKAGE_FILES.sha256` | 1,089,277 B | `f7e6463f4ea25ada207f34d1138018374c318c9bd5b177d42a75f90e63e2affa` |

The archive uses zstd long-distance matching, which stores the repeated financial-analysis SEC data once, so `--long=31` is required:

```bash
sha256sum -c SHA256SUMS
zstd -d --long=31 -c L2_Demo_V5_r3.tar.zst | tar -xf -
(cd L2_Demo_V5_r3 && sha256sum --quiet -c ../PACKAGE_FILES.sha256)
python -m trievo_bench.l2_compat --package L2_Demo_V5_r3 --inspect
```

Before publication, a round-trip extraction matched all 6,912 file hashes.

This is a portable review descriptor for the external L2 Demo V5 r3 package: 21 Streams, 297 Turns, and 291 canonical source identities. Implementation `d56034ef` passes 741 tests (31 skipped). All 14 canonical native samples, 60 QA Turns, 75 τ² Turns and fresh tictoc final pass red/green. Qualification is complete: 222 executed pass / 75 static-only, zero coverage errors, 216 executed unique sources. Nine prescribed Turn-1 executions ended, but only eight exercised native/Judge; AutoResearch sample_completed remains false and turn_execution_completed true; this directory is a descriptor, not a runnable package. The full package is published as a GitHub Release (below).

`stream_plan.json`, `capability_codebook.json`, `pools.json`, `panels.json`, `taxonomy.json`, and `selections/` are byte-identical portable copies from the full package. `source_summary.json` exposes source identities, hashes and admission stages without host paths. It does not establish runtime readiness.

`artifact_references.json` records exact hashes and sizes of the authoritative external manifest, runtime profiles and source catalog. Those catalogs contain host-bound image archive/provenance paths. They are referenced without rewriting their contents or silently changing manifest bindings. Obtain the full package and its actual catalogs before inspection/execution. No runnable `manifest.json` or task assets are included here.

The runtime handoff is in [RUNTIME_HANDOFF.md](RUNTIME_HANDOFF.md). The full-package documentation and W3 task report contain final qualification and smoke status. r2 remains unchanged in `../L2_Demo_V5`.

`IMAGE_REQUIREMENTS.json` maps every Stream/Turn to image pins, pending build/materialization flags, archive filenames and verifier environment variables. The current package has 58 pending native profiles. Hermes baseline pin and paid-budget recovery behavior are documented in RUNTIME_HANDOFF.md.

Tictoc includes an unchanged semantic ID scorer in addition to shape tests. Its paid submission received native reward 0.1; a classifier defect rejected that fractional reward. The classifier fix, fresh final qualification and checkpoint recovery are complete. The recovered Turn ends at public work limits with score 0 and completed evolution; its cumulative 2,349,049-token usage includes the original 413,502 tokens and must not be counted twice.

The approved qualification scope has 75 static-only placements; 58 pending profiles instead describe runtime readiness. τ² airline Turn 1 scores 1 (1,238,453 tokens, 133.644 s). Recovery phase/cumulative accounting is recorded in the handoff.

**Smoke coverage limit:** all nine prescribed family Turn-1 executions completed, but only eight exercised native verifier/Judge. AutoResearch exited 0 with `evaluation_complete=true`, completed evolution and Turn score 0 through the public execution-limit path (`terminal_timeout,background_terminal_forbidden`). It used 1,665,768 tokens / 32 calls / 412.655 s and invoked neither native verifier nor Judge. Its strict summary therefore retains `sample_completed=false`, `turn_execution_completed=true`, `native_judge_exercised=false`. This is not nine fully verified scoring smokes. No quality rerun or limit increase was performed. All 27 AutoResearch placements separately passed offline native gold/broken qualification; that does not establish the missing agent-submission/Judge path.

Final qualification meets the approved scope: **222 executed placements passed + 75 static-only placements, zero coverage errors**, across 291 source identities and 216 executed unique sources. Exact execution/input/profile hashes bind 162 executed placements; 54 original QA executions lack execution-time hashes and six QA recovery records have proven resource-metadata-only profile additions. Strict execution-hash coverage remains incomplete. Legacy QA qualification has no per-container resource readback; declared-only policy metadata is not an enforcement observation. The 58 pending profiles describe runtime readiness and are not the 75 static-only placement count.
Supervising review passed; the package is published as the `l2-demo-v5-r3` Release.

### Supplemental AutoResearch scorer diagnostic

The official AutoResearch smoke remains execution-limit score 0 with no native/Judge call. A separate diagnostic copied its saved paid submission and reply, then called the existing native verifier and Judge without another baseline, simulator or evolution. Native normalized score was 0.4346536176 (R² 0.9056499338), and Judge validity was 1.0. Additional usage was 34,313 tokens / 1 call; cumulative usage was 1,700,081 / the original 3,000,000 tokens. The original six evidence files and copied workspace remained unchanged, and restored image/transport cleanup completed. This proves the saved artifact's scoring path, not compliance with the original work limits; it does not replace the official Turn score or change the strict smoke flag. The Judge's validity rationale does not override the recorded terminal timeout/background-process violation. Full evidence is retained in the W3 task artifacts at `ar-scorer-diagnostic/RESULT.json`.
