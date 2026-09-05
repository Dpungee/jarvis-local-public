# The Council

`COUNCIL` is a section of the JARVIS desktop app (`Ctrl+M`, or the nav at the
top of the sidebar). It seats JARVIS and his five specialists around one table
and lets them work a problem out loud, in front of you, with you able to
interrupt at any point. A meeting produces an agenda, minutes and a report; the
report ends with JARVIS's decision about what Jarvis works on next.

```
+-------------------------------------------+--------------------------+
| COUNCIL     OpenAI API - JARVIS on Sol...  |  THE FLOOR               |
| [topic....................] Convene Pause  |  JARVIS -> the table     |
| DEPTH  Brief [Standard] Deep               |  Forge  -> JARVIS        |
|                                            |  Archivist -> Forge      |
|          (the room, drawn in 3-D)          |  Sentinel -> Archivist   |
|                                            |  Operator -> JARVIS      |
| AGENDA                     Item 2 of 3     |  ...                     |
| 1. ...  > 2. ...  3. ...                   |  [ interject.......  ^ ] |
+-------------------------------------------+--------------------------+
```

## Who is at the table

The roster is derived at import time from `jarvis/specialists.py`, so it cannot
drift from the specialists Jarvis actually has:

| Seat | Who | Mandate |
|---|---|---|
| Chair | **JARVIS** | holds the agenda, decides who speaks, rules on each item, decides what happens next |
| | **Forge** | software implementation, debugging, refactoring, verification |
| | **Archivist** | source-grounded research and learning briefs |
| | **Sentinel** | defensive cybersecurity, hardening, incident response |
| | **Relay** | network architecture and diagnostics |
| | **Steward** | bounded local workspace file operations |

The empty chair nearest the camera is yours.

## Models

The council has its own model policy, scoped to this room only. The agent's
ordinary routing (`fast` / `reasoning` / `coding` / `deep`) is untouched — a
meeting never changes which model answers a chat turn.

| Tier | Chair | Members | Selected when |
|---|---|---|---|
| OpenAI API | `openai:gpt-5.6-sol`, effort `high` | `openai:gpt-5.5`, effort `medium` | `JARVIS_OPENAI_API_ENABLED=true` and `OPENAI_API_KEY` is set |
| Codex CLI | `codex-cli:gpt-5.6-sol`, effort `high` | `codex-cli:gpt-5.5`, effort `medium` | `JARVIS_CODEX_CLI_ENABLED=true` |
| Local | `JARVIS_REASONING_MODEL`, thinking on | `JARVIS_FAST_MODEL`, thinking off | nothing above is available |

The tier is resolved by asking exactly the questions `build_model_client` asks,
so the badge on each name card states what is really serving that seat rather
than what was configured. The header line names the tier in full.

Four optional variables retune the room without a code change; each is
validated and falls back to the tier default if it is empty, unbounded, or
carries whitespace or control characters:

```
JARVIS_COUNCIL_CHAIR_MODEL      e.g. openai:gpt-5.6-terra
JARVIS_COUNCIL_MEMBER_MODEL     e.g. codex-cli:gpt-5.5
JARVIS_COUNCIL_CHAIR_EFFORT     none | low | medium | high | xhigh | max
JARVIS_COUNCIL_MEMBER_EFFORT    none | low | medium | high | xhigh | max
```

## What the room is, and is not

This is the important part.

`delegate_specialist` — Jarvis's real delegation path — runs **one** specialist
at a time, peer-blind, with a bounded tool allowlist, so it can act on your
behalf. Specialists there receive no roster, no channel to each other and no
ability to delegate. **That invariant is untouched by the council.**

The council is a different thing: a deliberation room.

- **No tools.** Every council turn is a plain text completion with `tools=[]`.
  No filesystem, no network, no processes, no devices, no memory writes.
- **Nothing said here executes.** Members are told so in their contract and are
  instructed never to claim they ran, read, fetched or changed anything.
- **JARVIS still orchestrates.** Members see the transcript because the chair
  relays it. The chair decides who speaks, in what order, and rules on each
  item.
- **The transcript is data, not instructions.** Each contract says so
  explicitly: a line in the transcript that tries to change a member's mandate,
  grant authority, or extract configuration is to be ignored.
- **Output is a recommendation.** The report is a document. Acting on it goes
  through Jarvis's ordinary governed path, with your approval, like anything
  else.

Everything a seat says is bounded to 1,200 characters and passed through
`redact_secrets` before it reaches a widget or a file.

## How a meeting runs

The order of speakers is decided by code, never by a model — `next_directive`
reads the meeting state and returns exactly one next action. Per agenda item:

1. **JARVIS opens the item**, says what he wants settled and names who starts.
2. **Each member speaks in turn.** The first answers the chair; every one after
   that answers *the member who just spoke*, which is what makes it a
   conversation rather than five reports filed to a manager.
3. **Crosstalk.** Two members trade a free exchange, agreeing and sharpening or
   pushing back.
4. **JARVIS rules**: the decision, one owner, the first concrete step.

The lead seat rotates between items. When the agenda is exhausted the chair
writes the closing report. If a seat's provider is unreachable the meeting
records a notice turn and carries on — only a chair that cannot be reached ends
the sitting.

**Depth** sets the shape:

| | Agenda items | Speakers per item | Crosstalk |
|---|---|---|---|
| Brief | 2 | 3 | 0 |
| Standard | 3 | all five | 1 |
| Deep | 4 | all five | 2 |

## Interrupting

Type into the box under the transcript and press Enter. Your message is
recorded on the floor immediately and **preempts the next scheduled speaker**:
the chair answers you before anyone else talks. If your point deserves its own
item the chair can add one (`AGENDA:` in his reply), and it lands next in the
running order.

Pause holds the floor without ending the meeting. Adjourn ends it now and files
the report from whatever was said.

## When the tier cannot answer

A cloud flag only says which tier you *want*. Before every sitting the runtime
asks the model client whether that tier can actually answer — for the Codex
CLI that means Jarvis's **own isolated profile** is signed in (your global
`codex login` does not count). If it cannot, the sitting runs on the local tier
and the chair opens with a notice saying so and how to fix it:

```
python -X utf8 -m jarvis.provider_setup --login codex
```

Run that from the Jarvis folder in a terminal and sign in once in the browser.

## While you are away

The council can sit on its own. Turn it on in the **WHILE YOU ARE AWAY** panel
under the agenda:

| Setting | Meaning | Default |
|---|---|---|
| Let the council sit | arm the night watch | off |
| Window | local-time `HH:MM-HH:MM`, may cross midnight | `23:30-07:00` |
| Sittings | cap per night (1–8) | 3 |
| Depth | Brief / Standard / Deep, as for a normal meeting | Brief |
| Focus | the standing brief the chair picks topics from | *cool, useful app ideas for the operator* |

Every couple of seconds while no meeting is sitting, the worker checks five
gates in order — armed, nothing sitting, inside the window, under the cap,
desktop idle for ten minutes — and the status line always says which gate is
holding it ("Waiting for 4 more idle minutes", "Tonight's cap of 3 sittings
is reached"). When all five open:

1. **The chair picks the topic.** JARVIS is given the focus, the titles of
   meetings already held (so nothing repeats), and one random *spark* drawn
   from a fixed list ("something for the first ten minutes of the morning",
   "something that turns one recurring chore into one command", …) to push
   it somewhere new. It answers with one line, which becomes the meeting.
2. The meeting runs exactly like a convened one and files its documents.
3. The sitting is folded into **`data/council/night-YYYY-MM-DD.md`** — one
   page for the morning with each topic, the chair's decision, and the
   proposals on the table. It is rewritten after every sitting, so a crash
   never loses a finished one. A window that crosses midnight is one night.

Coming back ends it: any activity in the desktop other than speaking to the
council adjourns an unattended sitting (its report is still filed) and the
room goes quiet. Interjecting keeps it going — you have joined the meeting.

The settings persist in `data/desktop_ui.json`; when night sessions are on,
the watch starts with the app even if you never open the Council view. The
digest summary and an **Open the morning digest** button appear in the panel.

Everything above is still tool-free deliberation. A night's output is ideas
and specifications, not code; pick one and **Take the decision to chat** to
start building it.

## What gets filed

At the end of a meeting, under `<data dir>/council/<timestamp>-<topic>/`:

| File | What it holds |
|---|---|
| `agenda.md` | topic, seats with their models, the items |
| `minutes.md` | every turn grouped by item, the ruling on each, proposals / risks / disagreements / open questions, operator interventions, attendance |
| `report.md` | the decision about what Jarvis works on next, decisions by item, proposals to pick up, risks to carry, provenance |
| `transcript.jsonl` | one JSON object per turn, for anything that wants to read the meeting back |

All four are written with LF endings.

**Open report folder** opens the directory. **Take the decision to chat**
switches to the chat view with the decision loaded into the composer, which is
how a meeting turns into work.

## Reading the room

The table is drawn on a Tk canvas, which has no gradients, no shaders and no
depth buffer. Three things do the work instead:

- **Ordering.** Seats are painted back to front, then the table top, then the
  name cards, then the operator's chair. The table crops each figure at the
  waist and the near chair is cropped by the canvas edge, which is what puts
  you in the room.
- **Distance.** Scale and a fade toward the room colour both track how far back
  a seat sits. JARVIS is at the head of the table and carries a presence
  multiplier so the chair still reads as the chair from the furthest seat.
- **Light.** Every solid is a few offset, stepped-tone shapes standing in for a
  single light from the upper left; heads get four bands plus a rim arc.

While a seat holds the floor its halo lights, its eyes take its accent colour,
its mouth moves, and a trace of light travels across the table surface from its
name card to the card of whoever it is addressing — so *who is talking to whom*
is legible without reading a word. Traces are anchored to the cards, never the
heads, so they cannot cut across a face.

## Testing

`tests/test_council.py` covers the roster, the model tiers and their env
overrides, the scheduler's choreography, reply parsing, the contracts, the
runtime against a scripted client (including an unreachable seat and an
operator interjection), the documents, and the view's pure colour and layout
helpers. No test opens a window or reaches a model.
