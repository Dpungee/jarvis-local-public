# Agent Hub: build → run → verify → open

When the operator asks a Hub agent to build a web app or game **and open it**, the agent
writes the files in its project folder, serves them locally, proves in a real browser that
the app renders and responds to input, and opens the running app in the Hub's preview
panel. Pasting the app's HTML into chat does not complete the request.

## What goes wrong without it

Before this change, "Build me a simple web Tetris game and open it for me" went through
three failures in the live Hub:

1. **Research detour.** The agent's role text ("Helps me research …") travelled inside the
   prompt, so the intent classifier routed the build into web research. The Hub now sends
   only the operator's words (see the latency change: `_compose_prompt`, `operator_brief`).
2. **No file-writing lane.** "Build … a Tetris game" was a coding request but not a *code
   change*, so `write_file` was never offered and the model could only paste HTML. Creating
   games, simulators, calculators, visualizers, widgets and web pages now counts as a
   software build and a code change (`_SOFTWARE_PRODUCT_BUILD_INTENT`,
   `_INTERACTIVE_SOFTWARE_BUILD`, creation verbs only). "Fix/update my game" (an installed
   application), "game plan", "game night" and similar are excluded.
3. **"Browser" read as "browse".** The typo corrector changed the real word "browser" into
   the research verb "browse". Real words one edit away from an intent word are no longer
   "corrected" (`natural_language._NOT_TYPOS`).

## The workflow

| Step | Tool | Gate |
| --- | --- | --- |
| Build | `write_file` | Existing coding contract: inspect, write, re-read |
| Serve | `start_process` → `python -m http.server <port> --bind 127.0.0.1 --directory <dir>` | Only a loopback `--bind`, a port ≥ 1024 and an in-workspace directory; CGI and other options refused |
| Answer | `http_health` with the `process_id` | Healthy only if the managed process (or its child) is the program listening on that port |
| Verify | `web_app_check` with input actions | Loads the page in a disposable headless Edge/Chrome, fails on script/console errors, requires something to render and **an observable response to input** |
| Open | `open_preview` (Hub-provided) | Refused unless this run started the process, it is running, it owns the port, and a browser check of the same origin and process passed; never the Hub's own port |

Acceptance (`Agent._web_launch_obligation`): where the host offers both `web_app_check`
and `open_preview` (the Agent Hub), a launched web app is complete only after a passing
browser check **and** `open_preview`. Any file write after the check clears it, like every
other verification marker. A passing check with input also counts as the executed test
evidence the coding contract requires. Hosts without a preview panel keep the earlier
launch rule.

## `web_app_check`

`jarvis/web_check.py`, exposed through `ToolBox.web_app_check`.

- **Targets:** only `http` URLs on 127.0.0.0/8, `::1` or `localhost`.
- **Network:** every request the page makes goes through DevTools request interception.
  Anything that is not a loopback HTTP(S) resource or an inline scheme (`data:`, `blob:`,
  `about:`) is refused and listed under `blocked_non_local_requests`. A check never reaches
  the internet.
- **Browser:** a fresh temporary profile with no extensions, sync or background networking.
  It starts suspended inside a kill-on-close Windows job, then resumes, so every browser
  process ends with the check. The profile is deleted afterwards.
- **Actions:** `key` (named keys, letters, digits; `repeat`), `click` (selector or x/y),
  `drag` (x/y → to_x/to_y with the button held), `type`, `wait`, and `inspect`.
  `inspect` evaluates with V8's side-effect guard (`throwOnSideEffect`), so it can read
  state such as `window.appState.score` but cannot change the page.
- **Verdict:** `verified` requires a 2xx/3xx load, no uncaught exceptions or console errors,
  rendered content, and at least one input that produced an observable change.
  - A page that animates by itself can't prove input response through pixels. Changed page
    text, or an `inspect` value that differs before and after the input, is required instead.
  - A missing favicon and requests the check itself blocked are not counted as app errors.
- **Evidence:** the report includes per-step observations and two screenshots, saved under
  the agent's data directory in `web-checks/<id>/`.
- **Classification:** it is an execution-class tool (`EXECUTION_TOOLS`, `MUTATING_TOOLS`),
  because it runs the page's own scripts. Read-only autonomy and web-tainted turns never
  offer it.

## Preview panel and isolation

- **Where the app runs:** on its own loopback port, which is a different origin from the
  Hub.
- **Framing:** the Hub frames it in a sandboxed `iframe` (`allow-scripts allow-same-origin
  allow-forms allow-pointer-lock allow-modals`, `referrerpolicy=no-referrer`) in a panel
  outside the repainted chat view. Polling therefore never reloads a running game. The Hub
  page cannot read the frame, and the frame cannot read the Hub's storage or token.
- **CSP:** the Hub's Content Security Policy allows framing only `http://127.0.0.1:*`,
  `http://localhost:*` and `http://[::1]:*`, and keeps `frame-ancestors 'none'`.
  Generated HTML never executes in the Hub's origin.
- **Controls:** the panel offers **Open in new tab**, **Reload**, **Stop** and **Start again**.
  Chat turns show a card per app with the same controls.
- **Stop and Start again:** the server process outlives the agent's run in the Hub's shared
  managed-process registry, so the operator can keep playing. Stop terminates it the way
  `stop_process` does. Start again re-runs the recorded command through `start_process`'s
  full policy check, then waits for a bound health check. It is refused if the agent lost
  the "Run programs" permission.
- **Hub restarts:** preview servers are children of the Hub process. Previews that were
  running are marked stopped at startup, with a Start again button.
- **API:** the `hub_previews` table, `POST /api/previews/<id>/stop|start`, and per-turn
  `previews` in the chat view.

## Limits

- **Remote access:** previews are on the Hub computer's loopback. With remote (paired)
  access, the panel says the app can only be opened on the Hub computer. No proxy is
  provided, because proxying through the Hub would put generated code on the Hub's origin.
- **Port ownership:** the check reads the Windows TCP table. On other platforms it reports
  "unknown", and the checks fall back to process liveness.
- **Asking again:** a second "build … and open it" in the same project folder may reuse the
  existing app. The agent then verifies and opens it, but JARVIS's coding contract marks the
  run incomplete because no code changed.
- **Idle servers:** a server the agent started but never opened (for example, a failed
  check) keeps running until the Hub stops or restarts. It is not listed in the panel.
- **Depth of checking:** `web_app_check` checks what the model asks it to exercise. The
  workflow requires at least one observed input response, but not full gameplay coverage.

## Verification

- **Focused suites** (`tests.test_agent_hub_preview` plus the Hub, tool-catalog, policy,
  process, intent and routing suites): 226 tests, OK. The full suite's only failures are the
  sealed strategy-transfer runtime-pin cascade from editing `agent.py`, which the reseal tool
  resolves after integration.
- **Live acceptance on the operator's Research agent definition** (Claude Opus 5.5, all
  permissions):
  - "Build me a simple web Tetris game and open it for me." completed in 45.6 s: written,
    served, browser-verified and opened.
  - An independent browser run confirmed move, rotate, soft drop, hard drop, scoring and
    restart; the Tetris table is in the operator handoff.
  - A second app ("drawing app … pick colors … open it"), run with port 8765 deliberately
    occupied, completed in 58.2 s. Painting, colour choice and clear were confirmed by drag
    and click input.
