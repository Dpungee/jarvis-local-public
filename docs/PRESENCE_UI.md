# Presence interface guide

`start_jarvis_presence.bat` (or `python -m jarvis presence`) serves the
browser interface from `jarvis/presence.html`, `presence.css`, `presence.js`
and the local API in `jarvis/presence.py`. This page describes the operator
surface; the security model (loopback only, paired remote sessions, same-origin
JSON, CSP) is unchanged and documented in `SECURITY.md`.

## Navigation

The sidebar groups every view:

| Group | Views |
|---|---|
| Workspace | **Overview** (dashboard), Projects, Project files |
| Automation & memory | Scheduled, Dispatch, **Memory**, **Activity**, Performance |
| Devices & safety | Devices, Companion, Public Presence (collapsed by default) |
| — | Settings |

Groups collapse and remember their state. Below the navigation: the project
switcher, pinned projects, and the chats of the active project with a filter
box. Each chat row shows its message count, age, a "working" marker, an unread
badge for replies that finished while you were elsewhere, and hover tools to
pin, open a menu (open / rename / pin / export / delete) or delete.

`Ctrl+K` opens the command palette: actions, runtime control, every view,
every chat and every project, filtered as you type. `Ctrl+/` lists shortcuts.

## Conversation

- Replies render as markdown built from DOM nodes only (no HTML injection is
  possible): headings, emphasis, inline code, safe http(s) links, lists, task
  lists, quotes, rules, tables, and fenced code blocks with a language label
  and a Copy button. Streaming text stays plain until the final message
  arrives, then it is rendered.
- Every message has a time stamp and hover actions: Copy and Edit for your
  prompts; Copy, Regenerate and Quote for replies. Replies also show model,
  status and elapsed time.
- Quick prompt chips above the composer prefill common requests; the Code mode
  switches them to review / tests / fix / refactor / explain.
- Images can be attached with the `＋` button, pasted from the clipboard, or
  dropped onto the composer.
- A "jump to latest" button appears when you scroll up. A character counter
  appears near the 50,000-character limit.
- Optional desktop notifications (Settings) announce a finished reply while
  the tab is in the background; the tab title shows the unread count.

## New views

- **Overview** — runtime state with Pause / Resume / Emergency stop, jobs in
  progress with per-job Stop, what needs you (approvals, standing grants,
  features to review), recent chats, scheduled work, and performance headline
  numbers. The sidebar status card also shows a clickable runtime chip.
- **Memory** — search governed memory and browse recent memories. Results
  include the recall diagnostic (discovery mode, candidate count, dropped
  terms, abstention). Queries that contain secrets or private identifiers are
  refused by the memory layer and return nothing.
- **Activity** — the bounded, redacted audit log with category filters.
- **Scheduled** — now lets you queue a background task for the current project
  with a model profile, and pause / enable learning topics and backlog items.
- **Settings** — theme (system / dark / light), density, text size,
  notifications, plus durable preferences (list and add).

## Keyboard shortcuts

| Keys | Action |
|---|---|
| Enter / Shift+Enter | Send / new line |
| Esc (while typing) | Stop the current request |
| Ctrl+K | Command palette |
| Ctrl+Shift+O | New chat |
| Ctrl+Shift+S | Toggle split view |
| Ctrl+Shift+E | Export this chat as Markdown |
| Ctrl+B | Toggle the sidebar |
| Ctrl+/ | Shortcut list |
| Double-click title | Rename the chat |

## Performance

Event polling stays at 150 ms while a job is running and 700 ms just after
activity, then backs off to 1.5 s after 30 s idle and 3 s after 2 min; a
hidden tab polls every 4 s and refreshes status every 20 s instead of 5 s.
Message cards, markdown and views are rendered incrementally with
`textContent`, and view renders remain generation-guarded so a stale response
can never paint over a newer view.

## API additions (all same-origin, session-gated like the rest)

| Method and path | Purpose |
|---|---|
| `GET /api/memory/recent?limit=` | Newest non-claim memories (1–200), redacted and bounded |
| `POST /api/memory/search` `{q, limit}` | Ordinary memory search (query ≤ 500 chars, limit 1–50) with the recall diagnostic |
| `GET /api/activity?limit=` | Audit rows (1–500) with summarised, redacted details |
| `GET /api/preferences` / `POST /api/preferences` `{name, value}` | Durable operator preferences |
| `POST /api/tasks` `{prompt, project_id?, model?}` | Queue a background task (`auto`, `fast`, `reasoning`, `coding`, `deep`) |
| `POST /api/schedule/{learning|backlog}/{id}/{enable|disable}` | Toggle a learning topic or backlog item |
| `POST /api/conversations/{id}/rename` `{title}` | Retitle an operator conversation (internal Companion chats are refused) |
| `GET /api/conversations/{id}/messages` | Now includes `created_at` per message |

Runtime events added: `conversation_renamed`, `task_queued`.
