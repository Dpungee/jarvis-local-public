# Screen Companion

Screen Companion is Jarvis's opt-in active-window collaboration layer. It is designed
to help with the work currently visible on the operator's screen without becoming an
unbounded recorder or a hidden desktop-control channel.

## Modes

| Mode | What Jarvis receives | What Jarvis may do |
| --- | --- | --- |
| Disabled | Nothing | Nothing |
| Observe | Redacted foreground application and window-title metadata | Update local Presence status only |
| Suggest | Metadata plus a transient capture of the active non-sensitive window | Queue bounded advice or research when requested/configured |
| Collaborate | The same bounded context as Suggest | Run operator-authored per-app/title routines through ordinary Jarvis tools and approvals |

Screen Companion starts disabled. Switching modes is visible in Presence and in the
small optional Windows companion indicator. The operator can pause or disable it at
any time.

## Privacy properties

- Raw screenshots remain in process memory and are never written to SQLite.
- Rule receipts store hashes, status, and job identifiers—not window titles or pixels.
- Credential managers are excluded by application name.
- Login, banking, password, wallet, recovery-phrase, and private-browsing windows are
  excluded by sensitive-title checks before pixels are captured.
- Additional applications can be excluded by the operator.
- Screen-derived text is treated as untrusted context, never as instructions or
  authorization.
- Screen activity does not bypass file, execution, publishing, account, or desktop
  approvals.
- The Embodied Presence bridge accepts a bounded redacted scene summary and never
  forwards the observation's image or pixels into the avatar event channel.
- Studio Mode rejects screen-summary context entirely.

## Routines

An operator may create a rule for an exact foreground application, an optional title
fragment, a bounded prompt, and either suggestion or collaboration behavior. A stable
window debounce and per-rule cooldown prevent rapid repeated runs. Durable Presence
jobs preserve queued work through a restart, while the exact screen contents remain
ephemeral.

Examples include:

- Offer outline feedback after a document remains active.
- Explain a visible error and research current documentation.
- Suggest the next step when an approved project tool stalls.
- Run a preapproved routine when Gmail or another app is opened, while still requiring
  normal approvals for any email, file, or account mutation.

## What it is not

Screen Companion is not continuous video storage, covert surveillance, unrestricted
mouse/keyboard automation, or evidence that Jarvis is conscious. It is a bounded sensor
and event source connected to the same policy, approval, verification, and memory
boundaries as every other Jarvis capability.
