# Long-horizon workflows

Phase 5 adds durable coordination for bounded work that spans multiple stages or
process restarts. It stores a closed manifest, ordered stage state, chained
checkpoints, pre-operation usage reservations, append-only mutation receipts,
and an independently signed final-verification receipt.

It does **not** add an automatic executor, new tools, background authority, or
permission to run model-authored code. Registering a workflow records a plan; it
does not execute it. A future tool/model adapter must use the existing policy and
approval gateways and must derive real usage itself. The Phase 5 coordinator
does not trust a model or arbitrary callback to report its own resource use.

## Operator commands

Every command requires an exact project identifier and runs without invoking a
model:

```text
jarvis workflow status --project PROJECT_ID
jarvis workflow list --project PROJECT_ID
jarvis workflow show PLAN_ID --project PROJECT_ID
jarvis workflow start --project PROJECT_ID --manifest MANIFEST.json
jarvis workflow pause PLAN_ID --project PROJECT_ID
jarvis workflow resume PLAN_ID --project PROJECT_ID
jarvis workflow cancel PLAN_ID --project PROJECT_ID
```

`workflow start` accepts one regular, non-symlink UTF-8 JSON file no larger than
128 KiB. The document must match the closed Phase 5 schema exactly, bind the
existing project/conversation/task, contain at least five ordered stages, and
stay within every configured limit. Unknown, duplicate, executable, prompt,
credential, URL, path, module, and command fields are not part of the schema and
are rejected.

There is deliberately no `workflow run` command. `start` reports that it
registered durable state and did not activate a stage executor.
`start` is the retained operator command name, but in Phase 5 it means
**register only**, not begin execution.

The first workflow command may initialize the local version-40 tables and create
`<jarvis.db>.long-horizon.key`. It is model-free and sends no workflow content to
a provider, but it is not a zero-write status probe. Protect and back up the
database and sidecar key as one pair. Never commit the key or copy it into a
report. On Windows its protection inherits the Jarvis data-directory and user
ACL. A missing or different key must make existing workflow records fail closed.

## What is preserved across restart

- project, conversation, task, goal, contract, constraints, approval-scope,
  and artifact-set digests;
- ordered stage definitions and their remaining budgets;
- the checkpoint hash chain and completed-stage cursor;
- one-shot mutation intent, authorization, consumption, outcome, and signed
  reconciliation rounds;
- pause, cancellation, failure, quarantine, clock floor, and global-stop state;
- signed receipt authority identifiers and verifier runtime bindings.

Public keys are supplied by external configuration and are not persisted in the
workflow database. Any process that validates or advances signed receipts after a
restart must receive the same pinned public-key configuration. Phase 5 does not
create or persist production verifier or reconciler private keys.

State and receipts are validated before transitions. Mutable plan/stage state is
authenticated with a keyed digest whose 32-byte key is stored beside the local
database, never in the database or CLI output. Losing that key makes existing
workflow state unverifiable and therefore unavailable. A newly generated key
cannot authenticate records written under a missing earlier key.

## Mutation recovery boundary

A consequential adapter must reserve its measured budget before work, record an
intent, obtain a fresh live approval, consume a short-lived one-shot permit
immediately before the effect, then record the observed result. A crash after
authorization or effect dispatch enters reconciliation. Signed reconciliation is
append-only: an uncertain observation may later resolve to applied or not
applied, while a confirmed applied effect can never be downgraded or replayed.

Phase 5 ships the durable protocol and deterministic recovery evaluation, not an
adapter that performs real external mutations. No arbitrary Python callback is
accepted as an executor because in-process callback code could perform unmetered
work and lie about usage.

## Meaning of controls

- **pause** prevents new claims and routes ambiguous mutation state to
  reconciliation; it does not pretend an external effect was undone.
- **resume** applies only to a coherently paused plan and grants no expired
  approval or lease.
- **cancel** is terminal. Ambiguous already-dispatched effects remain available
  for authenticated reconciliation, but cancellation cannot restart execution.
- **failed**, **quarantined**, **complete**, and **cancelled** states cannot be
  promoted back to active by operator controls.
- **complete** is valid only after all stages have checkpoints and a distinct,
  pinned Ed25519 verifier signs the bound evidence and outcome.

## Honest limits

- The coordinator is evidence and state-transition infrastructure, not a new
  source of intelligence or capability.
- Budget ceilings apply to operations performed through a future instrumented
  gateway that reserves measured usage before dispatch. Phase 5 intentionally
  provides no generic executor and cannot meter unrelated code running outside
  that gateway.
- The local keyed digest detects database-only modification. It is not protection
  against an attacker who can both rewrite the database and steal or replace the
  same user's sidecar key or running process.
- Ed25519 verification proves possession of a configured private key and exact
  receipt binding. Operational separation still depends on keeping verifier and
  reconciler private keys outside executor processes.
- A deterministic fixture proves only the tested state-machine behavior. It does
  not establish live provider reliability, model quality, or safe performance for
  an untested external service.
- The sealed fixture's authority keys are deterministic synthetic test material
  for reproducibility. They are never production keys and authorize no live use.
