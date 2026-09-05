# Public benchmarks

These are public benchmark observations. **They are not release gates.** The
release authority is the sealed one-use holdout set (V3, V5, the
strategy-transfer trial, the long-horizon restart, the memory-graph holdout, and
the M5 compaction holdout), each scored once per phase against a frozen runtime
pin. Public numbers are re-runnable, may move with the model, the provider, the
host or the date, and are never copied forward across a code, model or hardware
change. Datasets are fetched at run time and are not redistributed here; LoCoMo
is CC BY-NC 4.0 and only measurements about it appear below. **Non-commercial
use is declared for every run in this file**: the operator's use of Jarvis is
personal and non-commercial, and the runner refuses to fetch a use-restricted
dataset when a commercial use is declared. Compliance rests on that positive
declaration, recorded per run, not on an environment variable happening to be
unset.

---

## Standing block

| item | value |
|---|---|
| Declared use | **non-commercial** (personal use; recorded per run in every config) |
| Leakage check last run against a fetched dataset | **never** — no dataset has been fetched on this host yet |
| Leakage check coverage | the **whole file**, streamed one element at a time; values are unescaped through the JSON parser and compared with whitespace normalised on both sides |
| Runner version | `1.0.0` (`scripts/benchmarks/__init__.py`) |
| Judge prompt sha256 | printed by `python scripts/benchmarks/run.py list`, beside the judge's pinned temperature and seed; published in every config that uses the judge |

The leakage line above is updated by hand from the line
`python scripts/benchmarks/run.py fetch <dataset>` prints after each fetch. It
is recorded here on purpose: the strongest leakage assertion can only run when a
real dataset is present, so it never runs in CI, and a reader must be able to
see when it last ran rather than assume a green CI covered it. The line the
`fetch` subcommand prints names the number of sampled values and whether the
scan covered the whole file, so the coverage claim above is recorded per run
rather than assumed.

---

## What these numbers may and may not be used for

**May:** appear in this file; be quoted in a release note beside their config
hash; be compared against *our own* earlier runs; be used to choose a default
(for example, whether compaction ships on).

**May not:** appear in an exit gate, a release gate, a CI gate, or any
threshold; justify changing a sealed threshold; be published without their
config; be compared against a competitor number we did not run; or be described
with a superlative. What differentiates Jarvis is *that its memory behaviour is
measured and gated*, not that the architecture is unprecedented. The report
renderer refuses a smoke-tier run and the report writer refuses any run whose
observed models fall outside the config's `allowed_model_prefixes`, so neither
a smoke nor a local-model pass can become a number in this file.

### The ten honest-reporting rules the runner enforces

1. **Sealed holdouts are the release authority; public benchmarks demonstrate.**
   No public number appears in an exit gate, a release gate, a CI gate, or a
   threshold.
2. **A public benchmark is re-runnable and never one-use.** Never quarantine a
   public benchmark; never re-score a sealed one.
3. **No leakage into sealed evidence.** No benchmark case, question, answer,
   haystack sentence, entity name, date or paraphrase enters any sealed fixture,
   development battery, prompt template, or test.
4. **Datasets are never vendored.** `MAX_TRACKED_FILE_BYTES` in
   `scripts/check_public_release.py` stays at 5 MiB. Every dataset is fetched at
   run time into `JARVIS_BENCHMARK_CACHE`, asserted at run time to be outside
   the repository root, and verified by sha256 before use.
5. **Every published number carries** model id, provider, UTC date, config hash,
   dataset sha256, licence-file sha256, git commit, schema version, host class,
   `n`, and the exact command.
6. **The local model never scores.** Any `ollama:` model may be used for an
   ingestion smoke run; the runner refuses to write a report if any observed
   model falls outside `allowed_model_prefixes`
   (default `claude-cli:claude-sonnet`).
7. **Judged numbers are published beside deterministic ones, never instead** —
   with the judge model and the judge prompt's sha256.
8. **Partial runs are published as partial.** `n` is stated, nothing is
   extrapolated, and a subset is never labelled with the benchmark's plain name.
9. **No cross-product comparison we did not run.** Another system's number is
   cited as "reported by X on date Y".
10. **A regression is published too.** This file is append-only: a superseded
    row keeps its place with a note.

---

## Datasets

Every fact below was verified by a read-only metadata call on **2026-09-04**
(HTTP `HEAD` for sizes, the hosts' own licence and metadata APIs, and a short
range read of LoCoMo's licence file). **No dataset was downloaded to build this
table, and none is stored in this repository.**

| dataset | source | licence | class | size (bytes) | how it is used |
|---|---|---|---|---|---|
| `longmemeval_s` | HF `xiaowu0162/longmemeval-cleaned`, `longmemeval_s_cleaned.json` | MIT | open | 277,383,467 | the published LongMemEval run set (500 instances, five abilities) |
| `longmemeval_oracle` | same repository, `longmemeval_oracle.json` | MIT | open | 15,388,478 | the control arm: evidence sessions only |
| `locomo10` | `snap-research/locomo`, `data/locomo10.json` | **CC BY-NC 4.0** | **restricted** | 2,805,274 | 10 samples, ~300 turns each; measurements only |
| `ruler_style` | generated here, seeded | Apache-2.0 (ours) | n/a | 0 | the long-context stress; no external corpus |
| `longmemeval-shape` | generated here, seeded | Apache-2.0 (ours) | n/a | 0 | offline fallback, reported under **this** name |
| `locomo-shape` | generated here, seeded | Apache-2.0 (ours) | n/a | 0 | offline fallback, reported under **this** name |

LongMemEval-V2 (HF `xiaowu0162/longmemeval-v2`, Apache-2.0, 7.12 GB, 451
questions) is recorded as a later option, not run.

### The commercial-use refusal

`locomo10` is **CC BY-NC 4.0**: redistribution with attribution, commercial use
prohibited. Two rules follow, and both live in
`scripts/benchmarks/cache.py::ensure_dataset`, keyed on the recorded
`licence_class` rather than on a benchmark's name, so a future restricted
dataset inherits them without a bespoke check:

* When `JARVIS_BENCHMARK_COMMERCIAL_USE` is set — to any value; presence is the
  declaration — a `restricted` dataset **refuses to be fetched or read at all**.
  Nothing enters the cache, the command exits 2 with
  `refused (commercial_use_declared)`, and **no report is written**: failing
  closed is the behaviour, and there is deliberately no
  `not run (commercial use declared)` row, because a row is a thing a run
  produced. The `ruler_style` and `longmemeval-shape` paths remain available and
  raise no licence question.
* A `licence_sha256` that no longer matches the pin **refuses the run** for a
  restricted dataset, and is reported for an open one. A licence you are relying
  on must not have moved under you between the pin and the fetch, and noticing
  afterwards is not compliance. The licence file is **re-fetched on every
  `fetch`** and the fresh digest compared against both the pin and the cached
  copy: digesting the cached copy would compare the file with itself, so drift
  could never be seen after the first fetch.

LoCoMo rows in this file carry `sample_id`, the question index, the category and
the scores, and **no question text, answer text, dialogue turn, persona,
caption or URL**. Publishing measurements about a dataset is not redistributing
it; publishing its questions is. Multimodal fields are ignored and never
fetched.

### Why NVIDIA's RULER harness is not run

The licence (Apache-2.0) is fine. The reasons are that its dependency set
(`nemo-toolkit[all]`, `tritonclient[all]`, `transformer_engine[pytorch]`,
`vllm==0.5.4`) is Linux/CUDA-only and will not install on this host; that it
drives a **served model's** context window while these benchmarks measure an
**agent's memory**, so publishing its number under Jarvis's name would be
dishonest; and that its `essay` haystack is copyrighted prose we would have to
fetch and re-emit. `scripts/benchmarks/ruler_style.py` therefore generates its
own fictional haystack, drives `Agent.run`, and says so in the report.

---

## Report format

Two artefacts per run.

**1. `reports/benchmarks/<benchmark>-<YYYY-MM-DD>-<config8>.json`** — machine
readable, ids and numbers only, written from the resumable per-case JSONL:

```json
{
  "schema": 1,
  "benchmark": "longmemeval_s",
  "run_id": "longmemeval_s-2026-09-12-4f2a91c0",
  "started_utc": "...", "finished_utc": "...",
  "config": { "...": "the block below" }, "config_sha256": "4f2a91c0...",
  "commit": "<sha>", "dirty": false,
  "n": 150, "tier": "subset", "resumed_from": null,
  "command": "python scripts/benchmarks/run.py run longmemeval_s --config ... --n 150 --judge",
  "aggregate": {
    "overall": {"n": 150, "deterministic": 0.0, "judge": 0.0},
    "by_group": {}, "group_key": "type",
    "abstention": {"n": 9, "accuracy": 0.0, "asserted_while_declining": 0},
    "judge_reliability": {"n": 150, "unparsed": 0, "unparsed_rate": 0.0},
    "delivery": {"n": 0, "not_delivered": 0,
                 "delivered_fraction_p50": null, "delivered_fraction_min": null},
    "latency_ms": {"p50": 0, "p95": 0},
    "tokens_per_answer": {"prompt_p50": 0, "completion_p50": 0},
    "errors": 0,
    "models_seen": ["claude-cli:claude-sonnet-4-5"]
  },
  "rows": [{"case_id": "...", "type": "knowledge-update", "det": true,
            "judge": "CORRECT", "abstained": false, "asserted": true,
            "latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "model": "claude-cli:claude-sonnet-4-5"}],
  "limitations": ["subset, not the full 500",
                  "judge is Sonnet, not GPT-4o as in the paper"]
}
```

Zeroes above are template placeholders. A runner never writes a number it did
not measure: an unmeasured cell is `null`.

Every row key comes from a closed set of ids, enums, booleans and numbers, and
any string value longer than 64 characters is refused by
`report.validate_row`. Every row must also name the model that produced it: a
report whose rows attest nothing is refused (`models_unrecorded`), and the
observed model is read from `model_call_metrics.model`, never from the
configured hint — a config must not be allowed to attest to itself. That is the
structural reason a CC BY-NC dataset can be measured here at all.

The report also carries `command`: the exact command line the run was started
with, as honest-reporting rule 5 requires. It sits outside the hashed config on
purpose, so re-running the same configuration from a different path keeps the
same hash. A per-case JSONL that scores the same `case_id` twice is refused by
name and line number rather than silently double-counting `n` and every rate.

**The published config**, hashed by `config_sha256` over its canonical JSON:

```json
{
  "benchmark": "longmemeval_s",
  "runner_version": "1.0.0",
  "dataset": {"name": "longmemeval_s", "sha256": "<pinned>", "bytes": 277383467,
              "licence": "MIT", "licence_class": "open",
              "licence_sha256": "<pinned>", "pinned": true, "licence_drift": false},
  "n_cases": 150,
  "sampling": {"strategy": "stratified", "key": "question_type", "seed": 20260904},
  "runtime": {"JARVIS_COMPACTION_ENABLED": false,
              "JARVIS_MEMORY_EMBEDDINGS": "disabled",
              "JARVIS_CONTEXT_LENGTH": 32768,
              "JARVIS_DIRECT_CONTEXT_LENGTH": 65536},
  "model": {"answer": "claude-cli:claude-sonnet-4-5",
            "judge": "claude-cli:claude-sonnet-4-5",
            "judge_prompt_sha256": "<published>",
            "judge_temperature": 0.0, "judge_seed": 20260904,
            "allowed_model_prefixes": ["claude-cli:claude-sonnet"]},
  "ingestion": "transcript", "fresh_conversation_per_case": true,
  "provider": "jarvis", "tier": "subset",
  "use_declaration": "non-commercial: the operator's use of Jarvis is personal and non-commercial",
  "compaction_available": false,
  "host": {"os": "win32", "python": "3.13.7"}
}
```

`compaction_available` is detected, not asserted: M5 half A had not landed when
this runner was written, so a config that asks for compaction on a tree without
`Memory.compact_conversation` **refuses** with `compaction_unavailable` rather
than quietly reporting a configuration it did not run.

The whole `runtime` block is **applied**, not merely published. The context
length and the embeddings setting are pushed into the agent the run builds, and
read back off the live agent afterwards; a mismatch refuses with
`runtime_config_not_applied`. The config hash is what makes a published number
re-derivable, so a runtime key that is hashed and then dropped would make the
hash decorative — a sweep over `JARVIS_CONTEXT_LENGTH` would otherwise run five
identical configurations and publish five different hashes describing them.

**2. This file** — human readable, append-only. One row per run:

| date | benchmark | n | tier | model | commit | config hash | dataset sha256 | licence sha256 | deterministic | judge | abstention | p50 latency ms | prompt tokens/answer | report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

*(no runs recorded yet)*

`python scripts/benchmarks/run.py report <path> --markdown` prints the row and a
per-group breakdown ready to paste. It refuses a smoke-tier report.

---

## Scoring

Every scoring path is a pure function of its inputs, so a published number can
be re-derived from the per-case JSONL months later.

* **Deterministic column.** NFKC casefolded normalisation, punctuation and
  article stripping, ISO date normalisation for temporal answers, then
  containment of the gold answer in the reply. A single-token gold is matched on
  a word boundary. A reply that both names the value and declines is **not**
  correct: that is a contradiction, not an answer.
* **Abstention column.** The same detector the M1 and M3 live batteries use, so
  a benchmark's abstention number and the batteries' abstention probes agree.
  LongMemEval's `_abs` ids and LoCoMo's category 5 are scored here: correct iff
  Jarvis declines **and does not also state a value**. The contradiction rule is
  symmetric — "The value is four. Nothing is stored about it." is not a correct
  abstention, it is an answer with a disclaimer bolted on, and scoring it as a
  decline would reward the behaviour these cases exist to detect. Every row
  carries `asserted`, and the aggregate carries
  `abstention.asserted_while_declining`, so the rate of answer-plus-hedge is
  visible rather than absorbed. For LoCoMo category 5 the dataset's own
  `adversarial_answer` is the grounded signal and no heuristic is used.
* **Judge column, optional.** `claude-cli:claude-sonnet-4-5` with a frozen
  prompt whose sha256 is published in the config, at **temperature 0.0 with a
  pinned seed**, both recorded in the header — a judged column that does not pin
  its decoding is not reproducible even in principle. The judge sees the
  question, the gold answer and the reply — never the haystack and never the
  store — and must answer with a line of exactly the form
  `VERDICT: CORRECT|INCORRECT|ABSTAINED`. Anything else, including an
  explanation containing a verdict word, is `UNPARSED`: scanning loose prose for
  the first verdict word resolved "INCORRECT. The answer is not correct." to
  CORRECT and biased the column upward. `UNPARSED` cells are excluded from the
  judged denominator **and counted**, and the count is printed beside the number
  as `judge_reliability.unparsed`, so a judge that failed on half the cases
  cannot produce a confident-looking number over the other half. The LongMemEval
  paper judged with GPT-4o, so our judged column is **not numerically comparable
  with theirs**, and every judged row says so. The judged column is published
  beside the deterministic one, never instead of it.
* **LoCoMo** adds category-wise exact match and token F1. Category 5 reports no
  F1: an adversarial question has no answer to overlap with, and a number about
  nothing is worse than a blank.
* **RULER-style** is exact string match, all-values for the multi-value and
  multi-query shapes, and full-chain for variable tracking — the `vt` task asks
  which variables hold a given value, so every link of the chain has to be
  followed to be named. No judge, so the arm is cheap.
* **The `direct` control arm does not run through the agent.** It is one
  tool-free `ModelClient.chat` call with no store, no compaction and no prompt
  clipper. Routing it through `Agent.run` put it through
  `Agent._compact_messages`, whose `_clip` keeps head 2/3 and tail 1/3 and
  deletes the middle: at 32K — the top of the default grid, not the opt-in 64K
  row — the depth-0.5 needle was removed before the provider saw it, which both
  inflated `jarvis - direct` and manufactured the Lost-in-the-Middle curve the
  benchmark exists to observe. A control the harness edits is not a control.
* **The control arm has its own window, and it is published.**
  `JARVIS_DIRECT_CONTEXT_LENGTH` sizes the direct arm separately from
  `JARVIS_CONTEXT_LENGTH`, because a 32K-token haystack cannot be delivered
  into a 32K context and raising the agent's window to fit the grid would
  change what the `jarvis` arm measures. Left unset for `ruler_style`, the
  runner sizes it to twice the largest grid length and writes the value it used
  into the published config.
* **A cell that did not fit is not delivered, not wrong.** When a direct prompt
  exceeds that window the cell reports `context_exceeded`,
  its `det` is `null` rather than `false`, and it is excluded from every rate.
  Each row carries `delivered_fraction`, and the aggregate carries a `delivery`
  block naming how many cells were not delivered and the smallest fraction
  seen. Missing evidence is not evidence of failure.
* **Degradation** is `score(oracle) - score(s)`, both measured by us, on the
  same day, with the same model. No other form of that number is published.

---

## Estimated cost and wall time (not measured)

The provider is `claude-cli` — the Claude Code CLI on the operator's
subscription — so the cost is subscription usage and the practical limit is a
quota ceiling. Run one tier at a time, and one benchmark at a time at the `full`
tier.

Every number in the two tables below is an **estimate**, not a measurement.
Nothing in this repository has run these benchmarks against a live model yet.

| tier | what it is | report? |
|---|---|---|
| `smoke` | 25 units of model work, round-robin across every shape, usually with `--provider fake` | **no** — writes only `*.smoke.jsonl`; the report writer and the markdown renderer both refuse it |
| `subset` | the published defaults below | yes |
| `full` | the whole dataset, one benchmark at a time | yes, and it requires `--confirm-full` |

The tiering is a gate, not a label: `--tier full` refuses without
`--confirm-full`, and so does an `--n` above the budget the config itself
declares. Those are the two ways an operator spends hours of subscription quota
by accident.

| run (estimated) | model calls | wall clock (est.) | approx. tokens (in / out, est.) |
|---|---|---|---|
| LongMemEval_s ingestion, 500 instances | 0 | 15-40 min | 0 |
| LongMemEval_s answers, n=500 | 500 | 1.5-2.5 h | ~5.0M / 0.20M |
| LongMemEval_s judge, n=500 | 500 | 20-35 min | ~0.75M / 0.03M |
| **LongMemEval_s full** | **1,000** | **~2.5-4 h** | **~5.8M / 0.23M** |
| **LongMemEval_s n=150 stratified (default)** | 300 | ~50-80 min | ~1.8M / 0.07M |
| LongMemEval oracle control, n=150 | 300 | ~35-50 min | ~0.6M / 0.07M |
| LoCoMo ingestion | 0 | < 2 min | 0 |
| LoCoMo n=300 stratified + judge | 600 | ~1.2-1.8 h | ~2.0M / 0.10M |
| `ruler_style` `arm=jarvis`, 6x5x20 | 600 | ~1.5-2 h | ~2.5M / 0.05M |
| `ruler_style` `arm=direct`, same grid | 600 | ~1.5-2.5 h | **~28M** / 0.05M |
| **everything, default sizes** | ~2,700 | **~7-10 h** | **~35M / 0.35M** |

The `arm=direct` grid is capped at 32K by default; 64K is opt-in
(`--lengths 4096 8192 16384 32768 65536`) because its token cost is an order of
magnitude above everything else — and it is the run most likely to die part-way,
which is why every run writes a resumable per-case JSONL and takes `--resume`.
A run that dies at case 412 of 600 still yields `n=412`, which rule 8 requires
be published as partial.

---

## Running one

Nothing below fetches anything unless you ask it to, and nothing writes a
dataset byte inside the repository.

```
# what exists, what it costs, where the cache is
python scripts/benchmarks/run.py list

# smoke tier: no network, no provider, no report - 25 cases each
python scripts/benchmarks/run.py run ruler_style \
    --config scripts/benchmarks/configs/ruler_style_config.json --smoke --provider fake
python scripts/benchmarks/run.py run longmemeval-shape \
    --config scripts/benchmarks/configs/longmemeval_shape_config.json --smoke --provider fake --judge

# the published subset, on the operator's live model
python scripts/benchmarks/run.py fetch longmemeval_s          # prints the digests
#   paste dataset.sha256 and dataset.licence_sha256 into the config, then:
python scripts/benchmarks/run.py run longmemeval_s \
    --config scripts/benchmarks/configs/longmemeval_s_config.json --n 150 --judge
python scripts/benchmarks/run.py report \
    reports/benchmarks/longmemeval_s-<date>-<config8>.json --markdown

# the whole dataset, one benchmark at a time, intent stated out loud
python scripts/benchmarks/run.py run longmemeval_s \
    --config scripts/benchmarks/configs/longmemeval_s_config.json --tier full --confirm-full --judge
```

`--provider jarvis` is the default and drives the real agent: a fresh store per
instance, transcript ingestion, and **a brand-new conversation for every
question**, because same-conversation scoring is inflated (10/12 against 3/6 was
measured during the M1 work) and is banned. The answering model defaults to
`claude-cli:claude-sonnet-4-5` and is overridable with `JARVIS_BATTERY_MODEL`.

Reports land in `reports/benchmarks/`, which is a generated directory: it is not
tracked, and `scripts/check_public_release.py` refuses to publish anything under
`reports/`.

## Notes for the merge

* **Config templates are named `<benchmark>_config.json`, never after the
  dataset.** The isolation guard forbids any tracked file whose basename is or
  contains a dataset filename, and it cannot tell a 700-byte template from the
  2.8 MB CC BY-NC dataset it configures — so `locomo10.json` as a template name
  tripped it. `locomo10_config.json` neither equals nor contains
  `locomo10.json`, and its stem and first dotted component differ too, so the
  rule provably cannot match it. A test asserts that for every shipped
  template, and a companion test asserts the guard still catches a real
  vendored dataset, including one renamed `copy-of-locomo10.json`. Do not
  rename these back.
* The test files are `tests/test_benchmarks_runner.py`,
  `tests/test_benchmarks_scoring.py` and `tests/test_benchmarks_isolation.py`
  (three files, plural). The M5 design named
  `tests/test_benchmark_runner.py` and `tests/test_benchmark_isolation.py`;
  the plural naming came from the implementation brief. Recorded here rather
  than discovered at merge, since three owners share this tree.
* `.gitignore` needs one line, `reports/benchmarks/`, so the generated per-case
  JSONL and reports stay untracked. That file is not in this owner's list.
* Half A of M5 (compaction) had not landed when this runner was written.
  `compaction_available` is detected, and a config asking for compaction on a
  tree without `Memory.compact_conversation` refuses rather than reporting a
  configuration it did not run.

## Related

`docs/EVALUATION.md` holds the sealed-evaluation policy these benchmarks sit
beside. The split is the whole point: the sealed one-use holdouts decide whether
a phase ships; the numbers in this file only describe how it behaved on a
particular day, against a particular model, on a particular host.
