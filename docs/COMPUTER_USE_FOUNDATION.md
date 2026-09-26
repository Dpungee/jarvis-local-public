# Computer-use foundation

## Current capability

This branch defines a typed, fail-closed broker and adapter contract for future
application-scoped computer use. It includes no native Windows adapter and cannot
capture the operator's screen or send host input. Computer use is therefore
`UNAVAILABLE` by default. An injected adapter remains `DISABLED` until the trusted
host enables the broker and installs an unexpired operator-issued grant.

This is a security foundation and synthetic simulator contract, not verified live
desktop support.

## Authority boundary

Every grant binds an actor, application, exact window, isolated session, permitted
action and observation types, resource scopes, issuer, and expiry. Revocation,
cancellation, expiry, target loss, focus changes, or the emergency stop fail closed.
Screen, OCR, and application text are always untrusted data and cannot issue a grant.

Generic computer-use grants categorically reject credential/key entry, wallets,
transactions, payments, purchases, and publishing. Those effects require separate
purpose-built capabilities and approvals; adding a native adapter must not weaken
that rule.

Pixel or text observations require an explicit local-processing release. The public
contract intentionally has no cloud-release mode. This does not claim local filtering
can prove secrecy: a future adapter must provide a genuinely isolated capture and
processing path or refuse the observation.

## Adapter invariants

A native adapter must:

1. Resolve stable application, window, isolated-session, bounds, and generation data.
2. Return the current target without changing focus or interacting with the desktop.
3. Capture only the granted target and never fall back to whole-screen capture.
4. Revalidate target identity immediately before input.
5. Return `UNKNOWN` for ambiguous effects. The broker stops and forbids automatic
   replay of that action.
6. Never place raw pixels, OCR, typed text, secrets, or private window titles in logs.
7. Check cancellation and emergency-stop state between every primitive action.

## Command-center integration handoff

The UI owner can integrate without changing this module:

- Construct one `ComputerUseBroker` in the trusted host process. With no adapter,
  expose `broker.status()` exactly as unavailable.
- Add `broker.status()` as a separate `computer_use` state object. Do not map the
  integration registry's existing `READY` label to authorization.
- The UI may display redacted `AuditEntry` values. It must not accept raw grants,
  authorization assertions, screenshot bytes, OCR, or typed text over its HTTP API.
- Operator grant creation, revocation, and emergency stop need a separate trusted
  control surface with origin/authentication protections. No agent/provider response
  may call `add_grant`.
- Route lifecycle cancellation into the broker's per-run cancellation event. Keep the
  global emergency stop independently reachable.

Before activation, add a separately reviewed native adapter, OS-level containment,
an operator grant UI, secure local observation processing, and disposable-environment
tests. Live desktop activation remains out of scope and disabled.

## Synthetic verification

`tests/test_computer_use.py` uses only fake windows and byte strings. It covers target
and focus races, stale coordinates, expiry, mid-run revocation, cancellation,
emergency stop, unauthorized scopes, sensitive-operation blocks, observation release,
malicious on-screen instructions, bounded runs, redacted audit entries, and ambiguous
outcomes without replay.
