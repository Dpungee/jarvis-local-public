# JARVIS Desktop

## Integrated worker safety

Provider stream fragments pass through a stateful redactor before reaching Tk,
including credentials split across chunks. Closing cancels both workers, closes
a blocked Council provider, files interrupted sittings, and waits for both workers
up to the bounded shutdown deadline. Tool tracing and the current desktop layout
remain enabled alongside these protections.

`start_jarvis_ui.bat` (or `python -m jarvis ui`) opens the native Windows chat
window implemented in `jarvis/ui.py`, with `jarvis/ui_panes.py` (Memory,
Routines, Inbox, quick-ask window), `jarvis/ui_popups.py` (the `/`, `@` and
`+` menus, Snip), `jarvis/ui_sidepane.py` (the Diff · Artifact · File pane)
and `jarvis/ui_win.py` (tray icon, notifications, screen capture). It talks to
the same SQLite memory, Agent, approvals and safety controls as the terminal
and Presence interfaces. Nothing in the window bypasses an approval, and
nothing it shows about memory, approvals, tasks or routines comes from
anywhere but the store's own rows.

## Launch

- `python -m jarvis ui` runs the provider setup check first; if a provider
  still needs configuring the command prints why and exits with code 2.
- The window opens at the saved size and position (default 1280 × 820, minimum
  900 × 600), renders at native DPI, and follows the theme for its title bar on
  Windows 10 20H1+ / 11.
- Until the worker thread reports ready the composer is disabled and reads
  "Starting Jarvis…". If the model provider is unreachable (Ollama not running,
  a CLI provider signed out) the app starts anyway in a "Provider offline"
  state; the first send retries, and the palette offers "Reconnect model
  provider".
- If startup fails outright, a persistent notice above the composer carries
  the error with **Copy details** and **Retry** (restarts the worker thread);
  no modal dialog is raised.

## Layout

### Sidebar

| Element | What it does |
|---|---|
| Brand row | ☰ hides the sidebar (Ctrl+B). |
| Views | **Chat** · **Council** · **Memory** · **Routines**. Ctrl+M switches Chat ⇄ Council; Ctrl+Shift+M and Ctrl+Shift+R open Memory and Routines. The Council is documented in [COUNCIL.md](COUNCIL.md). |
| New chat | Ctrl+N. New chats start in the current project. |
| Search or jump to… | Opens the command palette (Ctrl+K). |
| Filter chats + list filter | The entry filters titles as you type. The button beside it cycles **Active · Archived · All chats**; the choice is remembered. |
| Chat list | Grouped **Pinned** (📌) then Today / Yesterday / Previous 7 days / Previous 30 days / Earlier. Row states: a pulsing 3-px accent bar on the left while that chat is working; an amber `!` on the right when it needs you (a pending approval scoped to it); bold title and a teal dot when a reply landed while you were elsewhere; muted text when archived; `⑂` before the title of a branch. Hovering a row shows Pin/Unpin and Archive/Unarchive glyphs; right-click opens Open · Rename… · Pin · Archive · Mark as unread · Copy title · Delete. |
| Footer | Current **project** (click to switch, filter, create, or open the workspace folder), **Approvals** with a pending badge (Ctrl+Shift+A), theme / export / shortcuts / Presence / settings glyphs, and a status card (online state, provider, background-control state, current activity). |

The sidebar list shows the 120 most recently active chats. Pin, archive,
unread and branch flags live in `data/desktop_chat_meta.json`, never in the
database; they are pruned against the full set of conversation ids the store
reports, so flags on older chats survive.

### Top bar

Chat title (double-click or F2 to rename inline; Enter saves, Esc cancels), a
subtitle with the message count and model profile (or the current activity
while working), the **context pill** (see Replies), the theme chip, then on the
right: Find (⌕, Ctrl+F), the **bell** (Ctrl+U) with a badge — an amber count of
items that need you, or a plain dot when there are only unread replies — the
context-panel toggle (◨, Ctrl+I) and Stop.

### Conversation column

Message cards. Your prompts sit right-aligned in a bubble with attachment
chips under them and Copy · Branch · Edit actions. Jarvis replies render
markdown (see Replies). Long chats build only the newest 40 cards; a
"Show 40 earlier · N hidden" button at the top adds the rest, and "Load
earlier messages" asks the store for the page before the oldest row shown
(300 rows per page). A "↓ Latest" button appears when you scroll up.

The empty state greets you by time of day with four suggestion cards that
prefill the composer.

## Composer

- Grows from one to nine lines. Enter sends; Shift+Enter or Ctrl+Enter inserts
  a newline. Up to 50,000 characters including inlined attachments; a counter
  appears past 80 %.
- **Mode chips** — Think (reasoning model), Code (coding model), Deep (30B).
  Tap to switch, tap again for Auto. The label beside them shows the active
  profile and model name; click it or use Ctrl+1…5 to cycle. The first Deep
  send asks once whether to load the 30B model and remembers the answer.
- **＋ menu** — **Files…** (file dialog), **Snip screen** (launches the Windows
  snipping overlay, then watches the clipboard for up to 60 s and attaches the
  capture), **Paste** (clipboard image, or copied file paths). Arrow keys,
  Enter and Esc work inside the menu.
- **Attachments** — images (PNG/JPG/WEBP/GIF, up to four, sent as image
  attachments) and text or code files (up to eight, inlined as fenced context
  blocks of at most 24,600 characters each, read from the first 200 KB of the
  file). Drop files
  from Explorer anywhere on the window, paste a screenshot with Ctrl+V (a
  bitmap on the clipboard is saved as PNG under `data/attachments/`), or paste
  file paths. Chips show what is attached; × removes one. Reading text
  attachments happens off the Tk thread; the send button reads "Reading
  files…" meanwhile.
- **/ commands** — typing `/` at the start opens a filtered list (↑↓ choose,
  Tab completes, Enter runs, Esc closes). Commands are handled locally and
  never sent to the model; an unknown command is refused with a notice.
  `/new`, `/model auto|fast|reasoning|coding|deep`, `/theme midnight|graphite|paper`
  (no argument cycles), `/project [name]` (choose, or create by name),
  `/task <prompt>` (queue for the worker), `/remember [fact]` (guided project
  fact), `/export [html]`, `/council`, `/context`, `/settings`, `/facts`
  (Memory view), `/schedule [prompt]` (Routines view; a prompt prefills a new
  routine), `/inbox`, `/help`.
- **@ file picker** — `@` at the start of a word opens a picker over the
  project workspace (index built on the worker thread after startup and after
  every reply, bounded to 2,000 entries; up to 12 matches ranked by basename
  prefix). Enter or Tab attaches the file and removes the `@token`.
- **↑ in an empty box** pulls the last queued follow-up back, otherwise recalls
  earlier prompts (history is redacted before it is persisted; ↓ walks
  forward).
- **◷** queues the typed prompt as a background task instead of sending it (an
  empty box opens the "Queue a background task" sheet).
- **Queued follow-ups** — while Jarvis is working, Enter or Tab queues the
  draft (up to eight) in a strip above the box, labelled "⏳ Queued". The next
  one is sent only when the reply ends `complete`, and only into the chat it
  was typed for. After a Stop or an error nothing auto-sends; the strip
  explains ("Not sent: you stopped the last reply") and each row gains
  **Send now**; × drops a row. While a turn waits for an approval the strip
  reads "Waiting for your decision above — send when ready".
- **Drafts** are kept per chat: switching chats stashes the text and
  attachment chips and restores them when you come back; a theme change keeps
  them too.
- **Win+H** starts Windows dictation into the box (the hint line and the
  shortcut list mention it; nothing in the app intercepts it).

## Replies

- **Markdown** — headings, bold/italic/strike, inline code, links (http/https
  only), bullet, numbered and task lists, block quotes, rules, and:
  - **Tables** as a real grid (bold header, zebra rows, cells wrapped to their
    share of the width) up to eight columns; wider tables fall back to an
    aligned monospace block.
  - **Code blocks** with a language label, a line-number gutter that stays
    aligned when lines wrap, **Copy** (turns into "Copied ✓"), **Save as…**
    (a save dialog), and for blocks over 60 lines a 20-line preview with
    **Open in pane**.
  - Replies longer than 6,000 characters get an **Open in pane** action in
    the footer.
- **Streaming** — providers that stream (claude-cli, cloud clients) show the
  text as it arrives, re-rendered as markdown at most every 400 ms and as
  plain text past 6,000 characters until the reply lands. Ollama does not
  stream; there the tool rows are the live feedback.
- **Working state** — a pulsing "Working · 3 s" (or "Loading model · 14 s")
  marker, the last four activity steps, and the last eight tool rows with a
  running timer.
- **Tool rows** — one row per tool call the agent made, recorded by wrapping
  the toolbox for the duration of the turn: `▸ read_file  jarvis\ui.py  ✓ 120 ms`;
  `✕ … failed · 1.5s · "first line of the error"`; `⏸ … needs approval`;
  `● … 2.1 s` while running. Three or more consecutive calls of one tool with
  the same outcome fold into `web_search ×3`. Click a row for **Arguments**
  (redacted; up to 10 keys of 400 characters), the **Result** preview (up to
  700 characters / 12 lines), and any file paths it mentioned (click to open,
  right-click for View in side pane · Open · Reveal · Attach · Insert path ·
  Copy path; "View" reads it in the side pane). Only absolute local paths, or
  relative paths that exist under the workspace, become links; URLs, URI
  schemes and UNC shares never do. Scripts and programs are only ever
  revealed in Explorer, never opened.
- **Ctrl+O** cycles the timeline detail: **Normal** (rows collapsed under
  `▸ Worked for 2.1s · 4 steps · 1 tool call`, click to open), **Verbose**
  (every row opened with arguments and results), **Summary** (only the one
  line unless something failed). The choice is remembered. The open detail
  also shows the run's trace id with Copy.
- **Metrics line** in the reply header: time · model (or `initial → final`
  after a failover) · elapsed · `1.8k in · 212 out` (or "tokens unmeasured") ·
  `first token 640 ms` (only when the provider streamed) · `N failovers`.
  Hover for profile, provider, model calls, retries, latency and trace id.
- **Context pill** (top bar): `6.2k in · 0.4k out` from the last reply's
  metrics, plus ` / 16k (39 %)` only when the route reported its context
  length (Ollama profiles). Amber at 75 %, red at 90 % with the hint "Long
  chat — consider a new one".
- **Footer actions** — Copy · **Regenerate ▾** (last reply only; the menu
  offers the current profile and every other one) · **Branch from here** (on
  earlier replies: a new chat holding everything up to that reply) · Continue
  (after a stop or an incomplete turn) · Quote (into the composer as `>`
  lines) · a ‹ 1/2 › pager when a reply was regenerated · a **+N −M** chip
  when the turn changed workspace files (opens the Diff tab) · Open in pane.
- **Edit** on one of your prompts branches the chat from just before it and
  puts the text in the composer; the original chat is untouched. Branches
  are titled "Branch · <title>", carry `⑂` in the sidebar, and copy the
  messages through the store's own `add_message`.
- **Error and stop states** — *Couldn't finish · <kind>* with the agent's
  reason, **Retry** (only when the agent reported the turn as retryable) and
  **Copy details** (kind, reason, model, trace id); *Model provider offline*
  with **Reconnect**; *Stopped by you · partial reply kept* with **Continue**
  (Stop reads "Stopping…" and is disabled until the worker acknowledges);
  *Stopped early: <reason>*; *Waiting for your approval above*.
- A reply that lands while another chat is open marks that chat unread and,
  per Settings → Notifications, raises a notification.

## Approvals

When the agent stops for a sensitive action, the reply card shows the
approval card fed by the store's row:

- Header **Needs your approval · <action> · expires in 23 min** (or
  "expired"). Body: the exact sanitised resource in monospace, the reason,
  and the scope (*This chat* / *Task #n*).
- Buttons: **Approve once** · **This chat · 24 h** · **Always** · … · **Deny**.
  *This chat · 24 h* appears only when the store marks the row eligible for a
  standing grant and its scope is this conversation; *Always* appears when the
  row is eligible. Otherwise the two are absent and a faint line reads:
  *Standing approval isn't available for this action — Jarvis only remembers
  exact read-only file requests.* That sentence is literally true of the store:
  standing grants exist only for `access_private_files` on the four exact
  read-only file tools, matched on the request's argument digest. Shell
  commands, `git status`, writes and everything else are approved once, every
  time.
- **Deny** reveals one entry, *Tell Jarvis what to do instead (optional)*,
  with **Send** and **Just deny**. Send records the denial and then sends
  `Denied approval #<id> (<resource>). Instead: <your text>` as your next
  message (queued if Jarvis is busy).
- Keys while the card has focus: Enter approves once, Esc opens Deny, Tab
  moves to the next pending card. The newest card takes focus on its own only
  when the composer is empty; otherwise the composer hint reads "Tab to review
  the approval" and Tab jumps there.
- Decided state replaces the buttons: `Approved once · 14:02`,
  `Allowed in this chat until tomorrow 14:02`, `Always allowed · Revoke`,
  `Denied` (`· reason sent` when an instruction went out). When the store did
  not accept a decision the card shows what the row says instead (`Already
  decided`, `Expired`, or the recorded outcome).
- After an approval the card offers **Retry with approval**: it re-sends the
  same request as a new turn (the new reply's header reads "Retried after
  approval #12"). Nothing resumes on its own.
- The same card, with the same buttons, appears under **Needs you** in the
  context panel (compact) and in the Approvals window (Ctrl+Shift+A), which
  also lists recent decisions. The quick-ask window shows a reduced card
  (Approve once · Deny · Open in Jarvis).
- **Standing approvals** are listed in Settings, each with its action,
  resource, grant time and expiry, and a **Revoke** button; the palette entry
  "Standing approvals" shows how many are active.

## Memory

- A governed fact command (`Remember this project fact: {...}`, `store it`,
  `Erase this project fact: …`) produces a **receipt chip** on the reply:
  `✓ Memory created / superseded / reasserted / confirmed proposal …` followed
  by `subject · predicate = value`. Both parts come from the memory store's own
  newest claim event for the conversation, never from the model's wording or
  from re-parsing your prompt; a rejection shows the same way without the
  tick. Click the chip to open the Memory view.
- When the store is still offering a fact for confirmation, the reply shows
  `Proposed fact: subject · predicate · value` with **Store it** (sends the
  literal message `store it`) and **Dismiss** (hides the chip; the offer lapses
  by the store's own rules — nothing else happens).
- **Remember a fact** (palette, context panel, `/remember`, Memory view) is a
  small dialog with Subject, Predicate and Value that builds the exact JSON
  command and sends it.
- **Memory view** (Ctrl+Shift+M, `/facts`): facts grouped by subject; each
  row shows `predicate · value` and a meta line with the source (*operator* or
  *model-proposed · confirmed*; hover for the raw store enums), the scope and
  when it was stored. `▸ history (n)` fetches the superseded chain from the
  store (● current, ○ superseded, ✕ retracted). Actions: **Update…** (opens
  the Remember dialog prefilled with the subject and predicate), **Erase…**
  (inline confirm, then sends `Erase this project fact: {"subject": …,
  "predicate": …}`; the receipt lands in the chat), **Copy**. The filter box
  narrows the list as you type and re-asks the store once the query is two
  characters or longer.
- The context panel's Memory section lists the store's recent memories
  (click to copy) and links to a palette search.

## Routines

**Routines** (Ctrl+Shift+R, `/schedule`) lists the store's scheduled jobs:
name, **Active/Paused** pill, `Every 6 h · Next: in 3h · Last: task #81 ·
complete`, the prompt, and the last task's error if it recorded one.
Actions: **Queue a run now** (queues a one-off background task with the
routine's prompt; it does not run the routine itself), **Pause/Resume**,
**Delete** (the row hides and a "Routine deleted · Undo" strip shows for
five seconds before the store hears the delete). **New routine** opens an
inline form: Name, Prompt, Project, Cadence (Every hour · Every 6 h · Every
day · Every week · Custom minutes). Cadences are intervals from creation;
there is no clock alignment. A footer line says *Runs will not start until
`python -m jarvis worker` is running* whenever the worker heartbeat is missing
or stale.

## Inbox and notifications

- The **bell** (Ctrl+U, or Ctrl+Alt+U, or `/inbox`) opens a popover under
  it: **Needs you** (pending approvals), **Unread replies**, **Finished tasks
  & routine runs**, **Errors** (notices, and "Worker offline" while tasks are
  queued and the worker is not running). Rows show the title, one line of
  detail and an age; clicking opens the chat and scrolls to the card, opens
  the task sheet, or explains what to start. **Mark all read** clears the
  badge. Esc or clicking elsewhere closes it.
- **Notifications** use the tray icon's balloon (shown by Windows as a toast
  under the name "Jarvis"). Settings → Notifications: *Reply finished*
  (never / unfocused / always; "unfocused" skips the chat you are looking
  at), *Approval needed*, *Background task finished*, *Tray icon* (Open
  JARVIS · New chat · Quit; restart to apply), *Quick-ask window on the
  global hotkey*, *Keep the window on top*. Clicking a balloon brings the
  window forward on that chat. If the tray icon is off, `jarvis.ui_win.notify`
  is used instead (a Windows toast via a hidden PowerShell process, then a
  balloon); if nothing could be shown and the window is unfocused, the
  taskbar button flashes.
- **Task detail** — task rows in the context panel and inbox open a sheet
  with the prompt, status pill, `attempts n/max`, last error, the result
  rendered as markdown, a link to any approval it waits on, **Copy result**
  and **Bring into chat** (quotes the result into the composer).
- **Notices** — errors that happen while idle, "Worker offline", an unknown
  slash command, a dropped message, and delete/undo confirmations appear in a
  dismissible strip above the composer (at most three, each with ×, some with
  Copy details or Undo). The 2.6-second toast near the composer is reserved
  for confirmations and never covers Send.

## Side pane (Ctrl+Shift+P)

A right-hand pane with three tabs; its visibility is remembered.

- **Diff** — the workspace files a reply changed, from a text-file index
  taken before the turn and compared after it (bounded to 5,000 files and
  1 MB per file; larger workspaces record "Changes not recorded"). A file list
  with A/M/D glyphs and `+N −M` counts, a coloured unified diff with line
  numbers, **Reveal**, **Ask Jarvis to revert this file** (sends the sentence
  "Revert the file <path> to how it was before your last change, and explain
  what you changed." — the agent's own gates apply; the desktop never writes
  to the workspace), **Copy diff**. F3 steps to the next hunk, then the next
  file. The diff text (≤ 200 KB per turn) is stored in the reply's timeline
  record, so it survives reopening the chat.
- **Artifact** — a long fenced block or a long reply in full, with line
  numbers, **Quote selection**, **Save as…** and **Copy**.
- **File** — a read-only view of any file reached from a tool row, the
  context panel or the Diff tab (1 MB shown, binaries refused), with
  **Reveal**, **Attach**, **Open** (hidden for scripts and programs; those are
  only revealed) and a local find (Ctrl+F inside the pane, F3 next). Esc
  inside the pane hides it.

## Quick-ask window (companion)

Ctrl+Alt+J from any app opens a small always-on-top tool window titled
"Jarvis": `Jarvis · <profile>` · **New chat** · **⤢ Open in Jarvis** · ✕; a
one-to-four-line box (Ctrl+V pastes a clipboard image as a chip, up to four);
the reply streams in as text and is re-rendered as markdown when it lands
(up to twelve lines, then it scrolls); a status line (`Working · 3 s`,
`Loading model · 14 s`, then model and elapsed, or "Stopped early: …"); and a
reduced approval card when a turn stops for one. Enter sends into a fresh
conversation the first time (the main window switches to that chat and it
appears in the sidebar); later Enters continue it until New chat. Esc hides
the window; pressing the hotkey while it is open raises the main window
instead. Its position is remembered. When the setting *Quick-ask window on
the global hotkey* is off, the hotkey brings the main window forward.

## Projects

The sidebar footer shows the current project. Click it to switch, to show
chats from all projects or only this one, to create a project (Ctrl+Shift+N;
it gets its own isolated workspace folder), or to open the workspace folder.
New chats, queued tasks and routines bind to the current project; the `@`
picker indexes its workspace; the context panel's "Changed in this chat"
section lists files under it modified since the chat started.

## Search and Find

- **Ctrl+K** — the command palette: actions (new chat, approvals, stop,
  regenerate, export Markdown/HTML, shortcuts, Council, Presence, sidebar,
  reconnect provider, context panel, side pane, find, timeline detail,
  settings, standing approvals, inbox, memory, routines, quick-ask window,
  keep on top, new project, queue a task, remember a fact, open workspace,
  pin/archive/branch this chat, chat list filter), projects, model profiles,
  themes, chats by title (archived ones only under the Archived/All filter),
  and — after three characters — **message text across all chats**, shown as
  an excerpt with the match in bold. Opening a hit loads that chat, scrolls to
  the message and flashes it. ↑↓ / Enter / Esc.
- **Ctrl+F** — a find bar over the open chat that highlights every match;
  Enter / Shift+Enter or **F3 / Shift+F3** step through them (F3 opens the bar
  when it is hidden).

## Settings (Ctrl+,)

A scrollable, resizable window capped at 80 % of the screen. Everything is
stored locally in `data/desktop_ui.json`.

| Section | Contents |
|---|---|
| Appearance | Theme (Midnight · Graphite · Paper; the window re-skins in place and keeps your draft), text size A− / A+. |
| Behaviour | Default model profile, global hotkey on/off (restart to apply), context panel, flash the taskbar when a reply finishes unfocused. |
| Notifications | Reply finished (never / unfocused / always), approval needed, background task finished, tray icon, quick-ask window on the hotkey, keep the window on top. |
| Standing approvals | Every This-chat / Always grant with its action, resource, grant time and expiry, and **Revoke**. |
| Where things live | Workspace and data folders with Open buttons. |
| Models | The configured model name per profile. |
| Shortcuts | Read-only; a button opens the full list. Rebinding is not available. |

## Themes

Three themes, switched with Ctrl+T, the ◐ button, the top-bar chip, `/theme`,
the palette or Settings: **Midnight** (near-black, teal accent, default),
**Graphite** (neutral dark greys, white accent) and **Paper** (warm light,
terracotta accent). Faint text in every theme reaches 4.5:1 against every
surface it is drawn on. The Council room and the Windows title bar follow the
active theme.

## Keyboard shortcuts

Every chord below is bound in the app and listed in the Shortcuts window
(Ctrl+/ or F1), which is generated from the same table. Chords are ignored
while the focus is in a text field other than the composer (the palette, Find,
the deny entry, dialogs) and in secondary windows.

**Conversation**

| Keys | Action |
|---|---|
| Enter | Send message · on a focused approval card: Approve once |
| Shift + Enter | New line |
| Tab | While Jarvis is working: queue the draft as a follow-up |
| ↑ in an empty box | Pull the last queued follow-up back, else recall earlier prompts |
| Esc | Focused approval card: Deny · palette or popup: close · while working: Stop |
| Ctrl + N | New chat |
| Ctrl + L | Focus the composer |
| Ctrl + Shift + C | Copy the last reply |
| Ctrl + R | Regenerate the last reply |
| Ctrl + O | Timeline detail: Normal → Verbose → Summary |
| F2 / double-click the title | Rename this chat inline (Enter saves, Esc cancels) |
| Win + H | Windows dictation into the composer |

**Navigate**

| Keys | Action |
|---|---|
| Ctrl + K | Command palette: chats, actions, message search |
| Ctrl + M | Switch between Chat and Council |
| Ctrl + Shift + M | Memory view: governed facts |
| Ctrl + Shift + R | Routines view: scheduled runs |
| Ctrl + Tab / Ctrl + Shift + Tab | Next / previous chat in sidebar order |
| Ctrl + I | Context panel: project, changed files, approvals, tasks, memory |
| Ctrl + Shift + P | Side pane: Diff · Artifact · File |
| Ctrl + F | Find in this chat |
| F3 / Shift + F3 | Next / previous Find match |
| Ctrl + U (or Ctrl + Alt + U) | Inbox: approvals, unread replies, finished tasks, errors |
| Ctrl + Shift + U | Mark this chat unread / read |
| Ctrl + Alt + P | Pin / unpin this chat |
| Ctrl + Shift + A | Review approvals |
| Ctrl + Shift + N | New project |
| Ctrl + E | Export this chat (Markdown or HTML) |
| Ctrl + B | Toggle the sidebar |
| Ctrl + , | Settings |
| Ctrl + / or F1 | This shortcut list |
| / | Slash commands: /new /model /theme /project /task /remember /export /facts /schedule /inbox /help |
| Ctrl + Alt + J (global) | Quick-ask window; press again to raise the main window |

**Models & look**

| Keys | Action |
|---|---|
| Ctrl + 1 … 5 | Auto · Fast · Reasoning · Coding · Deep |
| Ctrl + T | Cycle theme (Midnight → Graphite → Paper) |
| Ctrl + +/- | Zoom text |

Branch, Archive, Delete, Snip and Keep on top have no chord; they live in the
card footers, the sidebar row menu, the `+` menu and the palette.

## Export

Ctrl+E (or `/export`, `/export html`, the palette) saves the chat as Markdown
or a self-contained HTML page; the save dialog's type filter or the extension
you type picks the format. Every turn is written with its time, attachment
names, memory receipt line and stop marker. Writes happen off the Tk thread.

## Behaviour notes

- **Threads.** The worker thread (`JarvisSession`) owns SQLite and the Agent
  and is the only place that touches them; the Tk thread only receives
  redacted, bounded events. A two-thread I/O pool handles clipboard decoding,
  attachment reads, path probes, the Presence port probe and export writes,
  posting results back through the same event queue. The global hotkey and
  the tray icon each run on their own thread (Windows delivers hotkeys and
  tray messages to the registering thread).
- **Sidecar files** under the data directory, none of which hold prompts,
  secrets or raw tool payloads:
  - `desktop_ui.json` — theme, model profile, zoom, sidebar and context-panel
    state, side-pane state, chat-list filter, timeline mode, window and
    quick-ask geometry, notification and hotkey settings, the Deep-model
    confirmation, and a redacted prompt history (60 entries).
  - `desktop_chat_meta.json` — pinned / archived / unread / branched_from per
    conversation id.
  - `desktop_timelines/<conversation_id>.json` — per reply: steps, tool rows,
    metrics, status, reason, model, elapsed, receipt, regenerated versions,
    approval id and the workspace diff, keyed by the assistant message id and
    fingerprinted against the message text. Ordinary record data is capped at
    64 KB with a separate 200 KB budget for unified diff text, and files
    at 2 MB and 400 records, written atomically. Chats from before v3 have no
    timeline and show none; a reply whose text changed under the sidecar shows
    none rather than a wrong history.
  - `attachments/` — pasted screenshots and snips as PNG.
- **Worker heartbeat.** `data/worker.heartbeat` older than 120 s, or missing,
  counts as offline: the context panel, inbox and Routines footer say so, and
  a notice appears once when tasks are queued.
- **Every string** shown in the window passes through secret redaction and a
  display bound before it reaches Tk.
- **Focus and keys.** Buttons, card actions, code-block actions and the diff
  chip take keyboard focus, show a focus ring and activate with Enter or
  Space. Sidebar rows, empty-state suggestion cards and context-panel rows are
  mouse-only (Ctrl+Tab cycles chats; F2 renames the open one). Chords bound
  on the whole window check the focused widget first.
- **Closing.** The window saves its geometry, commits any pending chat
  deletions, and waits for the worker: up to 3 s when idle, up to 10 s while a
  reply is mid-run (the title reads "Finishing…"). Pending Tk timers are
  cancelled so nothing fires on a destroyed window.
- **Drag-and-drop** uses `WM_DROPFILES` on the Tk window and degrades silently
  when unavailable. If another app already owns Ctrl+Alt+J the app says so in
  a notice, raised only after Windows refused the registration.

## What the app deliberately does not do

- **No client-side approval decisions.** Every Approve / Deny / This chat /
  Always button calls the store; the card changes only after the store
  confirms, and standing grants exist only where the store allows them.
- **No thinking or reasoning display.** No provider surfaces a thinking
  channel to the desktop today, so none is shown or fabricated.
- **No auto-resume.** A turn that stopped for an approval stays stopped;
  **Retry with approval** re-sends it as a new turn and says so.
- **No workspace writes.** Revert asks Jarvis; Save as… and Export are
  user-initiated save dialogs; scripts and programs are revealed, never run.
- **No temporary chats, read-aloud, window capture, rebindable shortcuts,
  or per-project instructions** — cut or deferred in the v3 design.
