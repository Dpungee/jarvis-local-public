# Evaluation approach

Jarvis is evaluated by observable behavior rather than model-authored claims. The test
suite combines deterministic unit and integration coverage with bounded stress and
recovery scenarios.

## What is measured

- Natural conversation routing, latency classification, cancellation, and context
  continuity.
- Tool selection, argument validation, authorization, approval scope, and one-shot
  consumption.
- Memory provenance, contradiction handling, temporal confidence, retrieval relevance,
  and safety leakage.
- Cross-domain strategy selection against a frozen fictional holdout, including
  source-receipt eligibility, independent target outcomes, harmful negative-transfer
  controls, ordering invariance, and zero authority leakage.
- Web research source collection, citation traceability, synthesis gates, and honest
  incomplete outcomes.
- Code and document workflows, real execution evidence, artifact verification, and
  vacuous-test rejection.
- Specialist isolation, delegation budgets, project scoping, and restart recovery.
- Long-horizon project isolation, real subprocess restart windows, ordered
  checkpoints, pre-operation usage reservations, exactly-once simulated effects,
  append-only reconciliation, cancellation persistence, and independently signed
  completion evidence.
- Proactive and initiative gates, recovery attestations, daily limits, and drift
  detection.
- Redaction, path containment, external mutation, desktop adapters, and self-repair
  immutability boundaries.

## Reproducing the deterministic suite

```powershell
python -m pip install -e ".[documents]"
python -m unittest discover -s tests
python -m jarvis doctor
```

GitHub Actions runs the deterministic suite on the supported Windows platform with
cloud models, local models, computer access, host execution, and external access
disabled. Provider-dependent and live UI evaluations remain separate so a network or
subscription outage cannot be mistaken for a source regression.

## Evidence policy

Public reports must contain the exact commit, configuration class, test command,
platform, pass/fail result, and known limitations. Raw prompts, conversation logs,
postal codes, usernames, local paths, provider responses, and account data are private
development artifacts and must not be committed.

Benchmark numbers are historical observations, not permanent product guarantees.
Results should be refreshed on the release commit and never copied forward after code,
models, providers, or hardware change.

The Phase 4A strategy-transfer holdout is deterministic benchmark evidence only. It
does not establish production causal lift and cannot activate advice. Production
activation requires a separate operator-started trial that persists randomized
control/treatment assignment before outcomes exist and is not part of Phase 4A.
Phase 4B derives its fixture, evaluator, configuration, and runtime pins from the
exact installed sealed benchmark; the operator does not copy those hashes. Its
assignment seed is generated locally and is never printed. A completed trial still
cannot activate advice until the causal attestation passes every declared gate and
the operator separately promotes that exact manifest.

The Phase 5 long-horizon holdout uses multiple valid projects, fresh child
processes, a separate SQLite effect ledger, and deliberate process exits before
dispatch, after the effect but before its result receipt, and after a checkpoint.
It verifies the coordinator and recovery protocol only. Phase 5 ships no automatic
tool/model executor, so the benchmark does not claim that unrelated Python code or
an uninstrumented external service is metered. See
[`LONG_HORIZON_WORKFLOWS.md`](LONG_HORIZON_WORKFLOWS.md) and
[`LONG_HORIZON_THREAT_MODEL.md`](LONG_HORIZON_THREAT_MODEL.md).

From a source checkout, reproduce the packaged Phase 5 holdout with:

```powershell
python -m unittest -v tests.test_long_horizon_eval
```

The public wheel contains the runtime and sealed fixture, but not the repository's
test modules; this exact command is therefore source-checkout-only.

For repeatability, the holdout derives deterministic synthetic Ed25519 authority
keys. They exist only inside the temporary benchmark processes, are not production
authority keys, and cannot authorize live activation. Executor/recovery processes
receive no private authority key; the separate verifier process must reopen and
hash the real deterministic artifact bytes plus the effect ledger and exported
workflow evidence before signing.
