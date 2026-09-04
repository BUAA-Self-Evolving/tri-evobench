# MLGym Titanic tabular-classification Stream

This three-Episode pilot materializes native MLGym task `titanicSurvival`:
E001 uses the native full test set, and E002/E003 use two deterministic disjoint
PassengerId slices with Episode-namespaced public CSVs and private ground truth.
The Agent returns a Python training/prediction script; the package-local adapter
validates `submission.csv` with the native column/row/PassengerId semantics and
reports native accuracy.

MLGym records a baseline Accuracy of `0.76555` in the native task config but does not define a pass/fail threshold. The pilot uses that recorded baseline as its explicit demo success threshold rather than inventing an unrelated value. Scores remain continuous; the terminal Episode result is the latest Interaction.

The task data and most of MLGym are distributed under CC-BY-NC-4.0. This Demo
is for non-commercial research. It is not a GPU/container reproduction of the
full MLGym harness, and three related variants are still only pilot evidence,
not a statistically meaningful learning curve.
