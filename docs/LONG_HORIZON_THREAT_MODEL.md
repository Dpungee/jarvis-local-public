# Long-horizon workflow threat model

## Scope

Phase 5 is a project-scoped, restart-safe coordination substrate. It protects
bounded workflow metadata and evidence without granting tools, permissions,
network access, provider access, or autonomous activation. No automatic stage or
mutation executor ships in this phase.

## Invariants

- A store is bound to one existing project; plan identifiers from another
  project are indistinguishable from missing records through that store.
- The closed manifest and stage definitions are immutable after registration.
- Checkpoints are ordered, append-only, hash chained, and bound to the exact
  project, plan, stage, executor, artifact, outcome, and reserved usage.
- A usage reservation is durable before a stage can checkpoint or request a
  mutation permit. Counters never decrease after restart.
- Mutation receipts are append-only. Authorization is fresh, short-lived,
  one-shot, actor-bound, and cannot survive as reusable authority.
- Expired leases in an ambiguous mutation state never become an ordinary retry.
- Applied effects are monotonic and cannot be relabeled absent or dispatched
  again.
- Pause, cancellation, terminal failure, quarantine, and the global stop state
  dominate new claims.
- Final completion requires a pinned external Ed25519 verifier whose identity is
  distinct from every recorded stage executor and mutation/reconciliation actor.
- Database-only state modification fails repeatedly; handling the first error
  must not re-authenticate the modified row for a later read.

## Required responses

| Condition | Required response |
| --- | --- |
| Restart between verified stages | Resume at the first incomplete stage. |
| Crash before a mutation permit is consumed | Do not dispatch; reconcile the append-only intent/authorization evidence. |
| Crash after a permit is consumed | Require signed reconciliation by stable effect key; never ordinary retry. |
| Reconciliation remains uncertain | Preserve an append-only round and allow a later signed round to resolve it. |
| Two workers claim one stage | Exactly one atomic lease succeeds. |
| Project, manifest, stage, receipt, cursor, counter, or clock evidence changes | Reject the transition and remain fail closed. |
| A receipt or authority is substituted | Reject before changing workflow state. |
| A budget reservation exceeds a stage or plan limit | Refuse before the next instrumented operation marker. |
| Approval is absent, expired, changed, or revoked | Refuse mutation authorization; history grants no permission. |
| Operator pauses, cancels, or globally stops work | Permit no new claim; preserve ambiguous effects for reconciliation only. |
| Final artifact/evidence does not match | Refuse signed completion. |

## Key and authority assumptions

The sidecar integrity key is created as a regular, non-symlink file with restrictive
POSIX permissions where the platform exposes them. On Windows, the implementation
inherits the containing directory and user-account ACL; operators must protect the
Jarvis data directory. The key path and key bytes are never emitted by workflow CLI
receipts.

Only public Ed25519 keys are configured in the coordinator. Verifier/reconciler
private keys belong in separate minimal processes and must not be passed to an
executor. This separation is exercised by the packaged deterministic evaluation;
deployments must preserve it rather than co-locating secrets with an executor.

## Out of scope

- arbitrary model-authored commands, executable manifests, callbacks, or plugins;
- automatic tool/model execution or a claim that unrelated code is metered;
- new private-file, account, publishing, deployment, purchase, trading, desktop,
  or network authority;
- protection from a same-user attacker who controls both database and key/process;
- claims of AGI, consciousness, flawless operation, or universal safety.

