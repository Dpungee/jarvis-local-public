# Authenticated local broker

Roadmap Phase 1 asks for "an authenticated local broker or Windows named pipe with
user ACLs". `jarvis/local_broker.py` provides it and the Companion indicator is its
first consumer.

## Why loopback HTTP was not enough

Loopback TCP cannot tell the operator's own processes apart from any other local
process: another Windows account on the same machine, a sandboxed (AppContainer or
low-integrity) process, or anything that can spoof an `Origin` header. Presence
already rejects browser-origin attacks, but a hostile local process could still drive
the Companion control plane over `127.0.0.1`. The roadmap named this gap explicitly.

## What the broker enforces

- **Windows named pipe** `\\.\pipe\jarvis-local-<scope>-<nonce>` where `<scope>` is
  derived from the data directory and `<nonce>` is random per Presence start, so two
  installs never share a pipe and nobody can reserve the name in advance. The
  indicator receives the exact name on its command line.
- **Protected DACL and mandatory label** `D:P(A;;GA;;;<current user SID>)` plus
  `S:(ML;;NRNW;;;<integrity SID>)`: only the user that started Presence, at the same
  or higher integrity level, can open the pipe; a low-integrity process cannot even
  open it read-only. Remote clients are rejected (`PIPE_REJECT_REMOTE_CLIENTS`).
- **Single owner**: the server creates the only instance with
  `FILE_FLAG_FIRST_PIPE_INSTANCE`. If the name already exists, Presence refuses to
  start the broker and reports `local_broker_unavailable`; it never talks behind an
  impostor.
- **Peer verification on both sides**: after every connection the server checks the
  client process token (same user SID, integrity not below the server's) and the
  client checks the server process token the same way; a mismatch is refused and
  counted (`local_broker_rejected`, rate-limited to one event per second with a
  count). The intended policy is equal integrity inherited from Presence, which
  spawns the indicator itself; a manually launched indicator at a different level
  is refused and degrades like an HTTP failure.
- **Closed envelope**: one 4-byte length-prefixed UTF-8 JSON object per connection,
  at most 64 KiB and at most 32 nesting levels, of the form
  `{"kind": <str>, "payload": <object>}`. Anything else is refused before a handler
  runs, and every handler failure (any exception, even `SystemExit`) becomes a refusal
  carrying only the exception class name while the serving thread continues.
- **No new authority**: the handler decides which request kinds exist. Presence exposes
  exactly the four Companion-indicator operations that the loopback HTTP routes already
  served (`companion.indicator`, `companion.action`, `companion.control`,
  `companion.suggestion`), calling the same runtime methods with the same validation.

## Fail-closed behaviour

- Non-Windows hosts: `LocalBrokerUnavailable`; callers keep their existing behaviour
  and nothing new is exposed. The indicator was already Windows-only.
- Broker cannot start (name squatted, Win32 error): Presence emits
  `local_broker_unavailable` and does **not** start the indicator over HTTP.
- Indicator configured with a pipe: every request goes through the pipe; a broker
  failure surfaces as an error, never as a silent HTTP fallback.

## Known limits

- Serving is blocking and sequential; a same-user client that connects and never
  sends a frame delays later clients until it disconnects or Presence stops
  (stopping always completes because the parked I/O is cancelled). The DACL and the
  label keep that within the operator's own processes at the same integrity level,
  which the threat model treats as trusted.
- The pipe is a control channel for local Jarvis processes. It does not replace the
  browser-facing Presence HTTP interface, and it is not the future private-to-public
  bridge transport (roadmap Phase 4), although that bridge can reuse it.

## Tests

`tests/test_local_broker.py` covers the contract on every platform and the real pipe
on Windows: round trips, refusal of a second server on the same name, missing pipe,
oversized and malformed frames, handler failures, peer-identity mismatches on either
side, and clean shutdown. `tests/test_companion_indicator.py` and
`tests/test_presence.py` cover the wiring; `tests/test_public_process_isolation.py`
adds the negative import checks for the public process.
