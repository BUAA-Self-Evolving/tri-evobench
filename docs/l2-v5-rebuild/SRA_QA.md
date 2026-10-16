# SR-Agents QA pool

`tools/l2_v5_sra_qa.py` materializes the four native QA families from SR-Agents
revision `277fd8d2bbd7d3b81a5cf4ffa6e87e18c7906e4f`. It does not read the skill
corpus. Each capability is exactly one upstream skill; CHAMP uses the first
annotation and retains all annotations as native labels. The reviewed selection
is in `tools/l2_v5_sra_qa_selection.json`: three capabilities per family, five
representatives and two backups each. Other admitted instances have passed only
mechanical qualification and need individual curation before Stream selection.

## Native behavior

The prompt is the upstream system and user prompt concatenated, without
rewriting the question or injecting skills. The submission is `final_response`
with a text payload; the General Turn composer binds the final response string
and passes it unchanged to `source_verifier.verify`. No answer file is needed.

The private assets contain each dataset evaluator, `evaluate/common.py`,
`evaluate/base.py`, `llm.py`, and `prompts.py`, byte-for-byte from upstream.
Empty package initializers avoid importing unrelated BigCodeBench components.
`PROVENANCE.json` records paths, SHA-256 hashes and revision. The carrier only
maps native `correct` to pass/1.0 or fail/0.0. No tolerance or extraction fix is
applied. Source-instance hashes use sorted-key `json.dumps`, literal Unicode,
and its default separators, matching the existing SRA builder.

| Family | Extraction and scoring |
|---|---|
| MedCalc | Final `ANSWER:` line, JSON answer, trigger phrase, date/gestational pattern, then last number. Date calculator IDs 13/68 require matching parsed MM/DD/YYYY; ID 69 compares weeks/days. Integer calculator IDs or integer output type compare rounded numbers. Decimals require inclusive supplied lower/upper bounds; missing bounds fail. Fractions parse numerically. |
| LogicBench | BQA uses the final explicit answer phrase, polarity heuristics, then last yes/no; exact case-insensitive match to gold. MCQA takes last `choice_N`, explicit answer/option number, matching choice text, or numeric fallback. Native choices are numbered; arbitrary A/B/C/D letters are not equivalent. |
| TheoremQA | Native final-answer triggers; without a trigger, numeric extraction falls back to the last number. Yes/true and no/false substring checks precede numeric parsing. Case-insensitive exact answer or option-substring match; integer gold rounds predictions, float gold permits 4% relative error (absolute floor 1e-9). Numeric lists compare sorted members; its fallback recognizes parentheses, despite the prompt describing square brackets. The reviewed selection avoids list answers. Optional LaTeX parsing is installed in the image. |
| CHAMP | Final `ANSWER:` line, trigger phrase, then last nonempty line. Exact normalized/canonical phrases, yes/no, numbers, SymPy expressions and native “or equivalently/approximately” alternatives. Integer gold rounds predictions; floats use 4% relative error (floor 1e-9). Arbitrary set/assignment syntax is not supported. |

All four use the unchanged upstream think-tag stripper. Native TheoremQA and
CHAMP use Python expression evaluation; run these private verifiers only in the
benchmark's isolated verifier containers. Qualification does not substitute an
alternative parser or rewrite upstream answers.

## Build and qualification

Use explicit paths through environment variables; the upstream checkout is
read-only. `STAGING`, `LOGS`, and `OUTPUT` must be outside the source checkout.
Build and publish require empty target directories to avoid overwriting evidence.

```bash
# Verify this local base image ID before the extension build.
docker image inspect -f '{{.Id}}' trievo-l2-v5:0.7-native
# Expected: sha256:7305c4e84e529f05b5e9e472c892aff6d6077b1fff0e95ecd0d820b3c85d9aef
docker build --pull=false -t trievo-l2-v5-sra-qa:0.1 sandbox/l2-sra-qa
python tools/l2_v5_sra_qa.py build --upstream "$UPSTREAM" --pool "$STAGING" \
  --licenses docs/l2-v5-rebuild/sra-qa-licenses/licenses.json
python tools/l2_v5_sra_qa.py qualify --pool "$STAGING" \
  --image trievo-l2-v5-sra-qa:0.1 --logs "$LOGS" --jobs 24
python tools/l2_v5_sra_qa.py publish --staging "$STAGING" --output "$OUTPUT" \
  --selection tools/l2_v5_sra_qa_selection.json
uv run --frozen python -m pytest tests -q
```

The image extends existing L2 Python 3.11 with latex2sympy2 1.9.1 and
antlr4-python3-runtime 4.7.2. The qualified image is pinned by its immutable
Docker image ID in every task's runtime and qualification record. Rebuilding
can produce a different ID; always rerun qualification for that new image.

Each task's gold response and empty broken response execute in separate fresh
containers with `--network none`. Results are collected through bind-mounted
files, because attached stdout is unreliable on rootless Docker. Timeouts or
errors do not qualify as a valid red. Runtime task directories contain no
external source dependencies. Publishing retains only admitted task directories,
while `INDEX.jsonl` records every considered instance, failures and curation
rejections. `COUNTS.json` reports per-family thresholds, top ten skills, and
per-capability admission/rejection reasons. Selected tasks must be qualified and
match their declared capability before publication succeeds.

## Licenses

MedCalc data retains CC-BY-SA-4.0 attribution and share-alike terms; LogicBench,
TheoremQA and CHAMP retain their original MIT notices. The SRA MIT code license
does not replace dataset licenses. Dataset license notices and source URLs are
copied into each task's private assets, independently of the upstream checkout.
See `sra-qa-licenses/licenses.json` and the accompanying original license texts.
ToolQA is not pooled; its external corpus and model staging require separate
licensing and offline-runtime work.
