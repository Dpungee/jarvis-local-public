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
4. Choose a model-provider path before running setup:

   - **Codex CLI:** uses an eligible ChatGPT subscription through the official Codex
     sign-in. See the [official Codex CLI guide](https://learn.chatgpt.com/docs/codex/cli).
   - **Claude CLI:** uses an eligible Claude subscription through the official Claude
     sign-in.
   - **Both:** uses Claude for fast/reasoning work and Codex for coding/deep work.
   - **Ollama:** local-only operation is supported, but v0.6.3 does not yet expose an
     Ollama preset in the first-run chooser. Follow the manual local path below.

Account eligibility and usage limits come from the selected provider. Jarvis verifies
the official CLI login; it does not read, copy, print, or store the provider's login
file.

## Recommended subscription-provider setup

1. Double-click `setup.bat` in the extracted project folder.
2. Confirm the Python version and path shown at the beginning. Setup installs Jarvis and
   its document-generation libraries into that Python environment. It does not create a
   virtual environment in v0.6.3.
3. Choose Codex CLI, Claude CLI, or both. If the selected CLI is missing, setup can offer
   to install its exact Windows Package Manager package. If it is not signed in, setup
   can start the provider's official sign-in flow.
4. Review the optional features. **Not now** is the safe default. Choosing **Set up** in
   this review only saves local settings; the installer performs no scan, pairing,
   download, or containment action. When a feature requires another feature, the prompt
   names the prerequisite before saving the choice.
5. Setup sends a fixed, tool-free first-turn check through every unique configured model
   route, then runs `jarvis doctor`. The check contains no files, credentials, or personal
   prompt. Do not treat the installation as complete unless the window ends with **Ready**.
6. Double-click `start_jarvis_presence.bat` to open the recommended interface.

Rerunning `setup.bat` is safe after a stopped installation. Already reviewed provider
and optional-feature choices are preserved.

## Manual local-only Ollama path

Use this path only if you want local inference and are comfortable editing a text file.
Local models need substantial disk space, memory, and download time; performance depends
on the computer.

1. Install [Ollama for Windows](https://ollama.com/download).
2. Copy `.env.example` to a new file named `.env` in the project folder.
3. Keep `JARVIS_OLLAMA_ENABLED=true` and both subscription CLI flags set to `false`.
4. Choose models that fit the computer. For a bounded starting point, use the same
   smaller model for every profile instead of the larger defaults:

   ```text
   JARVIS_MODEL=auto
   JARVIS_FAST_MODEL=qwen3.5:9b
   JARVIS_REASONING_MODEL=qwen3.5:9b
   JARVIS_CODING_MODEL=qwen3.5:9b
   JARVIS_DEEP_MODEL=qwen3.5:9b
   JARVIS_BACKGROUND_MODEL=qwen3.5:9b
   JARVIS_LEARNING_MODEL=qwen3.5:9b
   JARVIS_OLLAMA_ENABLED=true
   JARVIS_CODEX_CLI_ENABLED=false
   JARVIS_CLAUDE_CLI_ENABLED=false
   ```

5. Run `setup.bat`. Because the model profiles differ from the untouched template, setup
   recognizes this as an intentional local configuration and preserves it. Setup will ask
   Ollama to download any selected model that is not installed, then verify a real first
   response from each unique model.

An unchanged copy of `.env.example` does not count as completed provider setup. This keeps
an accidental copy—or unrelated API keys inherited from Windows—from skipping the review.
Customize the local profiles as shown above when Ollama is the deliberate choice.

## What setup changes

Setup may:

- install the editable `jarvis-local` Python package and the declared document libraries
  into the Python environment shown on screen;
- install a selected provider CLI through Windows Package Manager after you answer yes;
- start the selected provider's official sign-in flow after you answer yes;
- save non-secret provider routing and optional-feature switches in `.env`;
- create local onboarding state under `data/`;
- download configured Ollama models when local inference is selected;
- send a fixed, tool-free first-turn canary to each unique configured model; and
- run Jarvis's local doctor check.

Setup does not place provider passwords, session files, or API keys in the repository.
Optional-feature review does not scan a network, enumerate Bluetooth devices, pair a
network, control the desktop, contact an external account, or start background services.

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
