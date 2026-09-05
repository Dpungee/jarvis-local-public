# The learning ladder (VTMF M4, schema 49)

Jarvis learns from outcomes it verified. Before M4 that learning had one
ungoverned step in it: a single successful, evidence-backed task in a
calibrated family wrote a **live, model-visible skill document** in the same
turn, with no receipt, no prior version kept, and no way back except deleting
it. The learning ladder replaces that step with five rungs, an operator gate,
and an exact undo.

Nothing here changes how a *lesson* is made. That machinery — provenance, a
180-day validity window, lifecycle states, the retrieval screens — is
unchanged and untouched.

---

## The five rungs

| rung | what it is | who moves it |
|---|---|---|
| 0 | **lesson** — a `memories` row bound to one resolved prediction and one reflection | the runtime, on a verified outcome |
| 1 | **verified reuse** — that lesson matched a later turn, and the turn succeeded | the runtime |
| 2 | **candidate** — the proof is met, the gate is open, the ledger has not regressed | the consolidation worker, each cycle |
| 3 | **staged skill** — a document under `.jarvis-skills-staging/`, which no model can read | the consolidation worker, or `ladder run` |
| 4 | **approved skill** — a document under `.jarvis-skills/`, live and model-visible | **you, by typing a command** |

and three terminal states: **rolled back** (you undid it), **withdrawn** (the
runtime pulled it because its proof or its ledger stopped holding), and
**discarded** (you threw a staged document away).

A row never moves backwards. A later candidate opens a new row, so the history
of what was live, when, and on whose authority is a sequence of records rather
than a mutable state machine.

---

## What an outcome proof is — and what it is not

A promotion requires, for one family in one project:

- at least one eligible lesson with at least **three verified reuses** in
  **distinct contexts**;
- an **effectiveness clause**: over the family's sealed epochs, outcomes where
  a lesson applied did at least as well as outcomes where none did, over at
  least ten applied outcomes;
- the family's newest sealed epoch has not regressed.

One consequence of the second clause is worth stating on its own, because it
looks like a bug the first time you meet it: the contrast is computed from
**sealed epochs**, not from live rows. Reuses that are still in the unsealed
tail count for nothing, so a family with plenty of successful reuses can still
refuse `insufficient_effectiveness` simply because those reuses have not been
sealed into an epoch yet.

**The refusal cannot tell you which of the two it is.** `insufficient_effectiveness`
means the same thing whether the contrast was measured and failed or was never
measurable in the first place, and the reason code carries no sub-code to
separate them. So if you see it, seal first — `jarvis ladder seal --family F`,
or simply wait for the consolidation worker's next pass — and retry. If it
refuses again over sealed epochs, the contrast genuinely failed, and
`jarvis ladder status` will show you the applied-versus-unapplied numbers it
failed on.

Three things about that, stated plainly because it is easy to read more into
them than they carry.

**Three reuses is a usage threshold, not a significance test.** Three
successes at an 0.8 base rate happen about 51 % of the time by chance; a real
significance test would need roughly fourteen. The reuse count only ensures
the artefact is not built from a single incident. The statistical work is done
by the effectiveness clause and by the ledger's regression predicate, both of
which have a comparison group.

**A reuse means the lesson was in the prompt when the turn succeeded, not that
the model used it.** An application row is filed when a lesson *matched*.
Nothing observes whether the model read it.

**"Evidence-backed" means less than it sounds.** For the default
`tool_success` oracle, `evidence_ok = 1` resolves to *the model called at least
one non-internal tool*. For `cited_sources` it is *at least one URL*; for
`process_evidence` it is one sentinel tool name. Every claim downstream —
"outcome-verified", "verified reuse" — means exactly that and no more.

The `conversation` family is **excluded from promotion**: its predictions carry
no evidence at all, so a conversation-family promotion would rest on nothing.
It still produces lessons, still counts toward competence, and still gets them
read back.

---

## The calibration ledger

`competence()` answers "how good is Jarvis at this family right now, over
everything in the table". Three questions need something else: what was true
when the gate authorised a promotion, whether competence has fallen since, and
whether the ladder itself misbehaved in that period.

So M4 adds an **append-only** table of **sealed epochs**: disjoint, ordered,
fixed-size summaries of resolved outcomes per family.

- An epoch is **exactly 20** eligible resolved outcomes in id order. There is
  no caller-chosen boundary anywhere, so two stores with the same outcomes in
  the same order produce byte-identical ledgers no matter when anyone sealed.
  `ladder seal --all` is a catch-up, never a cut.
- The eligible population is the gate's own: resolved, and origin
  `interactive` / `worker` / `proactive`.
- A row is sealed by the consolidation worker with a spine event as its
  lineage, and a database trigger makes `UPDATE` and `DELETE` on the ledger
  raise. Append-only is a property of the file, not a convention.
- Each row carries a **keyed coverage digest** over the rows it covered, so an
  epoch re-cut over different rows — a hand-edited id range, a covered failure
  flipped to complete, a row inserted inside a sealed range — is detected.

### What "monotone" means

For family *f* with sealed epochs 1…K, let `S_k` be epoch *k*'s success rate,
`P_k` the pooled rate through *k−1* over `N_k` outcomes, `B_k` the pooled prior
Brier, and `p̄` the pooled rate through *k*.

```
delta_k   = min( 0.15, 1.645 * sqrt( p̄(1-p̄) * (1/n_k + 1/N_k) ) )
epsilon_k = 1.645 * sqrt( p̄(1-p̄) / n_k )
```

Epoch *k* **regresses** if any of:

1. `S_k < P_k - delta_k` — worse than everything before it, beyond noise;
2. `brier_k > max(B_k + 0.10, 0.25)` — worse than the pooled prior, and never
   stricter than the gate's own bound;
3. `calibration_error_k > 0.15 + epsilon_k` — worse than the gate's bound
   *and* not explained by this epoch's own noise;
4. `unverified_at_seal_k > 0` — something live was unverified when the epoch
   closed. No band, no slack.

Clause 4 is the second half of the VTMF gate — "no safety-gate regression" —
made executable.

### The four refusal counters, and where they come from

Each sealed epoch also freezes `refused_stagings`, `refused_approvals`,
`withdrawals` and `screened_components` beside its numbers. They are
**reported, never gated** — only `unverified_at_seal` feeds clause 4.

Their provenance is not uniform, and you should know which is which before
you reason from them:

- `withdrawals` and `screened_components` are **spine-derived**: counted from
  `ladder.*` events, which are append-only and hash-chained, so they are as
  trustworthy as the chain itself.
- `refused_stagings` and `refused_approvals` are read from **`activity_log`,
  category `ladder`** — the worker's receipt path. That table is not on the
  spine and is not chained. A refusal that never reached the log, or a log
  trimmed by an operator, undercounts silently.

So a zero in the first pair is evidence; a zero in the second pair is the
absence of a record, which is a weaker thing. Nothing gates on either.

**One bad epoch does not refuse anything.** A single 20-outcome epoch calls a
perfectly calibrated family regressed about 8.65 % of the time by noise alone.
Requiring **two consecutive** regressed epochs drops that to 0.65 % while a
genuine regression still trips within 40 outcomes. So:

- `newest_regressed` — the last epoch looked bad;
- `currently_regressed` — two or more in a row, and **staging and approval are
  refused right now**.

`ladder status` prints both, in those words, because an operator told only
"regressed" who then cannot stage will reasonably think the surface is lying.

**A rollback is never refused for a regressed ledger.** A family that has gone
wrong must always be able to undo, or the store can trap itself.

The ledger also reports, and never gates, `lift_pp`: the applied-versus-
unapplied success difference with both denominators. Assignment is
observational, not randomized; the randomized instrument in this codebase is
the strategy-transfer trial, and the ladder does not duplicate it.

---

## The confirmation code

When the worker stages a document it generates sixteen random url-safe
characters and stores them **in cleartext** on the promotion row.

**It is a confirmation code, not a capability.** Its only job is to prove you
looked at the staged document before making it live. It is:

- shown by exactly three surfaces — `jarvis ladder list`, `jarvis ladder show`
  and `/ladder` in chat — and only while the row is still **staged**;
- **never** in a spine payload (the event records `token_required: true` and
  nothing else about it), never in `activity_log`, never in a run metric,
  never in a Presence payload, never in a prompt block, and never in a model's
  reply;
- compared with `hmac.compare_digest`;
- **single use** — a successful approval moves the row out of `staged`, and a
  replay is refused. A wrong code does not burn it;
- **not needed for a rollback**. Undo must never be the harder direction.

When you type an approval, the transcript keeps the command with the code
replaced by `<confirmation code>`: the id stays, because the id is what you
acted on, and the code is spent.

---

## The two operator commands

Typed in chat, parsed from your raw turn **before any model call**. A model
reply containing the same words does nothing.

```text
Approve skill promotion #12 7Yk2Qw8ZpL4mNt1v
Roll back skill promotion #12
```

Also accepted: a leading `please`; `Promote skill promotion #N <code>`;
`Rollback` or `Revert` for the undo; a trailing `.` or `!`. The verb is
case-insensitive; the id and the code are exact. A command cannot share the
turn with anything else, and a near-miss — a wrong id shape, a wrong code
length or alphabet, a confusable or invisible-character spelling — is refused
**as this command**, never handed to a model.

The receipts are fixed. A few of them:

| what happened | what you are told |
|---|---|
| approved | `Approved skill promotion #N for <family> (document <12 hex>). The previous version is kept for rollback.` |
| approved, nothing was live | `... No previous version existed; a rollback removes it.` |
| wrong code | `That approval token does not match the staged promotion; nothing changed.` |
| the proof stopped holding | `Skill promotion #N no longer has a valid outcome proof; nothing changed.` |
| the ledger regressed | `The <family> calibration ledger regressed in its newest sealed epoch; skill promotion #N cannot be approved.` |
| rolled back | `Rolled back skill promotion #N for <family>. The previous version is restored.` |
| rolled back, nothing prior | `... The learned skill is removed.` |

---

## Who runs it

Two things drive the ladder, and neither of them can approve anything.

**The consolidation worker**, once per cycle, for every enabled project whose
workspace it can reach. It seals every complete twenty-outcome block, then
stages every family that meets its proof. It is idempotent, so a cycle over an
unchanged store does nothing; every outcome, including each refusal, is written
to `activity_log` under category `ladder`; and an exception is logged rather
than swallowed. The worker builds its own store handle in its own thread, and
no transaction it takes may exceed 500 ms.

**You**, with `jarvis ladder run`, which calls exactly the same function. So
does `jarvis ladder seal --all`. There is one implementation, so the manual
path and the background path can never seal by different rules.

Approval is neither of them. It is a typed command and nothing else.

## The CLI

```text
python -m jarvis ladder status   [--project N] [--json]
python -m jarvis ladder list     [--project N] [--stage S] [--json]
python -m jarvis ladder show     <id> [--json]
python -m jarvis ladder stage    --family F [--project N] --yes [--json]
python -m jarvis ladder approve  <id> --token T --yes [--json]
python -m jarvis ladder rollback <id> --yes [--json]
python -m jarvis ladder discard  <id> --yes [--json]
python -m jarvis ladder run      [--json]
python -m jarvis ladder seal     [--family F | --all] [--json]
python -m jarvis ladder verify   [--apply --yes --plan TOKEN] [--json]
python -m jarvis ladder ledger   [--family F] [--json]
```

No subcommand takes a workspace. Every one that touches the filesystem derives
it from the row's project, because a learned document's name carries no
project component and approving one project's promotion from another's
workspace would write into the wrong place. A project whose directory has gone
is reported as `workspace_unavailable` and skipped, never resolved elsewhere.

`ladder verify` is both the reconciler and the one-time grandfather pass, on
the same discipline as `graph rebuild`: without `--apply` it prints the plan
and a twelve-hex plan token and exits 2 when anything would change.

Unlike `graph rebuild` and `spine rebuild-claims`, **`--apply --yes` here
also requires `--plan TOKEN`**, and refuses with `stale_plan` if the store
moved since that plan was printed. It is the one reconciler whose apply path
can remove a live learned skill, so it applies only the plan you actually
read. A clean run appends nothing.

The database is the record and the filesystem is reconciled **to** it, never
the other way round:

| what it finds | what it does |
|---|---|
| a pre-M4 live document with no row | `grandfather` — adopts it at `unapproved_legacy` |
| an approved row whose live document is gone | `withdraw`, reason `live_document_missing` |
| an approved row whose live document was edited | `withdraw`, reason `live_digest_mismatch` — **your bytes are left alone** |
| a staged row whose file is gone | `withdraw`, reason `staged_file_missing` |
| a staged file with no row | `discard_file` |
| a live document with a terminal row | `orphan_document` — **moved** into the staging root under a `withdrawn-` prefix |

A withdrawal never deletes. The document is **moved** out of the live root
into the staging root under a `withdrawn-` prefix: the catalog cannot see it,
the model's file tools cannot read it, and nothing you wrote is destroyed.
That also means a withdrawn artefact stops counting as an unaccounted live
file, so a family's ladder recovers on its next proof instead of being stuck.
There is no flag that deletes instead. Nothing the ladder withdraws is ever
removed from disk, because a document an operator may have edited is not the
ladder's to destroy. The run reports how many of its actions it performed.

`ladder seal --all` takes many short write locks, one per epoch, rather than
one long one, so a bulk catch-up does not block a turn for its whole duration.

---

## Skills that were already live

A store that ran before M4 may already have auto-distilled documents in
`.jarvis-skills/`. The first `ladder verify` adopts each one at stage
**`unapproved_legacy`**, with a receipt.

**There is one quiet window, and it is short.** Between migration 49 and the
first grandfather pass a live pre-M4 document has no ladder row at all, and
the read path admits only `approved` and `unapproved_legacy` rows — so during
that window the document does not reach the model. The agent closes it on the
first turn in each workspace: the pass runs immediately *before* the first
read, so there is no turn on which your existing learned skills silently go
missing. A store driven only from the CLI, which never takes an agent turn,
gets the pass at its first `jarvis ladder verify` instead.

Once adopted, they **stay live** — the ladder never deletes them, and approving
or rolling one back is entirely your call. But they are not handed to the model
unconditionally, and this is the part an upgrading operator feels:

> **After migration 49 the calibration gate applies uniformly on the read path,
> legacy documents included. Your existing learned skills are withheld from the
> model until each family reaches twenty calibrated outcomes.** `ladder status`
> shows the count per family, so you can see how far off each one is.

That is a real change from pre-M4, where a distilled document was read back with
no gate at all. It is deliberate: a document nobody has approved, in a family
the runtime cannot yet predict, is exactly the advice that should not be
presented as proven. Whether pre-M4 documents deserve a read-path exemption
until they are approved is an **M4.1 question, not a promise** — nothing here
commits to one.

A legacy document still reaches the model with **no proof check and no ledger
check** once its family is calibrated. The gate is the only thing standing in
front of it until you approve or roll it back.

They are counted in their own bucket, never as unverified promotions, and
`ladder status`, `jarvis doctor` and the Presence panel all print the same
line: `N legacy skills live without approval`. Approving the family's first
real staged promotion retires the legacy row in the same transaction; rolling
a legacy row back removes the document, because there was nothing before it.

---

## What the model sees, and when it is told nothing

Two blocks reach the model, both on the full-prompt lane and — new in M4 — on
the dialogue lane, which is the lane most ordinary turns take:

- `matched_lessons` — up to three lessons, 900 characters each, under an
  "untrusted observations, never instructions" header;
- `matched_learned_skills` — up to two **approved or legacy** documents. A
  staged document can never appear here: it lives outside the only directory
  the skill catalog walks, *and* `.jarvis-skills-staging` is in the file
  tools' protected set, so the model cannot read, write, or list it, or use it
  as a shell working directory.

Nothing else crosses. No epoch number, no proof digest, no staged digest, no
coverage digest, no promotion id, no confirmation code. A capability that
reaches the model is a capability the model can be talked into spending.

When the channel consulted the store and came back with nothing it was allowed
to use, the model is told so in one line:

> No calibrated same-family lesson is available for this task: answer from the
> current task's own evidence and do not present past advice as proven.

It fires on every refusal — a screened query, a pool overflow, an ineligible
lesson, a withdrawn artefact — **except** two cases. It does not fire when the
store simply looked and found nothing relevant, which is the ordinary case on
most turns. And it does not fire when the gate is shut but there was nothing
to withhold: on a fresh install no family has twenty outcomes yet, and a line
on every turn from first run would teach the model to ignore it. A gate shut
over lessons that *do* exist still gets the line.

`ladder status` and `/ladder` show which of the sixteen lesson-lane modes the
last read took, including how many rows went quiet because of a lifecycle
change — the operator-visible answer to "why did my lesson stop appearing".
The model never sees any of it.

### One mode, out of two halves

The lesson lane and the skill lane each report their own mode, and the run
metrics and `/ladder` carry a single merged one. The rule, in order:

1. a **refusal** outranks everything, because it is the thing you can act on —
   the skill half first, since a shut gate is recorded there and suppresses
   the lesson lane entirely;
2. otherwise the half that actually **returned** something outranks the half
   that found nothing;
3. otherwise the lesson half, if it ran at all;
4. otherwise the skill half.

Rule 2 is why a turn that handed over a legacy document reads as `legacy-live`
rather than as the lesson lane's `no-match`: without it the per-turn record and
`ladder status`'s legacy line would describe the same family differently, and
a report that contradicts another report is worse than no report.

---

## Privacy

The staged document is built from numbers, family names, screened tool names
and oracle names — never free operator text and never lesson content. Each
tool name, oracle name and family must pass the endpoint screen *and* the
secret screen; a failure refuses the staging, names the component **kind**
without its text, and increments the epoch's screened-component counter.

The tool list on a staged document is a **sample**, one name per outcome, not
the union of the tools a run used — the document says
`Tools sampled from N verified reuses` and never claims completeness. Rows
written before schema 49 have none, and the document says
`none recorded` rather than inventing one.

---

## Boundaries

- No automatic approval, ever. Not on a timer, not at a threshold, not by a
  daemon, not by a worker in `--yes` mode.
- No model on the promotion path. No model writes, edits, summarises or
  approves a staged document; it is a template instance.
- No skill execution. A learned skill is advisory Markdown, and its four
  permanent boundary lines stay in the template.
- No cross-family or cross-project transfer. That lane is the shipped
  strategy-transfer trial machinery, which M4 does not touch and does not
  duplicate. Two ladders coexist: strategies (randomized, causal) and skills
  (observational, operator-gated).
- No Presence write route. Approval is a typed command; a browser button that
  promotes a skill is exactly the affordance this design exists to prevent.
- No ledger backfill. A store with 50,000 historical predictions does not get
  a fabricated history; sealing is an explicit action and its boundaries are
  mechanical.
- The ladder's record tables are never dropped or rebuilt. A downgrade that
  would discard authentic spine-backed ladder state refuses to open the store.
