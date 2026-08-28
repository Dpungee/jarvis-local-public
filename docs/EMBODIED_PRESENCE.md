# Embodied Presence

Embodied Presence is an optional interface around Jarvis Core. It does not
replace reasoning, tools, verified memory, approvals, or policy enforcement.

## Current implementation status

The development build includes the mode-isolation contract, a separate relationship
memory store, high-level avatar intentions, provider-neutral listening/thinking/speaking
state with barge-in, and a sanitized Screen Companion bridge. A production avatar,
real-time speech providers, Twitch/Discord adapters, and Unity/VRChat navigation remain
separate integrations and are not presented as operational yet.

## Mode boundaries

| Mode | Intended use | Context available |
| --- | --- | --- |
| Private | Local-only conversation and trusted work | Private, operational, relationship, and public context; no automatic screen capture |
| Operator | Research, coding, and approved computer work | Private and operational context plus sanitized screen summaries |
| Companion | Voice, avatar, and conversational continuity | Relationship memory, public context, and sanitized screen summaries |
| Studio | Public streams and community interaction | Explicitly public context only |

Studio Mode requires an explicit operator confirmation every time it is
entered. Credentials, private memory, raw screenshots, browsing history, and
relationship-only memories cannot be sent to it.

## Control shape

Models emit bounded intentions such as `listen`, `think`, `acknowledge`,
`point`, or `change_scene`. A deterministic avatar driver translates those
intentions into animation. Models cannot send joint rotations, pixel buffers,
raw device commands, or credentials through this interface.

The provider-neutral voice state supports listening, thinking, speaking, and
barge-in. Speech-to-text, text-to-speech, wake-word, avatar, Twitch/Discord,
and Unity/VRChat adapters remain separate opt-in providers.

The Screen Companion bridge accepts only a bounded text summary after the
existing exclusion and redaction checks. It never forwards the observation's
image or pixel data into the avatar event channel, and Studio Mode rejects
screen-summary events completely.

## Relationship memory

Relationship memory uses a separate database and a restricted vocabulary. It
can store address and tone preferences, important projects, shared
experiences, jokes, topic preferences, promises, and conversational
boundaries. It cannot store credentials or become authoritative operational
truth. Entries are visible, supersedable, and forgettable. Nothing becomes
public to Studio Mode without explicit confirmation.

## Activation sequence

1. Keep all modes disabled in production while running focused evaluations.
2. Connect a local avatar driver and duplex voice providers.
3. Expose relationship-memory review and deletion in Presence.
4. Bridge only sanitized Screen Companion summaries into Companion Mode.
5. Evaluate interruption latency, false wake-ups, privacy exclusions, and
   restart recovery.
6. Add public chat and scene adapters only after Studio isolation tests pass.
7. Add virtual-world movement through a bounded navigation controller, never
   raw repeated movement guesses from a model.
