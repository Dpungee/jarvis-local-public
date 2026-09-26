# Recorded benchmark evidence

This directory holds the JSON artifacts a live benchmark run writes. Each one is a
self-describing receipt: it names the code it ran against, the provider and model,
the command that produced it, the gate thresholds, the observed values, and the
limitations that bound what those values mean.

Read [`../PHASE2_EVIDENCE.md`](../PHASE2_EVIDENCE.md) for the Phase 2 dossier that
interprets these files gate by gate. Read [`../EVALUATION.md`](../EVALUATION.md) for
the evidence policy every artifact must satisfy.

## Filename convention

```
phase2_<benchmark>_<provider>_<hash8>.json
```

- `<benchmark>` — the benchmark's short name, lowercase with underscores
  (`task_contract_resolver`, `task_contract_outcomes`, `transfer_lift`,
  `calibration`).
- `<provider>` — the provider that served the model (`ollama`), not the model name.
- `<hash8>` — the first eight characters of the **base file manifest** hash.

While Phase 1 and Phase 2 remain uncommitted, `<hash8>` is a manifest hash, **not a
commit id**. A commit id would name a tree that does not contain the code these
numbers describe. `jarvis.calibration_report.evidence_filename()` builds the name
from those three parts so the convention is enforced in code, not by hand.

## The base identity every artifact carries

```json
"base": {
  "kind": "phase2_base_file_manifest",
  "manifest_sha256": "6616688bfcc072eefffc2708c5846e53f556d8d597b972465f84e310b8f3f9b1",
  "manifest_file_count": 364,
  "base_commit": "35d5344",
  "branch": "claude/roadmap-phase1",
  "committed": false
}
```

The manifest lists the sha256 of every file under `jarvis/`, `tests/`, `scripts/`,
`docs/` and `requirements/`. It is held outside the repository with the Phase 2
planning documents, and it records its own hashing recipe: the digest covers the
`files` object alone, serialised with `sort_keys=True` and
`separators=(",", ":")` — not the whole manifest, and not the file on disk. An
artifact that claims a manifest hash it did not verify sets
`manifest_sha256_verified` accordingly; do not trust a base block without it.

## Required header fields

| Field | Rule |
|---|---|
| `base` | as above, including `committed` |
| `provider` | name and the version the provider reported at run time |
| `model` | `requested` and `served`, recorded separately |
| `configuration_class` | prose summary **and** the exact environment switches |
| `command` | the command that produced the artifact, local paths replaced by `<placeholders>` |
| `platform` | **OS name and Python version only** |
| `gates` | one row per gate: `metric_field`, `threshold`, `comparator`, `observed`, `passed`, `note` |
| `known_limitations` | non-empty; what the numbers do not mean |
| `created_at` | UTC timestamp |

`platform` is OS name plus Python version and nothing else. Never
`platform.node()`, never a hostname, never a home path, never a user name.

An unscored gate records `observed: null` with a note saying the run was incomplete.
**Never** substitute a partial or hand-computed figure for a metric the scorer
refused to produce; a raw receipt count may be reported beside a gate row only if it
is explicitly labelled as a receipt count and not as the metric.

## What is never committed here

- No prompts, case text, conversation logs or model replies.
- No local paths, user names, hostnames, e-mail addresses or account data.
- **No output from a report run against the operator's own database.** The
  quarantine report and any calibration report over his live data are read-only
  tools whose output stays on his machine. Only aggregate counts may be quoted in a
  write-up, and only when they carry no per-record detail.
- The calibration artifact in this directory is from an **isolated sandbox
  database** and says so in `sandbox_disclaimer`. Sandbox rows are not the
  operator's rows and do not move his initiative gate.

## Artifacts

| File | Benchmark | Package | Headline |
|---|---|---|---|
| `phase2_task_contract_resolver_ollama_6616688b.json` | TaskContract resolver over the sealed 66-case holdout | WP-2 | 59/66 resolved, 7 rejected by post-parse reconciliation; scorer refused a partial set, so all four contract gates are unscored |
| `phase2_task_contract_outcomes_ollama_6616688b.json` | End-to-end outcomes over the isolated Phase-2 toolbox | WP-4 | 60 agent runs, 183 model calls, scorable denominator 58; unscored because six cases were rejected in every resolver pass |
| `phase2_transfer_lift_ollama_6616688b.json` | Cross-domain transfer lift, model in the loop | WP-9 | treatment − control +7.5 pp against a 15 pp gate; 12/12 negatives rejected; underpowered and inconclusive |
| `phase2_calibration_ollama_6616688b.json` | Prediction calibration, three families | WP-10 | three families at 20 outcomes each in a sandbox; not the operator's gate |

Every one of these is a single run on a single host with pinned decoding. Benchmark
numbers are historical observations, not permanent guarantees, and must be refreshed
after any code, model, provider or hardware change.
