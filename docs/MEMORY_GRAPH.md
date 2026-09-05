# The temporal graph (VTMF M3, schema 48)

A question that spans two or three stored facts is answered from the facts,
in either direction, without a model call on the read path.

Before M3 the read path could follow one hop, forwards only: "Which datacenter
hosts the Kestrel relay?" worked because the relay's value named a subject the
next claim was about. The reverse of the same triple did not. "What runs on
the Harrier box?" walked away from the answer, "Which relay is in the Fenwick
datacenter?" found nothing, "Which region is the Kestrel relay in?" stopped one
hop short, and "Which relays are in the Northgate region?" was told nothing was
recorded — about a name the store held two hops of facts behind.

M3 adds a third retrieval channel over a graph projection of the claim
projection: entities, edges with validity intervals, and a bounded traversal
that runs behind every floor the claims lane already enforces.

## What the graph is

The graph is **derived**, never authoritative. A claim row is the fact; an edge
is its shadow.

```text
memory_spine_events  ->  memory_claims  ->  memory_graph_edges / _entities
     (authority)          (projection)            (projection of a projection)
```

`spine rebuild-claims` proves the claims equal the spine; `graph rebuild`
proves the graph equals the claims. Nothing is stored in the graph that the
claim rows do not already say, and the graph has **no spine event kinds of its
own** — its only receipt is `projection.rebuilt` with `projection: "graph"`.

Three tables, in the same database file as everything else, so an erase
removes the fact and its edges in one transaction (an attached second file
could not commit atomically under WAL):

| table | holds |
|---|---|
| `memory_graph_entities` | one row per `(scope, entity_key)`; explicit ids from `memory_graph_entity_sequence`, never reused |
| `memory_graph_edges` | one row per claim row, keyed **by the claim id** |
| `memory_graph_entity_sequence` | the id allocator |

An **entity key** is the claim identity's subject normalization plus NFKC:
`" ".join(unicodedata.normalize("NFKC", text).casefold().split())`. A fullwidth
spelling joins the ordinary one; a Cyrillic look-alike does not — the same
boundary `normalize_private_identifier_text` draws. Entities are per scope, and
the traversal joins them across visible scopes by key.

`label` is the first spelling seen in a scope. It is **display-only**: it is
read by `graph paths` and by `verify_graph` and by nothing on the read path,
every cue string comes from the claim row by `claim_id`, and a rebuild that
sees a different first spelling is **not** a divergence.

## What is excluded, and why you can see it

Every claim row that is not excluded projects to exactly one edge. Exclusion is
a closed set of three categories, counted in `graph status`, `graph verify` and
the migration receipt, so an operator can see how many facts the graph is not
carrying and why:

| category | rule |
|---|---|
| `excluded_predicate` | the predicate is in the reserved namespace `identity` / `permission(s)` / `preference(s)` / `safety`. Every user preference is such a claim; identity, permission, preference and safety rows are not facts to chain through |
| `subject_private` | the subject fails the widened screen. Subjects are **not** privacy-screened at write time — the claim writer checks secrets only — so this is the first gate that sees them |
| `subject_too_long` | the subject's entity key is over 80 characters, while a subject may be 200 (governed) or 500 (global API). The edge's `src_entity_id` is `NOT NULL`, so the honest choice is to exclude the claim rather than truncate a name into a collision |

The invariant "every non-excluded claim has exactly one edge" therefore holds
by definition: a claim with no edge is either in a category or a `missing_edge`
problem, never unexplained.

A claim's **value** is either an entity node or a literal terminal. A literal
still gets an edge, so a chain can end on "listen port 9090", but it is never
joinable and never a start. Values become literals when they are empty, over 80
characters, have no letter, look like prose (nine or more words, or a sentence
terminator), or fail the widened screen.

One more value never becomes a node, and it is the only admission rule that
fires on a value which passes every screen: a **bracketed redaction
placeholder** (`[REDACTED]`, `[EMAIL]`, `[USER]`, `[HOST]` — anything matching
`memory_graph.REDACTION_PLACEHOLDER`). `remember_claim` rewrites a
secret-shaped value to `[REDACTED]` before storing it, so two entirely
unrelated credentials become the same string; as a node that string would join
the facts about both, and a question about one probe could walk into the other.
The placeholder is a terminal hop instead. An ordinary bracketed name such as
`Rack [A]` is unaffected.

That single rule is the reversed-triple fix: **a value links to the facts about
it exactly when the same key exists as a subject entity in a visible scope.**

## Answering a question

```python
Memory.graph_chains(
    query, *, project_id, subjects, seed_claims,
    temporal=False, as_of=None, lane_mode=None, limit=None,
) -> {"rows": [...], "overflow": [...], "report": {...}}
```

Start entities come from the subjects the question names
(`_named_fact_subjects`), from the subjects and values of the main lane's seed
claims, and from a one-word proper name resolved by the alias rule. Resolution
is either **exact** or **non-exact**, and the two obey different floors:

- **An exact full-key match resolves a start unambiguously.** `UNIQUE(scope,
  entity_key)` means an exact key names one stored subject and no other, so the
  lexical look-alike floor — which exists because the claims lane discovers
  subjects with `LIKE '%term%'` and can return a row about a different stored
  subject — does not apply. A question spelling `Kestrel relay` correctly is
  answered from that key even when `Kestrel relay 2` and `Kestrelrelay` are also
  stored.
- **Every non-exact resolution carries the look-alike floor, store against
  store**: the candidate's full key is compared against every other visible
  entity key, and more than one candidate, or one candidate with a stored
  look-alike, abstains with mode `identity-conflict`. In a dense store this
  abstains almost always, which is the honest position: **exact resolution is
  the supported entry point, and the alias rule is a small-store convenience.**
- **A one-word name with two candidates abstains; it does not come back
  empty.** The alias rule matches a stored key by its last word, and when two
  keys match — `Marchbank Loom8` and `Pendreth Loom8` for a question about
  `Loom8` — the call abstains `identity-conflict`. Returning `no-start` there
  said the store had never heard the name, which was false and told the
  operator to go looking for a fact they already had.
- **The prefix rule matches whole words, in position.** A typed name resolves
  by prefix when every word of it equals the stored key's word at the same
  position, so `Tarnworth` matches `Tarnworth bolt`. A name that prefixes
  nothing and is not a one-edit near miss of a stored key resolves to nothing —
  mode `no-start`, even when the claims lane abstained `identity-conflict` on a
  shared first word. `Tarnworth mill` against a store holding `Tarnworth bolt`,
  `Tarnworth bolt 2` and `Tarnworthbolt` is `no-start`, not a conflict: the
  store has bolts and has never heard of a mill, and those are different things
  to tell an operator.
- Two or more exactly resolved subjects are allowed, and are the join case
  ("Is the Kestrel relay in the same datacenter as the Harrier box?").

**Seeds never answer for a name the store cannot identify.** When a question
names at least one subject and *none* of them resolves, seed-derived starts
contribute nothing and the call abstains. The lane's substring scan is broad
enough to hand over a row about something else entirely: asked about the
unknown `Yealand fold`, it offered `Yealand mill / parish / Zennorly fold`,
whose endpoints are both real keys, and the graph answered about the mill — a
confident answer to a question nobody asked, and the one leak of that holdout
run. Seeds add starts only beside a resolved named subject, or when the
question named no subject at all.

**One unidentified name among several does not abstain the call.** If the
operator names two subjects and only one resolves, the answer comes from the
one that did and the walk reports the other in `unresolved`; the agent turns
that into a single line — *"The store has no recorded fact about: Tarnworth
mill."* — in the per-turn wrapper. Without it a half answer reads as a whole
one. A name whose *non-exact* resolution fails is different and still abstains
the whole call `identity-conflict`: the store knows that name, it just cannot
tell which one was meant.

The two identity rules cut in opposite directions, and the difference is who
chose the name:

- A subject **the operator typed** that resolves only non-exactly abstains the
  whole call with mode `identity-conflict`, even beside another subject that
  resolved exactly. Answering half of a two-subject question from the half the
  store happened to recognize would be worse than saying which name is unclear.
- A **lane-supplied seed** never abstains anything — the claims lane chose it,
  not the operator — but a seed row is dropped entirely when its subject or its
  value is a look-alike of a name the operator spelled exactly. The whole row
  goes, not just the offending endpoint: the row's other endpoint is a fact
  *about* the look-alike, so admitting it as a start walks the chain straight
  back to the name the exact spelling was meant to exclude.

Exact spelling always answers alone.

Traversal is a breadth-first walk of at most three hops in both directions,
with the frontier keyed by entity key (a name present in `global` and in the
project is one node, expanded once, with the fan-out cap applied to the union).
Every expansion is one indexed `SELECT`; a recursive CTE was measured and
rejected at 22 ms for the same walk.

| bound | value | why |
|---|---|---|
| `MAX_HOPS` | 3 | the region is three hops from the relay |
| `FANOUT_CAP` / `FANOUT_CAP_FILTERED` | 16 / 32 | an inner hub's cost is multiplicative |
| `FANOUT_CAP_TERMINAL` | 64 | the last hop reads answers, not a frontier; a 40-in-edge datacenter overflowed at 16 and at 32 and could never answer "which relays are in Northgate" |
| `NODE_BUDGET` / `EDGE_BUDGET` | 48 / 96 | a star graph cannot make every turn expensive |
| `SCREENED_ROW_CAP` | 24 | the screen phase costs 0.04 ms for an ordinary row, and 3.8 ms for the pathological one (a 500-character subject beside a 4,000-character value) |
| `CHAIN_CAP` / `CHAIN_ROW_CAP` | 2 / 8 | the block is 4,200 characters and a single main-lane row can reach 1 KB |
| `TIME_BUDGET_MS` | 25.0 | **one deadline for the whole call**, taken once at entry and threaded through start resolution (including the look-alike floor's key-set read), the traversal loop, the screen phase, and every screened row |

**The graph narrows by the attribute a question names, not by the words of
the subject it names.** `memory_graph.narrow_asked_words` is the one place
that decides, and each of its three rules is a failure that was found rather
than foreseen:

- **A word of the named subject is the subject.** "Where is the Alder probe
  hosted?" asks nothing about "probe". Treating it as an asked attribute made
  four sealed-holdout `lookalike` cases and two joins answer nothing at all,
  because the only word they narrowed by was a word of their own subject.
- **An activity verb is dropped only when the question asked for something
  else too.** "Which datacenter used to host the Kestrel relay?" narrows to
  `datacenter`, not to `deployed on host` — the attribute is the datacenter.
  But in "Where is the Dornick probe hosted?" the verb is all the operator
  asked, and the store really does have a `hosted in` predicate, so dropping
  it answered a question about hosting with the subject's channel instead.
- **The visible store's own vocabulary decides what counts as an attribute.**
  A word that names a stored predicate narrows the walk. A word the store
  knows only as a subject or a value is a *thing*, not an attribute, and never
  narrows — the "hall" in "the same hall as" is somewhere, not something you
  can ask a fact for. A word appearing **nowhere** in the visible store is an
  attribute the graph cannot reach, so the question is unanswerable and the
  `not_recorded` cue answers it rather than a chain. A trailing plural folds
  (`relays` ≈ `relay`), and an activity verb never makes a question
  unanswerable on its own.

  This replaced a test against a fixed list of configured words, which let
  "almanac" through: *"Which almanac lists the Aldwin barge?"* was answered
  with a moorage and a district. Both were true facts about the barge, and
  neither was an almanac — which is the hardest kind of substitution for an
  operator to catch, because nothing in the answer looks wrong. Emptying the
  asked set is still not the same as never having asked: an unreachable
  attribute abstains, an unasked one is simply open.

Chains are then ranked by predicate overlap with the question, then fewest
hops, then the **minimum** authority along the chain, then current before
superseded, then the newest terminal claim. A chain is exactly as strong as its
weakest hop, and the cue says so: that row carries `weakest: true`, and when it
is below `operator` the terminal row carries `chain_authority`.

**A chain number identifies a start, not a rank.** Every chain that grows from
one start entity carries one number and numbers its rows `hop` 1…n from that
start, so a question naming two subjects comes back as chain 1 and chain 2 and
the model can tell which fact belongs to which name:

```text
chain 1 hop 1  Osprey relay  / deployed on host / Talon box
chain 1 hop 2  Talon box     / datacenter       / Moss Hollow
chain 2 hop 1  Kestrel relay / deployed on host / Harrier box
chain 2 hop 2  Harrier box   / datacenter       / Fenwick
```

**Which chains get the two slots is decided before rank, by two rules that
exist because the obvious ranking got the answer wrong.** A subject the
operator *typed* outranks one the claims lane merely handed over as a seed, and
every named start is served before any seed-derived start takes a slot; and one
chain is reserved per `(start, direction)` before any start takes a second in
that direction.

Both rules earn their place on one question. "What runs on the Harrier box?"
has a forward answer (the box is in Fenwick, which is in Northgate) and a
reverse one (the Kestrel relay runs on it), and only the reverse one answers
what was asked. Ranked purely on hops and recency, the seed's own two-hop
forward walk took both slots and the one-hop reverse chain — the whole point of
the reversed triple — was dropped with a "1 more stored chains answer this"
note. Now the block is the same with or without seeds:

```text
chain 1 hop 1  Harrier box   / datacenter       / Fenwick
chain 2 hop 1  Kestrel relay / deployed on host / Harrier box
```

Note the shape: the forward and reverse facts about one subject are **two
chains, not one path**. A genuine multi-hop question still comes back as one
chain — "Which region is the Kestrel relay in?" is chain 1, hops 1 to 3, with
`bridge_from` on hops 2 and 3.

`CHAIN_CAP` bounds how many such groups are emitted. When it drops chains that
would have answered, the block carries a note saying how many — *"N more stored
chains answer this; ask about one by name"* — rather than letting the two that
fit read as the whole answer. It is the same honesty rule as a fan-out
overflow, applied to the chain cap instead of the hub cap. A block under the
cap reports no truncation at all, so that note never appears on a complete
answer.

## Time

`valid_from` and `valid_until` are the claim's. An edge is current while its
status is `active` or `disputed` and historical once `superseded`.

- Default: current edges only.
- A past-tense question (`was`, `used to`, `before`, `previously`, …): current
  and superseded edges are both candidates, current ranks first, and a row that
  used a superseded edge is marked `superseded`, with `retracted: true` when its
  key has no current row.
- An explicit instant the agent can parse without a model — an ISO date, or a
  month name with a year, read as the first instant of that day or month in UTC
  — sets `as_of`. Anything vaguer stays in temporal mode; the agent never
  guesses a date, because an `as_of` the operator did not state would silently
  narrow the answer.

**A row superseded in place before schema 46 has no `valid_until`, and is
invisible to every `as_of` question.** The `as_of` filter excludes it
deliberately: without that exclusion such a row would match *every* instant,
including instants before it was written. Migration 48 does not rewrite claim
rows to backfill those intervals — writing to `memory_claims` is the claims
lane — so the honest outcome is that the store cannot say when a legacy version
stopped being true, and answers `no-answer` rather than guessing.

`Forget` retires a fact and keeps it traversable in temporal mode. Only `Erase`
removes edges, and it removes them in the same transaction as the claim rows,
sweeping any entity left with no edge in any status.

## What the model sees

Chain rows join the existing `temporal_claims` block after the main-lane rows
and before the history rows, inside the same 4,200-character budget. There is
no score fusion: a chain row cites exact lineage and must not be outranked or
promoted by a free-text score.

```json
{"subject":"Harrier box","predicate":"datacenter","value":"Fenwick",
 "status":"active","authority":"operator","confidence":1.0,
 "chain":1,"hop":2,"bridge_from":"Kestrel relay / deployed on host",
 "updated_at":"…"}
```

`hop` is 1-based within its chain and `bridge_from` names the previous hop, so
a reverse hop still shows the shared name. A superseded row adds
`superseded_at` and possibly `retracted`; a bounded chain adds `incomplete` to
**every** surviving row of that chain; the weakest hop adds `weakest`, and the
terminal row `chain_authority`.

**The block is a whitelist, and two fields deliberately stay out of it.** The
store returns more on each row than the model is shown: `scope` (`global` or
`project:N`) and the row's `claim_id`. Both are real and both are used — the
scope is how shadowing is decided and what `graph paths` and the tests check,
the claim id is how rows from two channels are recognised as one fact and
merged. Neither is something the model should reason about or repeat back: a
scope is store bookkeeping, and a claim id invites a citation that means
nothing outside the database. The whitelist is the only thing standing between
a store-side column and the prompt, so a test asserts that neither ever appears
in a rendered block.

**A bounded result is never presented as complete.** A hub whose fan-out cap was
hit produces an explicit `status: "overflow"` entry naming the hub and the hop
at which the walk stopped — at any depth, not only at a start — and the chains
that passed through it are marked incomplete. **A chain that *ends* at the
overflowing hub is incomplete too**, not only the ones that continued past it.
An open question walking into a 40-edge hub at hop 1 recorded the overflow
entry and returned the chain unmarked, which is the one shape where the marker
matters most — the chain is the answer, and all of it is a sample.

The marker follows the terminal, not the group. Sibling rows share every hop
but the last (§5.4), so within one sibling group **only the row whose own
terminal is the overflowing hub carries `incomplete: true`**; a sibling that
ends somewhere else is a complete answer and is left unmarked. Marking the
whole group would say the store had more to add about facts it had finished
reading, which costs the marker its meaning exactly where the operator is
relying on it. At most two such notes are
emitted; further overflows are counted in the report and their chains stay
marked. A question with forty answers at the terminal hop returns the eight
strongest with an accurate count in the note, which is the point of the larger
terminal cap: without it the operator got nothing at all.

The block shrinks from its tail under pressure, in this order: history rows,
then overflow entries, then chain rows from the highest hop downwards (so a
chain is shortened from its tail, never its head), then the second chain, then
main-lane rows. When the block does not fit, every chain row is marked
incomplete before it is rendered.

Guidance rides in the per-turn dialogue wrapper, only when the block carries
the marker, and **nothing is added to the compacted runtime contract**:

| trigger | line |
|---|---|
| `"hop":` | a chain number continues the chain named in `bridge_from`, in hop order |
| `"overflow"` | more stored facts link to that name than fit; say so and ask which one is meant |
| `"incomplete":true` | the store could not finish reading this chain; answer from it only as partial |

The full-prompt lead gains one clause when the block carries a chain, and a
second fixed clause when the claims lane could not resolve the subject and the
graph answered anyway from an exact key.

**The "former values only" lead is selected on the absence of a *current*
entry, not on the absence of a main-lane row.** A reverse or three-hop question
answered entirely from the graph has an empty main lane and is live; announcing
it as retracted would be confidently wrong. A temporal answer built only from
superseded edges still gets the retracted lead.

### When the graph stays silent

`report["mode"]` is a closed set:

| mode | meaning |
|---|---|
| `complete` | chains were returned |
| `screened` | the query, or the lane's own outcome, refused the read for a security reason |
| `project-unavailable` | the project is missing or disabled |
| `no-start` | the name did not resolve |
| `no-answer` | the start resolved but nothing is visible — notably under `as_of` |
| `identity-conflict` | a non-exact resolution was ambiguous or had a stored look-alike |
| `overflow` | a start entity's fan-out was too large and no chain answered |
| `budget-exceeded` | the deadline or a budget ran out; whatever was screened is still returned, marked incomplete |
| `screened-rows` | every answering chain lost a row to the screens |
| `error` / `idle` | the read failed, or has not run |

`no-start` and `no-answer` are deliberately distinct: an operator should be able
to tell "I have never heard of that name" from "I know it, but not for then".

A lane mode of `screened`, `project-unavailable`, `corrupt-strongest` or
`error` silences the channel: those are security and availability refusals, and
the graph is not consulted at all. A silenced call reports mode `screened`,
except a lane `project-unavailable`, which reports `project-unavailable`.

The distinction that matters when reading a mode: the lane abstains
`corrupt-strongest` when the *asked* fact itself fails the material screen — a
secret, a private identifier or an over-long value in the strongest claim
matching the question — and the graph then reports `screened`. A graph mode of
`screened-rows` means something narrower: the screened row was **not** the
lane's strongest match, so it is a screened terminal at hop 2 or 3, or a
screened sibling beside a clean strongest row.

`identity-overflow` and `identity-conflict` no longer restrict it. They used to
force the graph to exact names only, on the reasoning that an identity floor is
an identity floor; the sealed holdout showed that gate turning **four correct
resolutions into `no-start`**, two of which should have abstained
`identity-conflict` and could not, because the rule that raises it never ran.
The lane reporting `identity-conflict` is the lane saying *its own* substring
scan could not tell which subject was meant — that is not evidence against the
graph's rules, each of which carries its own floor. So the alias, word-prefix
and near-miss rules run regardless, and a question under an abstaining lane now
either answers or abstains `identity-conflict` on its own merits.

What does not change is what the operator is told: whenever the lane abstained
and the graph answered anyway, the cue carries the fixed clause saying the main
memory lane could not tell which stored subject the question named. That
sentence is now more load-bearing than before, because the name the chain
started from may not be one the operator spelled in full.

## Privacy

The graph is **stricter than the live claims lane**, deliberately. The lane
keeps `contains_private_identifier` so the sealed evaluations cannot move and
an operator who explicitly stored a management address still gets it back. The
graph, the agent's two history helpers and the memory listing use the widened
screen, `redaction.screen_endpoint`, which normalizes once and runs the secret
detection and the extended identifier scan over that one normalized string.

It screens e-mail addresses, user-home paths, IP-host e-mails, phone numbers,
bare IPv4 and IPv6 addresses, SSN-shaped strings, Luhn-valid card numbers —
**grouped or not**, since a bare 13-to-19-digit Luhn-valid run is a card number
however it is written — and street addresses. Both endpoints — **subject and
value** — are screened at node admission and again on every returned row.

**A word in front of a credential does not make it safe.** Negative context
used to be a general net over every kind, and the red team walked straight
through it: `case 078-05-1120`, `invoice 4111 1111 1111 1111` and
`rack 10.0.0.7` all passed. There is now **no exemption at all** for `ssn`,
`card`, `ipv6`, `street_address`, `email` or `user_home` — those shapes are
identifiers wherever they appear. Only two kinds keep an exemption, each
narrow enough to state exactly:

| kind | the only way out |
|---|---|
| `ipv4` | a CIDR suffix `/0`–`/31` (a network is not a host; `/32` **is** a host and still screens), or — for a **public** address only — a version word or version suffix. So `v1.2.3.4` passes and **`v10.0.0.7` screens**, because no version number is `10.0.0.7` |
| `phone` | fewer than ten digits, or an ISBN shape |

Still exempt by their own shape, because they are not host identities:
loopback, `0.0.0.0`, broadcast, the three documentation ranges, multicast,
`::1`, and `2001:db8::/32`.

**Three declared misses, stated rather than hidden**: an all-letter IPv6 such
as `dead::beef`; a dotted phone number such as `415.555.0199`, since dotted
digit groups are versions in this codebase's vocabulary; and a near-miss
subject whose single edit is its *first* character, which resolves as
`no-start` rather than `identity-conflict` — it never answers, it is only
reported as an unknown name rather than as an ambiguous one.

**What the 512-character scan cap covers, exactly.** No regex is ever handed
more than `SCAN_LIMIT` = 512 characters, which is what makes the screen's cost
independent of length and rules out backtracking.

The consequence is not that a long value is trusted — it is that **a value over
512 characters is screened on its length alone**, as the kind `long_value`,
whatever it contains. Past the scan cap the screen cannot see the whole value,
so it must not vouch for it: it fails closed instead. Such a value is prose and
far past `ENTITY_LABEL_MAX_CHARS`, so it was never going to be a node anyway.

*Which* kind gets named within a long value is still bounded: it is scanned at
**both ends** — the first 512 characters and the last 512 — with the full kind
set, while between those windows only the digit-run, compressed-hex and `@`
rules apply, un-normalized. So an e-mail buried at character 700 of a
2,000-character value is reported as `long_value` rather than as `email`. It is
screened either way, which is what the read path acts on.

The whole screen is bounded at **4 ms** for any input up to the 4,000-character
maximum a claim value can hold (measured worst 3.1 ms across the adversarial
corpus: all digits, repeated IPv4, digit-dash runs, hex-colon runs, parentheses
and `@` runs). `contains_secret` and `contains_private_identifier` are
unchanged byte for byte, so the sealed evaluations and the governed write gate
cannot move.

## Operator surfaces

```text
python -m jarvis graph status  [--json]
python -m jarvis graph verify  [--json]
python -m jarvis graph rebuild [--apply [--yes [--plan TOKEN]]] [--json]
python -m jarvis graph paths "<subject>" [--project N] [--hops 1..3] [--temporal] [--json]
```

`status` prints edge and entity counts and the three exclusion categories.
`verify` checks every edge against its claim row, every entity key against its
label, the sequence against the maximum id, and both screens over every label;
it exits 1 on any problem and its details **name fields, never values**.
`rebuild` is the graph's counterpart to `spine rebuild-claims` and carries the
same exit codes and the same plan-token discipline: without `--yes` it prints
the plan and its token and exits 2, `--apply --yes --plan TOKEN` refuses with
`stale_plan` when the store no longer matches that plan, and a refusal changes
nothing. `paths` shows values, because it is the same screened read the agent
performs — it goes through `Memory.graph_chains` and never touches the tables.

`spine verify` additionally reports the graph counts, but the graph never
changes its exit code: the spine is authentic whether or not a projection
drifted, and drift is a rebuild matter.

## Ordinary-memory erase

`memories.id` has been explicit and never reused since schema 47, which makes
it an operator-facing identity, so ordinary memories finally get the erase that
project facts have had since M2:

```text
Erase memory #<id>                          (also: Delete memory #<id>, a leading "please")
python -m jarvis memory list [--limit N] [--json]
python -m jarvis memory erase <id> [--yes]
/memory                                     (in chat: the same rows, with their ids)
```

The verb is parsed with the exact-parser discipline of the three claim verbs:
nothing else may share the turn, and a near-command such as `forget memory 12`
owns the turn and fails closed quoting the exact shape rather than reaching a
model. Three cases refuse and change nothing, each with a fixed reason: no such
row; a row that backs a project fact (use `Erase this project fact:`); and a
row that mirrors a vault note, which the indexer would simply re-create.

The receipt names the id, the kind, the created date and how many transcript
copies remain — never the content. `memory list` previews 120 characters
through the widened screen and prints `[PRIVATE]` instead of a row that trips
it. From the CLI, `erase` without `--yes` prints the kind and created date and
exits 2 without touching the store.

## Boundaries

The claims lane is unchanged: its 2,000-row identity and candidate bounds, and
their cost at ten thousand keys, are recorded, not fixed. The graph exists
partly because its start is an exact entity key, which has no such cliff.

There is no model-side entity resolution, no coreference beyond the
deterministic alias rule, no predicate synonymy beyond word overlap with the
question, no inferred edges, no materialized transitive closure, and no edges
from ordinary memories or lessons — only claims are triples.

Non-exact start resolution effectively stops working in a dense store, by
design: with twelve thousand entity keys drawn from a handful of stems, a
candidate conflicts with thousands of them and the look-alike floor abstains.

The one-hop bridge is kept only for a store without the projection and is
removed in M4. `jarvis/memory_graph.py` is in `_IMMUTABLE_REPAIR_FILES`, so
self-repair can never draft over it. Older trees refuse to open a database at
schema 48.
