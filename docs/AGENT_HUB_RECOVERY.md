# Agent Hub recovery and chat interface

## Active scope: agent-first conversational execution (2026-09-25)

The operator clarified that regular chat describes the interface, not removal of
agent capabilities. The text-only interpretation below is superseded. `/messages`
now queues the existing permission-enforced executor, with an atomic durable
chat/request/run binding. UI stays on the same chat and shows results, tool activity,
artifacts and approvals inline. New messages remain available while an earlier run
is working. Execution is serialized per agent using the existing runtime controls.
No phrase classifier or separate chat/task switch determines tool eligibility.

Completed legacy chat messages remain available as context. New execution history
is loaded for the same agent and chat at run time so queued follow-ups see earlier
results. Recent context is bounded; full stored history remains intact. Request IDs
are content- and chat-bound, reject conflicting reuse and deduplicate execution.
Actual grants, workspace scope, provider/model selection and approval gates still
come from the existing executor. This does not implement missing tool integrations
or promise instantaneous model/network completion or unlimited authority.

The privacy regression applied a graph entity-label screen (which refuses values
over 512 characters) to an entire JSON conversation. Conversation screening now
checks normalized prose in overlapping 512-character windows, retains the overall
80,000-character bound, and rejects long opaque tokens plus actual secrets/private
identifiers. Public HTTPS URLs no longer match a Windows-drive heuristic. Graph
identifier redaction itself is unchanged. Adversarial tests cover middle/end/edge
placements, private paths, email, internal IP and API-key shapes.

The live Hub was idle before restart and backed up. Pre-existing agents, settings,
tasks, chats, messages and folders compared unchanged afterward. No previous failed
message was automatically resent. Browser inspection was authenticated and read-only;
executor/tool tests use synthetic providers and tools, not external agent delegation.

## Superseded tool-free interpretation (2026-09-25)

The operator requested regular chat only, not a task UI styled as chat. The Hub
now connects the existing `LiveConversations` persistence and text-only subscription
transports. `/api/agents/:id/chats` creates a scoped chat, `/messages` accepts only
`chat_id`, `body`, and `request_id`, and `/chat?chat=...` reads its messages.
Messages cannot supply work mode, tools, attachments, delegation, or task IDs.
Absent text transports fail closed instead of falling back to simulated work.
Claude uses the existing trusted executable discovery; Codex uses the existing
subscription profile and retains the credential-free isolation probe.

The main chat UI has no task submission, pause/task controls, permission editor,
or task progress. It includes persisted chat navigation, follow-ups, a composer,
Enter-to-send/Shift-Enter newline and model settings. Legacy task APIs and records
remain for compatibility; no chat message routes through them. New agents created
from this UI receive no task tool grants. Existing grants remain unchanged.
Old task bookmarks now open the associated agent's regular chat; task records
remain available through the retained backend APIs.

Verification: synthetic responsive browser checks, mock-transport HTTP boundaries,
multi-turn context and an authenticated read-only render of the live server. One
bounded synthetic prompt through the actual selected Claude subscription returned
the requested text with tool-free initialization verified. No test conversation
was inserted into the operator's saved history. In-app reopening was queued by
the desktop tool; this is not proof that its foreground tab has refreshed.

## Active scope

Restore the original operator Hub and saved state, reconcile the incompatible
runtime/provider interfaces, and retain the chat-first design. Preserve all agent
identities, configuration, permissions, task/chat/project records and subscription
profiles. No paid API substitution, permission expansion, publication, or delegation.

## Root cause and compatibility decision

Worktree branch: `codex/command-center`, base commit `93119c4`; unpublished.
Changed files in this recovery: `jarvis/agent_hub.py`, new
`jarvis/agent_hub_runtime.py`, `jarvis/agent_hub_static/index.html`,
`jarvis/agent_hub_static/hub.js`, `jarvis/agent_hub_static/hub.css`,
`tests/test_agent_hub_recovery.py`, `scripts/verify_hub_chat_layout.cjs`,
`docs/AGENT_HUB_RECOVERY.md`, and `PROJECT_STATUS.md`.

Two implementations occupied `agent_runtime.py`. Their constructor, settings,
task-control, provider, audit and permission APIs were different, as were the SQLite
tables despite both using the filename `hub.db`. Adding missing constants would not
repair the constructor or storage mismatch.

The complete original source was available in the preserved implementation. It is
restored as **`jarvis/agent_hub_runtime.py`**, and `agent_hub.py` imports it explicitly.
This matches the operator's existing `hub_agent_settings`, task, event, artifact and
audit schemas. The competing `agent_runtime.py` and `agent_providers.py` are preserved
unchanged, not overwritten or silently migrated. The restored runtime rejects the
other task schema before table creation/recovery. This is an explicit separation of
two implementations, not an assertion that their database formats are interchangeable.

## Reconciled boundaries

- The Hub's existing complete task-control, provider, artifact, event and audit
  contracts now target their matching implementation.
- Subscription model clients use `agent_providers.build_agent_client`: shared login
  profile, one provider, and newest already-trusted Claude executable. Model checks
  select that same trusted binary, not whichever alias happens to be on PATH.
- `tool_groups` can be supplied at the HTTP adapter. Every unspecified permission
  becomes false for an explicit list. Unknown groups and conflicting `permissions`
  plus `tool_groups` are refused. A standalone `documents` grant is unrepresentable
  in the original schema (documents are within files_write) and is refused rather
  than broadened into general file writes.
- Actor is derived by HTTP authentication and stored in the audit log. It is never
  trusted from a request payload. Existing operator-to-owner lifecycle mapping stays
  intact; no mismatched actor argument is sent to the alternate runtime.
- Artifact content now verifies its SHA-256 before serving a stored blob.
- No live operator task was submitted during recovery. Provider status checks are
  not proof of a successful fresh model run.

## Interface

Project folders, agents and saved task conversations are visible in the sidebar.
Agent entry opens a message composer with a real task-model override; request and
result appear as messages. Task activity, controls, files and configuration remain
available. Mobile navigation collapses behind a menu button.

This is a chat-style interface to the existing task/conversation backend. Completed
tasks require a new conversation; running tasks support the existing steering API.
Progress is polled, not token-streamed. No new attachment upload, independently
isolated chat-memory layer, or automatic delegation was added.

## Verification

- `python -m unittest tests.test_agent_hub_recovery -q`: 12 passed. Covers complete
  read routes; auth/origin refusal; authenticated audit identity; group restrictions;
  project/lifecycle/configuration/task controls; saved restart without automatic
  rerun; provider-client binding, model verification/default/override contracts;
  incompatible-schema refusal; injected execution
  with a denied tool, successful artifact and corrupt-blob refusal.
- Final `python -m unittest tests.test_agent_hub_recovery tests.test_command_center
  tests.test_conversation_workspace tests.test_command_center_agent_status`:
  51 passed in 14.227 seconds, zero skips.
- `node scripts/verify_hub_chat_layout.cjs`: passed against the owning checkout's
  static files with synthetic API data. Desktop/mobile rendering, model submission,
  saved navigation, drafting during updates and four responsive widths are checked.
- `python -m ruff check --select E9,F63,F7,F82 jarvis/agent_hub.py
  jarvis/agent_hub_runtime.py tests/test_agent_hub_recovery.py`: passed.
  Full Ruff reports inherited style/broad-exception warnings in the restored source;
  it is not claimed clean.
- Operator service restored on its original port and state. Authenticated browser
  shows the saved agent, selected model and new composer. Original records compared
  equal across runtime, execution and Hub databases, excluding provider-health/event
  refresh. Private launch token was used only for local browser reconnection.
- `python -m unittest discover -s tests`: 4,343 run in 748.639 seconds,
  4,336 passed and 7 skipped, zero failures. Two tests were added after full-suite
  discovery and are included in the later 51-test focused pass above. Existing
  resource/deprecation warnings and non-enforced performance-budget overruns were
  printed; passing tests are not a production/performance acceptance claim.
- No commit or publication. Remaining limits: no fresh live model task was executed;
  the interface retains task-based, polled conversation semantics described above.
