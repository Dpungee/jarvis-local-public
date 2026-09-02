# JARVIS Desktop

`start_jarvis_ui.bat` (or `python -m jarvis ui`) opens the native Windows chat
window implemented in `jarvis/ui.py`. It talks to the same SQLite memory,
Agent, approvals and safety controls as the terminal and Presence interfaces;
nothing in the window bypasses an approval.

## Layout

| Area | What it does |
|---|---|
| Sections | The sidebar nav switches the main pane between **Chat** and **Council** (Ctrl+M, or the command palette). The Council seats JARVIS and his five specialists around a table and runs a chaired, tool-free meeting that files an agenda, minutes and a report — see [COUNCIL.md](COUNCIL.md). |
| Sidebar | New chat, command palette launcher, chat filter, chats grouped by day (Today / Yesterday / Previous 7 days / …), model-profile pills, approvals with a pending badge, theme / export / shortcuts / Presence buttons, and a live status card (online, provider, background-control state, current activity). |
| Top bar | Chat title (double-click to rename), message count and model, theme chip, Stop. |
| Conversation | Message cards. Your prompts sit right-aligned in a bubble; Jarvis replies render markdown: headings, bold/italic/strike, inline code, links (http/https only), bullet and numbered lists, task lists, block quotes, rules, tables, and fenced code blocks with a language label and a Copy button. Each card has Copy / Edit (user) or Copy / Regenerate / Quote (assistant) actions and a timestamp; replies show the model and elapsed time. |
| Working timeline | While Jarvis works, the reply card shows a pulsing "Working" marker and the last steps reported by the agent (model selection, tools, processing). When the reply lands the timeline collapses to "Worked for 2.1s · 3 steps · 1 tool calls". Replies stream in as they are generated. |
| Empty state | Time-of-day greeting with four suggestion cards that prefill the composer. |
| Composer | Grows from one to nine lines. Enter sends, Shift+Enter inserts a newline. `＋` attaches up to four PNG/JPEG/WebP/GIF images (5 MiB each) which are sent as image attachments. The model label toggles the profile; a counter appears near the 50,000-character limit. |
| Approvals | A dedicated window lists pending approvals as cards with the exact sanitized resource and Approve once / Deny buttons. A reply that needs approval shows an inline banner with a "Review approvals" button. |
| Command palette | Ctrl+K: actions (new chat, approvals, stop, regenerate, export, shortcuts, Presence, sidebar, reconnect provider), model profiles, themes, and every chat by title. Arrow keys / Enter / Esc. |

## Themes

Three themes, switched with Ctrl+T, the ◐ button, the top-bar chip, or the
palette:

- **Midnight** — near-black with a teal accent (default).
- **Graphite** — neutral dark greys with a white accent.
- **Paper** — warm light theme with a terracotta accent.

The Council room follows the active theme: the wall, floor, table and the fade
that carries depth are all derived from it, so the room is lit to match.

The Windows title bar follows the theme (dark or light) on Windows 10 20H1+
and Windows 11. The choice, model profile, zoom, sidebar state and window
geometry persist in `data/desktop_ui.json` (never any prompt or secret).

## Keyboard shortcuts

| Keys | Action |
|---|---|
| Enter / Shift+Enter | Send / new line |
| Esc | Stop the current request (or close the palette) |
| Ctrl+N | New chat |
| Ctrl+K | Command palette |
| Ctrl+M | Switch between Chat and Council |
| Ctrl+L | Focus the composer |
| Ctrl+B | Toggle the sidebar |
| Ctrl+R | Regenerate the last reply |
| Ctrl+Shift+C | Copy the last reply |
| Ctrl+Shift+A | Review approvals |
| Ctrl+E | Export the chat as Markdown |
| Ctrl+T | Cycle theme |
| Ctrl+1 … Ctrl+5 | Auto · Fast · Reasoning · Coding · Deep |
| Ctrl+ + / Ctrl+ − | Zoom text |
| Ctrl+/ or F1 | Shortcut list |

## Behaviour notes

- The window renders at native DPI (per-monitor aware), so text is crisp on
  high-resolution displays.
- If the model provider is unreachable when the window opens (for example
  Ollama is not running yet), the app starts in a "Provider offline" state
  instead of failing. It retries when you send a message; the palette also
  offers "Reconnect model provider".
- Chats are titled from the first prompt, ChatGPT-style. Rename from the
  title, the chat's right-click menu, or the palette. Deleting a chat removes
  its history only; project files are untouched.
- Every string shown in the window passes through secret redaction and a
  display bound before it reaches Tk.
- The worker thread (`JarvisSession`) owns SQLite and the Agent. The Tk thread
  only ever receives redacted events, so a slow model never freezes the UI.
