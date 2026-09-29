# Windows first-run guide

This guide describes the published JARVIS Local v0.6.3 public-preview installer.
Jarvis is alpha software for one supervised Windows operator. Setup does not grant
administrator, desktop, account, network-scanning, or publishing authority.

## Before you start

1. Use Windows 10 or Windows 11.
2. Install [Python 3.11, 3.12, or 3.13](https://www.python.org/downloads/windows/).
   Select **Add python.exe to PATH** in the Python installer.
3. Download the exact `v0.6.3` source archive from the
   [release page](https://github.com/Dpungee/jarvis-local-public/releases/tag/v0.6.3),
   or clone that exact tag. A clone of `main` may contain newer unreleased work.
   The wheel and source distribution are intended for Python package workflows; they
   are not the double-click installer.
4. Decide which provider account you want Jarvis to use. Setup presents all supported
   choices and automatically assigns task profiles:

   - **Codex CLI:** an eligible ChatGPT subscription through the official Codex sign-in.
     See the [official Codex CLI guide](https://learn.chatgpt.com/docs/codex/cli).
   - **Claude CLI:** an eligible Claude subscription through the official Claude sign-in.
   - **Both subscriptions:** Claude for fast/reasoning and Codex for coding/deep work.
   - **OpenAI API, Anthropic API, or Grok through xAI:** separately billed API access.
     Add the matching key to the Windows user environment before setup; never put it in
     this project.
   - **Ollama:** local inference on this computer; setup downloads selected preset models.

Account eligibility and usage limits come from the selected provider. Jarvis verifies
the official CLI login; it does not read, copy, print, or store the provider's login
file.

## Guided setup

1. Double-click `setup.bat` in the extracted project folder.
2. Confirm the Python version and path shown at the beginning. Setup installs Jarvis and
   its document-generation and Google Drive libraries into that Python environment. It
   does not create a virtual environment in v0.6.3.
3. Choose **Recommended**, **Customize everything**, or **Minimal**. Recommended enables
   helpful low-risk behavior while keeping private, external, and host-control access
   off. Customize reviews every capability family. Minimal installs core chat and
   workspace functionality.
4. Choose Codex CLI, Claude CLI, both, Ollama, OpenAI API, Anthropic API, or Grok through
   xAI. If a selected subscription CLI is missing, setup can install its exact Windows
   Package Manager package and start the official sign-in flow. Jarvis chooses models
   automatically after you choose the provider.
5. Review optional capability modes: Screen Companion, project/desktop execution,
   proactive work, signal-driven initiative, self-review drafts, memory quality,
   external connectors, Drive scope, image generation, and all granular network,
   Bluetooth, defensive-monitoring, and security-popup controls. **Not now** is the safe
   default for network features. Choosing **Set up** only saves local settings; the
   installer performs no scan, pairing, download, or containment action. Setup
   describes each safety boundary and names any prerequisite before saving it.
6. Setup sends a fixed, tool-free first-turn check through every unique configured model
   route, then runs `jarvis doctor`. The check contains no files, credentials, or personal
   prompt. Do not treat the installation as complete unless the window ends with **Ready**.
7. Setup offers the optional **Agent Hub**, a browser workspace for your agents. You can
   add it later with `install_agent_hub.bat`.
8. Choose whether to open Presence immediately, install Presence at Windows sign-in,
   install Presence plus the background worker at sign-in, or finish without starting.
   An unattended setup starts nothing; open Presence later with
   `start_jarvis_presence.bat`.

Rerunning `setup.bat` is the supported update/customization path, and it is safe after a
stopped installation. Existing data, unrecognized settings, provider login state, and
reviewed choices are preserved. The wizard lets you keep or change the provider and
review features again.

## Local-only Ollama path

Install [Ollama for Windows](https://ollama.com/download), run `setup.bat`, and choose
**Ollama**. Setup stores the bounded local profile preset, downloads models that are
missing, and verifies a real first response from every unique route. Local models can
require substantial disk space, memory, and download time; performance depends on the
computer. Advanced operators can edit profile names in `.env` later, then rerun setup to
download and verify the new selection.

An unchanged copy of `.env.example` does not count as completed provider setup. This
keeps an accidental copy—or unrelated API keys inherited from Windows—from skipping
the review.

## Direct API keys

For OpenAI API, Anthropic API, or Grok through xAI, open **Edit environment variables
for your account** from the Windows Start menu and create exactly one matching user
variable: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `XAI_API_KEY`. Paste the key in
the Windows editor, open a new terminal, and rerun `setup.bat`. The installer enables
only the provider you choose, refuses an API choice whose key is missing, and never
writes the key to `.env`, logs, or setup state.

xAI documents `XAI_API_KEY`, its OpenAI-compatible API, and current Grok aliases in the
[official quickstart](https://docs.x.ai/developers/quickstart) and
[model catalog](https://docs.x.ai/developers/models).

## What setup changes

Setup may:

- install the editable `jarvis-local` Python package and the declared document and
  Google Drive libraries into the Python environment shown on screen;
- install a selected provider CLI through Windows Package Manager after you answer yes;
- start the selected provider's official sign-in flow after you answer yes;
- save non-secret provider routing and optional-feature switches in `.env`;
- create local onboarding state under `data/`;
- download configured Ollama models when local inference is selected;
- send a fixed, tool-free first-turn canary to each unique configured model;
- run Jarvis's local doctor check;
- set up the optional Agent Hub when you answer yes;
- install the selected Presence/worker scheduled tasks when requested; and
- open Presence when requested.

Setup does not place provider passwords, session files, or API keys in the repository.
Optional-feature review does not scan a network, enumerate Bluetooth devices, pair a
network, control the desktop, contact an external account, or start background services.
Account connectors, Home Assistant, gateways, remote access, and other credentialed
integrations require a separate pairing step after first boot.

## Common failures

### Python was not found

Install a supported Python from python.org, make sure **Add python.exe to PATH** is
selected, close the setup window, and rerun `setup.bat`.

### The wrong Python was selected

The first setup line prints the exact interpreter path. On computers with several Python
installations, ensure the intended installation appears first on `PATH` before rerunning
setup. The double-click launchers also resolve `python` from `PATH` each time.

### A provider is missing or not signed in

Allow the offered official installation/sign-in step, or install and sign in to that CLI
yourself, then rerun setup. To deliberately change an existing subscription-provider
choice later, open a terminal in the project folder and run one of:

```powershell
python -m jarvis.provider_setup --login codex
python -m jarvis.provider_setup --login claude
python -m jarvis.provider_setup --login both
```

To switch to an API or local provider, rerun `setup.bat` or use one of:

```powershell
python -m jarvis.provider_setup --configure openai-api
python -m jarvis.provider_setup --configure anthropic-api
python -m jarvis.provider_setup --configure grok
python -m jarvis.provider_setup --configure ollama
```

An API choice is refused until its key is present in the Windows user environment.

If sign-in succeeds but the first-turn check fails, confirm the selected model is
available and rerun `setup.bat`, or retry only the bounded check with:

```powershell
python -m jarvis.provider_setup --canary
```

### Ollama is selected but unavailable

Install and start Ollama, or switch to a verified subscription provider using the command
above. A failed model download is safe to retry after checking the Internet connection
and available disk space.

### Presence does not open

Rerun `setup.bat` and confirm it reaches **Ready**. Then run
`start_jarvis_presence.bat` again. If it still fails, open a terminal in the project
folder and run:

```powershell
python -m jarvis doctor
python -m jarvis presence
```

The foreground command keeps the error visible. Include that error, the Python version,
and the step that failed when opening a public issue. Never include `.env`, provider
tokens, private file contents, or the `data/` directory in an issue.

## Removing the preview

Run `uninstall_presence.bat` and `uninstall_worker.bat` first if you installed either
always-on scheduled task. Then uninstall the Python package with the same interpreter
shown during setup:

```powershell
python -m pip uninstall jarvis-local
```

The editable source folder, document-library dependencies, `data/`, and `workspace/`
remain so an uninstall cannot silently destroy projects or memory. Review and remove
those yourself only after making any backup you need.
