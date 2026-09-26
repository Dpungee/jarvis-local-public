# Conversational workspace preview

## Implemented interaction

The command center now opens a conversation rather than a task-submission form.
The top-left selector switches agent identity. New Agent configures an identity;
New conversation creates a persistent chat under that agent's project. The sidebar
shows the selected agent's conversations and project, with activity, files and
artifacts beside the main conversation. Explicit project membership does not grant
filesystem access. The operator may create another project and configure its agent.

Each message has an explicit intent: discussion, new work, steering, clarification
reply or delegation proposal. Discussion never creates work. Steering names an
existing run and is refused when it belongs to another chat/agent/project or is
already terminal. Messages progress from queued to delivered to applied; application
is recorded by a simulator checkpoint, not inferred from an HTTP response.
The composer stays usable during work and returns to Discussion after sending.

The offline simulator pauses for a clarification in the same chat. Replies and
steering update the existing work without resetting its checkpoint. Pause prevents
checkpoint advancement; resume preserves pending clarification; stop cancels active
and queued work for that identity. On process restart, active work is interrupted
and requires explicit resume. Sent messages, work, grants and attachments persist
in the local execution database. Browser session drafts and the selected chat are
restored independently per agent. Switching identities does not control their work.

## Composer and deterministic boundaries

- Attach: explicit `.txt`, `.md` or `.csv` selection, bounded to 20 KB in the picker
  and 20,000 characters server-side, at most five attachments per message. Protected
  names, path-bearing names and detected secrets are rejected. Staged records are
  removable until sent and scoped to an exact agent/project/chat. They remain local;
  the simulator does not consume them as model input. Selecting files does not grant
  cloud release authority.
- Permissions: only local attachment staging and read-only file preview are
  configurable. Both are checked by the API; grants do not transfer to another
  agent. Revocation prevents subsequent use, including sending a staged attachment.
  Existing authorized history is retained. No setting grants shell, cloud upload,
  external networking, microphone, wallet or delegation execution.
- Files: only startup-configured project roots, plus the per-agent file-preview
  grant. No root can be configured through HTTP. Traversal, device syntax, protected
  paths, symlinks, reparse points and hard-linked content are refused. File previews
  accept bounded source/text types and screen for secrets. These preview checks are
  not a production sandbox against a concurrently malicious host process; real
  agent filesystem execution remains gated.
- Model: changes apply to future messages/work. Previously admitted offline work
  retains its stored model binding. No private context is sent on a switch. Model
  labels are operator configuration, not evidence that a provider supports them.
- Usage: actual visible character counts are shown separately from unavailable
  token usage and context capacity. No tokenizer, provider measurements or verified
  model-specific capacity is supplied, so no token percentages are invented.
- Dictation: unavailable. No microphone or browser speech-recognition call is made.
  A verified local transcription adapter is required before implementing the
  recording/stop/cancel/edit workflow. Remote audio processing needs separate consent.
- Subagents: a request produces an exact readback and separate approval action.
  Approval records `APPROVED_NOT_DISPATCHED`; it creates no agent and assigns no
  work. Text saying "yes" or demanding authority cannot bypass that control.

## Real, simulated and unavailable

Real: local identity/chat persistence, message handling receipts, project navigation,
scoped file previews, attachment staging, grants/revocation, checkpoint controls,
authentication, origin checks and browser reload restoration.

Simulated: conversational replies, work execution, clarification requests and example
artifacts. They are labeled OFFLINE SIMULATION. This checkpoint adapter does not
answer real questions or perform agent work; no generated reply claims otherwise.

Unavailable/gated: Codex/Claude live chat, live model/tool loops, external integrations,
actual delegation, dictation, context-token accounting and file-writing/change
adapters. Project Changes explicitly reports that no writer is enabled. Memory bridge
activation, egress containment, release authorization and provider activation remain
separate gates. The inherited task-run API is retained for compatibility testing,
but is no longer the primary UI workflow.

## Run and verify

Run the synthetic preview without touching an existing store:

```powershell
python scripts/preview_conversation_workspace.py --port 8766
```

The process creates disposable local stores and one synthetic `brief.md`. It prints
a loopback URL containing a session token in the fragment. The browser removes the
fragment and retains the token in session storage so reload remains authenticated.
The token is never embedded in HTML or source. Keep preview URLs private.

Focused verification:

```powershell
python -m unittest tests.test_conversation_workspace tests.test_command_center tests.test_integration_harness tests.test_multi_agent_runtime
node --check jarvis/command_center.js
git diff --check
```

Headless browser scenario (requires existing Playwright and Edge): set
`JARVIS_TEST_URL` to the fresh disposable preview URL, and make the existing
Playwright package available through `NODE_PATH` if needed, then run:

```powershell
node scripts/verify_conversation_preview.cjs
node scripts/verify_composer_boundaries.cjs
```

This scenario verifies agent switching, new chat, per-agent draft restoration,
unavailable dictation with a mocked microphone guard, local grants, synthetic
attachment staging/removal, ongoing work, clarification, steering acknowledgement,
discussion during work, pause/reload/resume, synthetic file preview, context isolation,
artifact content, cancellation, JavaScript errors and responsive overflow at 1440,
900, 760 and 430 pixels. It does not open the host microphone or a visible browser.
The second scenario additionally verifies revocation after staging, preservation of
the draft on rejection, local-only attachment receipts, non-inheritance of grants,
future model selection, blocked live chat and unavailable token accounting.

The first full regression attempt was interrupted when the approved composer scope
expanded. Its partial output is not completion evidence. The final revision passed
57 focused tests (zero skips). The complete command
`python -m unittest discover -s tests -v` ran 4,287 tests in 815.375 seconds:
4,280 passed, 7 skipped, zero failures. Three Docker checks lacked a daemon, the
dataset-value scan lacked a cached dataset, and the sealed ladder v7, graph v4
and retrieval v5 holdouts lacked run tokens. Both headless browser scripts passed;
targeted Ruff, JavaScript syntax and diff-whitespace checks passed as well.
# Live subscription follow-up

The subscription text-chat foundation and current real-provider blockers are
documented in [SUBSCRIPTION_CHAT.md](SUBSCRIPTION_CHAT.md). Earlier simulator
acceptance below is not evidence of live subscription readiness.
