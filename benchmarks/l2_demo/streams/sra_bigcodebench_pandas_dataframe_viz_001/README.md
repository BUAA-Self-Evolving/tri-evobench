# SRA BigCodeBench pandas/DataFrame Stream

This executable pilot Stream contains two native BigCodeBench items selected from SRA-Bench because both carry gold skill `bigcodebench_094` and both require pandas DataFrame construction/transformation followed by a visualization object. Each Episode keeps a complete private copy of its original SRA item as `evaluation_assets/source_item.json`; the public request and private evaluator fields are then split into the L2 envelope.

Episode order:

1. `E001` / `bigcodebench_00009`: list of pairs → DataFrame + bar chart.
2. `E002` / `bigcodebench_00020`: CSV → parsed dictionary column → Seaborn pairplot.

The Stream workspace persists across both Episodes. These native items are response-only code-generation tasks, so they have no Agent-visible input files to seed or patch into the Workspace; the full request is copied into each `episode.json`. The pilot runtime stores interaction records and learned memory under `workspace/.l2_state/`, then injects that memory into later Episodes. It evaluates only the current Episode submission and never exposes gold skill IDs to the Agent.

Scoring is copied from the native SRA BigCodeBench evaluator: sanitize/extract the model-generated code, combine it with the native code prompt, execute the native unit tests through a sandboxed `untrusted_check`, and assign `1` only when the result is `PASS`; otherwise assign `0`. There is no partial credit. The native evaluator scores one response; allowing up to three Interactions and using the latest Interaction as the terminal Episode result is the project's wrapper policy, not an upstream BigCodeBench rule. Retry feedback exposes only `pass`, `fail`, or `timeout`, not extracted code, evaluator details, tests, answers, or gold skill IDs.

Each Episode includes an executable package-local verifier contract and points to `l2_runtime.sra_bigcodebench:SraBigCodeBenchVerifier`. For this authorized demo, generated code runs in a temporary local subprocess with time and resource limits. This is deliberately labeled unsafe and requires an explicit CLI opt-in; it is not a security-grade container Sandbox.

The immediate SRA-Bench source repository is MIT-licensed. Its BigCodeBench evaluator and task lineage point to the Apache-2.0 BigCodeBench project, so this package retains both license texts. The copied prompts/tests/answers are unchanged; only their public/private placement and surrounding L2 metadata are project additions.

This two-Episode Demo exercises data packaging, provenance, Stream/Workspace lifecycle, visibility separation, model invocation, bounded retries, learner-memory persistence, and native end-task scoring. One successful run does not establish statistically meaningful L2 learning improvement.

Scope boundary: SRA evaluates retrieval separately through Recall@K/nDCG@K artifacts, while its BigCodeBench evaluator scores only end-task execution. This pilot preserves the latter. It excludes the 232 MB uncompressed candidate skill corpus, retrieval/reranking configuration, and retrieval metrics; those are harness-level inputs for a future full SRA integration, not hidden dependencies of these materialized end-task Episodes.
