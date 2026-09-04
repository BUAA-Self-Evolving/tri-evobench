# AIRS SICK textual-entailment Stream

This three-Episode pilot materializes native AIRS task
`TextualClassificationSickAccuracy`: E001 uses the native full test split, and
E002/E003 use two deterministic disjoint test slices with Episode-namespaced
public CSVs and private labels. The original Hugging Face SICK splits were
adapted from Arrow datasets to package-local CSV files.

The native task defines continuous Accuracy, an `estimated_worst_score` of `0.1451284`, and an optimal score of `1.0`, but no binary pass threshold. For the executable demo, the declared success boundary is the native `estimated_worst_score`; the original continuous Accuracy is still reported. This makes the threshold provenance explicit and must not be interpreted as an AIRS leaderboard success criterion.

The Agent returns one Python script which is executed in a temporary copy of the Stream Workspace. This is not the full AIRS container/RAD harness and is not a security Sandbox. AIRS-Bench is CC-BY-NC-4.0 and the downloaded SICK dataset card declares CC-BY-NC-SA-3.0; use is limited to non-commercial research under the retained source materials.
