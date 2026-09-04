# Release scope and safety

This archive is a self-contained, non-commercial research handoff for the L2
data-demo contract. It is ready to send to the benchmark/runtime team, but it
is not a claim that every upstream dataset has been cleared for unrestricted
public redistribution.

## Distribution boundary

- AIRS/SICK and MLGym/Titanic carry non-commercial Creative Commons terms;
  their retained license files govern use.
- SkillLearnBench and SkillsBench retain stock-data lineage notes. One
  attributed Kaggle source is marked `Other` without additional terms in its
  description. Keep these fixtures inside the research handoff unless a
  project owner completes a separate redistribution review.
- AppWorld is excluded because its protected bundle and derivatives must stay
  encrypted. The demo intentionally contains no AppWorld plaintext data.
- Every Stream keeps its source revision, license, and additional lineage notes
  inside its own directory.

## Credential and execution boundary

- The package contains no API key. `L2_API_KEY` is read only from the process
  environment and is not accepted as a command-line argument.
- `python demo.py check` scans the files selected for release for key-shaped
  tokens, private keys, Bearer credentials, suspicious credential filenames,
  required contract fields, private-evaluation boundaries, and broken package
  paths.
- Four Streams execute model-generated Python locally. They require the
  explicit `--allow-unsafe-code-execution` flag and are **not** protected by a
  security-grade container Sandbox. Use a trusted disposable machine.
- The two browser/file-artifact Streams expose native verifier smokes but are
  not full terminal/browser-Agent Flows.

## Workspace contract

One Stream owns one copied Workspace for its entire run. Episodes continue in
that Workspace, retain declared learner state, and use the latest Interaction
as the terminal result. A new Stream invocation receives a fresh UUID run
directory and never reuses a prior run. The Interaction cap is three.

`README.md` is the authoritative quick-start document. `TROUBLESHOOTING.md`
records observed failure modes. `runtime/README.md` explains implementation
details but deliberately does not duplicate commands.
