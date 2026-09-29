from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .config import Config, MAX_DOTENV_BYTES, ROOT
from .feature_onboarding import FEATURE_SPECS, FeatureOnboardingStore, run_interactive
from .provider_setup import (
    ProviderSetupError,
    ProviderSetupRequired,
    has_provider_configuration,
    select_provider_interactive,
)


class InstallerError(RuntimeError):
    """The guided installer could not apply a reviewed configuration safely."""


@dataclass(frozen=True)
class InstallerMode:
    mode_id: str
    label: str
    description: str
    values: tuple[tuple[str, str], ...]
    prerequisite: str = ""


@dataclass(frozen=True)
class InstallerFeature:
    feature_id: str
    title: str
    description: str
    safety_boundary: str
    modes: tuple[InstallerMode, ...]
    recommended_mode: str
    minimal_mode: str


def _mode(
    mode_id: str,
    label: str,
    description: str,
    values: Mapping[str, str],
    prerequisite: str = "",
) -> InstallerMode:
    return InstallerMode(mode_id, label, description, tuple(values.items()), prerequisite)


INSTALLER_FEATURES: tuple[InstallerFeature, ...] = (
    InstallerFeature(
        "screen-companion",
        "Screen Companion",
        "Optionally observe the foreground app and offer contextual help.",
        "Always visible when active; sensitive windows are excluded and raw pixels are not stored.",
        (
            _mode("disabled", "Off", "No screen observation.", {"JARVIS_SCREEN_COMPANION": "disabled"}),
            _mode("observe", "Observe", "Keep only redacted app/title metadata in memory.", {"JARVIS_SCREEN_COMPANION": "observe", "JARVIS_SCREEN_COMPANION_INDICATOR": "1"}),
            _mode("suggest", "Suggest", "Offer help for the active non-sensitive window.", {"JARVIS_SCREEN_COMPANION": "suggest", "JARVIS_SCREEN_COMPANION_INDICATOR": "1"}),
            _mode("collaborate", "Collaborate", "Run operator-authored per-app routines with normal approvals.", {"JARVIS_SCREEN_COMPANION": "collaborate", "JARVIS_SCREEN_COMPANION_INDICATOR": "1"}),
        ),
        "disabled",
        "disabled",
    ),
    InstallerFeature(
        "computer-control",
        "Apps, files, and project execution",
        "Choose no host access, isolated Docker builds, or bounded trusted-desktop control.",
        "Desktop mode is limited to the selected user directory and keeps consequential approvals.",
        (
            _mode("disabled", "Workspace only", "Use Jarvis's workspace without host command or desktop access.", {"JARVIS_EXECUTION_MODE": "disabled", "JARVIS_EXECUTION_BACKEND": "host", "JARVIS_COMPUTER_ACCESS": "disabled"}),
            _mode("docker", "Docker sandbox", "Run project commands inside the bundled isolated container.", {"JARVIS_EXECUTION_MODE": "trusted-host", "JARVIS_EXECUTION_BACKEND": "docker", "JARVIS_COMPUTER_ACCESS": "disabled"}, "docker"),
            _mode("desktop", "Trusted desktop", "Use bounded files/apps under your Windows user folder.", {"JARVIS_EXECUTION_MODE": "trusted-host", "JARVIS_EXECUTION_BACKEND": "host", "JARVIS_COMPUTER_ACCESS": "trusted-desktop", "JARVIS_COMPUTER_ROOT": "{home}"}),
        ),
        "disabled",
        "disabled",
    ),
    InstallerFeature(
        "proactive-work",
        "Proactive idle work",
        "Let the worker pick up operator-approved research, idea, and prototype backlogs while idle.",
        "Only approved subjects are eligible and daily/time limits remain enforced.",
        (
            _mode("disabled", "Off", "Do nothing when idle.", {"JARVIS_PROACTIVE_ENABLED": "false"}),
            _mode("enabled", "On", "Process only the approved proactive backlog.", {"JARVIS_PROACTIVE_ENABLED": "true"}),
        ),
        "disabled",
        "disabled",
    ),
    InstallerFeature(
        "bounded-initiative",
        "Signal-driven initiative",
        "Observe eligible signals or act only after calibration and recovery gates pass.",
        "Enabling act never bypasses calibration, approved domains, policy, or approvals.",
        (
            _mode("disabled", "Off", "Ignore initiative signals.", {"JARVIS_INITIATIVE": "disabled"}),
            _mode("observe", "Observe", "Record what Jarvis would do without acting.", {"JARVIS_INITIATIVE": "observe"}),
            _mode("act", "Gated action", "Act only when every permanent eligibility gate passes.", {"JARVIS_INITIATIVE": "act"}),
        ),
        "observe",
        "disabled",
    ),
    InstallerFeature(
        "self-review",
        "Self-inspection and repair drafts",
        "Allow read-only source inspection and optionally generate isolated, reviewable repair proposals.",
        "Jarvis never applies its own repair drafts; protected gates, policy, redaction, and tests are immutable.",
        (
            _mode("disabled", "Off", "Do not inspect Jarvis source.", {"JARVIS_SELF_INSPECT": "disabled", "JARVIS_SELF_REPAIR": "disabled"}),
            _mode("inspect", "Read-only inspection", "Diagnose against source without drafting changes.", {"JARVIS_SELF_INSPECT": "read-only", "JARVIS_SELF_REPAIR": "disabled"}),
            _mode("propose", "Reviewable proposals", "Draft isolated repairs for a human to review and apply.", {"JARVIS_SELF_INSPECT": "read-only", "JARVIS_SELF_REPAIR": "propose"}),
        ),
        "inspect",
        "disabled",
    ),
    InstallerFeature(
        "memory-quality",
        "Long-term memory quality",
        "Select normal provenance checks, strict claim enforcement, or OpenAI semantic retrieval.",
        "Secrets are redacted and semantic embeddings require a separately billed OpenAI API key.",
        (
            _mode("standard", "Standard", "Provenance-aware memory with claim checking in shadow mode.", {"JARVIS_MEMORY_AUTO_IMPROVE": "true", "JARVIS_MEMORY_CLAIM_CLOCK": "shadow", "JARVIS_MEMORY_EMBEDDINGS": "disabled"}),
            _mode("strict", "Strict verification", "Enforce the claim clock before using stale knowledge.", {"JARVIS_MEMORY_AUTO_IMPROVE": "true", "JARVIS_MEMORY_CLAIM_CLOCK": "enforce", "JARVIS_MEMORY_EMBEDDINGS": "disabled"}),
            _mode("semantic", "Semantic retrieval", "Add OpenAI embeddings to strict verified memory.", {"JARVIS_MEMORY_AUTO_IMPROVE": "true", "JARVIS_MEMORY_CLAIM_CLOCK": "enforce", "JARVIS_MEMORY_EMBEDDINGS": "openai"}, "openai-key"),
        ),
        "standard",
        "standard",
    ),
    InstallerFeature(
        "external-connectors",
        "GitHub, Drive, deployment, and connector access",
        "Expose installed external connectors for operator-requested work.",
        "Account mutation, uploads, publishing, and deployment still require exact approvals.",
        (
            _mode("disabled", "Off", "Keep external-account tools unavailable.", {"JARVIS_EXTERNAL_ACCESS": "disabled"}),
            _mode("enabled", "Available", "Expose configured connectors with their normal approval gates.", {"JARVIS_EXTERNAL_ACCESS": "trusted-external"}),
        ),
        "disabled",
        "disabled",
    ),
    InstallerFeature(
        "google-drive-scope",
        "Google Drive scope",
        "Choose the narrow app-files scope or allow the separately authenticated account's full Drive.",
        "OAuth is completed after installation and mutations remain approval-gated.",
        (
            _mode("app-files", "App files only", "Use only files created through the Jarvis Drive app.", {"JARVIS_GOOGLE_DRIVE_ACCESS": "app_files"}),
            _mode("full", "Full Drive", "Use the authenticated Drive after explicit OAuth setup.", {"JARVIS_GOOGLE_DRIVE_ACCESS": "full"}),
        ),
        "app-files",
        "app-files",
    ),
    InstallerFeature(
        "image-generation",
        "OpenAI image generation and editing",
        "Expose the separately billed OpenAI image provider when an API key is configured.",
        "Image calls never activate from an ambient key unless this switch is enabled.",
        (
            _mode("disabled", "Off", "Keep image generation disabled.", {"JARVIS_OPENAI_IMAGES_ENABLED": "false"}),
            _mode("enabled", "On", "Enable image generation and editing.", {"JARVIS_OPENAI_IMAGES_ENABLED": "true"}, "openai-key"),
        ),
        "disabled",
        "disabled",
    ),
)


ALWAYS_INCLUDED_FEATURES = (
    "Presence chat UI, natural conversation, streaming, and automatic model routing",
    "specialist agents, parallel task tracking, web research, and verified citations",
    "workspace coding, skills, documents, spreadsheets, PDFs, image/file input, and artifacts",
    "provenance-aware memory, approvals, redaction, recovery controls, and audit receipts",
)

PAIR_AFTER_INSTALL_FEATURES = (
    "Google Drive OAuth, GitHub/Vercel authentication, and connector credentials",
    "Home Assistant entity pairing, Telegram/Signal gateway IDs, and Tailscale remote access",
    "Obsidian vault path, voice preferences, and operator-approved proactive subjects",
    "Public Presence (foundation only; intentionally unavailable until a later reviewed release)",
)

_MANAGED_KEYS = frozenset(
    key for feature in INSTALLER_FEATURES for mode in feature.modes for key, _ in mode.values
)
_KEY_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _ordinary_env(path: Path) -> tuple[str, int | None]:
    if not os.path.lexists(path):
        return "", None
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise InstallerError("Jarvis could not inspect its local configuration.") from exc
    attributes = getattr(details, "st_file_attributes", 0)
    if stat.S_ISLNK(details.st_mode) or attributes & _WINDOWS_REPARSE_POINT or not stat.S_ISREG(details.st_mode):
        raise InstallerError("Jarvis .env must be an ordinary non-link file.")
    if details.st_size > MAX_DOTENV_BYTES:
        raise InstallerError(f"Jarvis .env exceeds {MAX_DOTENV_BYTES} bytes.")
    try:
        return path.read_text(encoding="utf-8"), stat.S_IMODE(details.st_mode)
    except (OSError, UnicodeError) as exc:
        raise InstallerError("Jarvis could not read its local configuration.") from exc


def _render_env(existing: str, updates: Mapping[str, str]) -> str:
    if not updates or any(key not in _MANAGED_KEYS for key in updates):
        raise InstallerError("Installer attempted to write an unmanaged setting.")
    for value in updates.values():
        if not isinstance(value, str) or len(value) > 4096 or "\n" in value or "\r" in value or "\x00" in value:
            raise InstallerError("Installer setting is invalid.")
    newline = "\r\n" if "\r\n" in existing else "\n"
    had_final_newline = existing.endswith(("\n", "\r"))
    rendered: list[str] = []
    written: set[str] = set()
    for line in existing.splitlines():
        match = _KEY_PATTERN.match(line)
        key = match.group(1) if match else None
        if key not in updates:
            rendered.append(line)
        elif key not in written:
            rendered.append(f"{key}={updates[key]}")
            written.add(key)
    missing = [key for key in sorted(updates) if key not in written]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Capabilities selected through the Jarvis guided installer.")
        rendered.extend(f"{key}={updates[key]}" for key in missing)
    result = newline.join(rendered)
    if result and (had_final_newline or missing):
        result += newline
    if len(result.encode("utf-8")) > MAX_DOTENV_BYTES:
        raise InstallerError(f"Updated Jarvis .env would exceed {MAX_DOTENV_BYTES} bytes.")
    return result


def persist_feature_updates(updates: Mapping[str, str], root: Path | str = ROOT) -> Path:
    """Atomically update only the reviewed non-secret installer settings."""
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    env_path = base / ".env"
    existing, existing_mode = _ordinary_env(env_path)
    before = hashlib.sha256(existing.encode("utf-8")).digest()
    rendered = _render_env(existing, updates)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".jarvis-install-", suffix=".tmp", dir=base)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as destination:
            destination.write(rendered)
            destination.flush()
            os.fsync(destination.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        current, _mode_value = _ordinary_env(env_path)
        if hashlib.sha256(current.encode("utf-8")).digest() != before:
            raise InstallerError("Jarvis configuration changed while setup was running; rerun setup.")
        os.replace(temporary, env_path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return env_path


def _expanded_values(mode: InstallerMode) -> dict[str, str]:
    home = str(Path.home().resolve())
    return {key: value.replace("{home}", home) for key, value in mode.values}


def _prerequisite_available(prerequisite: str, environ: Mapping[str, str]) -> bool:
    if not prerequisite:
        return True
    if prerequisite == "docker":
        return shutil.which("docker") is not None
    if prerequisite == "openai-key":
        return bool(str(environ.get("OPENAI_API_KEY", "")).strip())
    return False


def _feature(feature_id: str) -> InstallerFeature:
    normalized = str(feature_id).strip().casefold()
    for feature in INSTALLER_FEATURES:
        if feature.feature_id == normalized:
            return feature
    raise ValueError("Unknown installer feature")


def _selected_mode(feature: InstallerFeature, mode_id: str) -> InstallerMode:
    normalized = str(mode_id).strip().casefold()
    for mode in feature.modes:
        if mode.mode_id == normalized:
            return mode
    raise ValueError("Unknown installer feature mode")


def apply_feature_mode(feature_id: str, mode_id: str, root: Path | str = ROOT) -> Path:
    return persist_feature_updates(
        _expanded_values(_selected_mode(_feature(feature_id), mode_id)), root
    )


def _prompt_style(input_fn: Callable[[str], str], output: TextIO) -> str:
    print("\nChoose a setup style:", file=output)
    print("  1. Recommended - useful defaults, private/consequential access stays off", file=output)
    print("  2. Customize everything - review every optional capability", file=output)
    print("  3. Minimal - chat and core workspace features only", file=output)
    mapping = {"": "recommended", "1": "recommended", "recommended": "recommended", "2": "custom", "custom": "custom", "3": "minimal", "minimal": "minimal"}
    for _ in range(3):
        try:
            answer = input_fn("Setup style [1]: ").strip().casefold()
        except (EOFError, KeyboardInterrupt) as exc:
            raise InstallerError("Setup was cancelled before any capability changes were applied.") from exc
        if answer in mapping:
            return mapping[answer]
        print("Enter 1, 2, or 3.", file=output)
    raise InstallerError("Setup style was not recognized.")


def _prompt_feature(
    feature: InstallerFeature,
    *,
    input_fn: Callable[[str], str],
    output: TextIO,
    environ: Mapping[str, str],
) -> InstallerMode:
    print(f"\n{feature.title}", file=output)
    print(f"  {feature.description}", file=output)
    print(f"  Safety: {feature.safety_boundary}", file=output)
    available: list[InstallerMode] = []
    for mode in feature.modes:
        ready = _prerequisite_available(mode.prerequisite, environ)
        suffix = "" if ready else " (setup prerequisite missing)"
        print(f"  {len(available) + 1}. {mode.label} - {mode.description}{suffix}", file=output)
        available.append(mode)
    default_index = next(
        index for index, mode in enumerate(available, 1)
        if mode.mode_id == feature.recommended_mode
    )
    for _ in range(3):
        try:
            raw = input_fn(f"Choice [{default_index}]: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise InstallerError("Setup was cancelled before feature review completed.") from exc
        if not raw:
            selected = available[default_index - 1]
        elif raw.isdigit() and 1 <= int(raw) <= len(available):
            selected = available[int(raw) - 1]
        else:
            print(f"Enter a number from 1 through {len(available)}.", file=output)
            continue
        if not _prerequisite_available(selected.prerequisite, environ):
            requirement = "Docker" if selected.prerequisite == "docker" else "OPENAI_API_KEY"
            print(f"  {requirement} is not available. Choose another mode or configure it and rerun setup.", file=output)
            continue
        return selected
    raise InstallerError(f"No valid mode was selected for {feature.title}.")


def _review_network_features(
    store: FeatureOnboardingStore,
    style: str,
    *,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> None:
    if style == "custom":
        run_interactive(store, input_fn=input_fn, output=output)
        return
    status = store.list_status()
    digest = str(status["configuration_sha256"])
    decision = "disable" if style == "minimal" else "skip"
    for row in status["features"]:
        if row["decision"] != "pending":
            continue
        result = store.decide(
            str(row["capability_id"]),
            decision,
            expected_configuration_sha256=digest,
        )
        digest = str(result["configuration_sha256"])


def run_guided_install(
    *,
    root: Path | str = ROOT,
    environ: Mapping[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> dict[str, object]:
    """Run one upgrade-safe provider and capability review."""
    base = Path(root).resolve()
    values = os.environ if environ is None else environ
    print("\n" + "=" * 68, file=output)
    print(" JARVIS guided setup", file=output)
    print("=" * 68, file=output)
    print("Existing data and unrecognized settings are preserved. Secrets are never written here.", file=output)
    style = _prompt_style(input_fn, output)

    if has_provider_configuration(base):
        try:
            keep = input_fn("\nKeep the currently configured model provider? [Y/n] ").strip().casefold()
        except (EOFError, KeyboardInterrupt) as exc:
            raise InstallerError("Provider review was cancelled.") from exc
        if keep in {"n", "no"}:
            select_provider_interactive(base, environ=values, input_fn=input_fn, output=output)
        else:
            print("Keeping the current provider and automatic model routes.", file=output)
    else:
        select_provider_interactive(base, environ=values, input_fn=input_fn, output=output)

    selected: dict[str, str] = {}
    for feature in INSTALLER_FEATURES:
        if style == "custom":
            mode = _prompt_feature(feature, input_fn=input_fn, output=output, environ=values)
        else:
            mode_id = feature.recommended_mode if style == "recommended" else feature.minimal_mode
            mode = _selected_mode(feature, mode_id)
        persist_feature_updates(_expanded_values(mode), base)
        selected[feature.feature_id] = mode.mode_id

    config = Config.load() if base == Path(ROOT).resolve() else None
    data_dir = config.data_dir if config is not None else base / "data"
    store = FeatureOnboardingStore(base, data_dir)
    _review_network_features(store, style, input_fn=input_fn, output=output)

    print("\nIncluded in every installation:", file=output)
    for item in ALWAYS_INCLUDED_FEATURES:
        print(f"  - {item}", file=output)
    print("\nAvailable to pair after first boot:", file=output)
    for item in PAIR_AFTER_INSTALL_FEATURES:
        print(f"  - {item}", file=output)
    print("\nSetup choices saved. The installer will now validate every selected model route.", file=output)
    return {"style": style, "features": selected, "network_feature_count": len(FEATURE_SPECS)}


def _summary(output: TextIO) -> None:
    print("Core capabilities (always installed):", file=output)
    for item in ALWAYS_INCLUDED_FEATURES:
        print(f"  - {item}", file=output)
    print("Optional capabilities reviewed by setup:", file=output)
    for feature in INSTALLER_FEATURES:
        print(f"  - {feature.title}: {', '.join(mode.label for mode in feature.modes)}", file=output)
    print(f"  - Network/Bluetooth/security controls: {len(FEATURE_SPECS)} granular choices", file=output)
    print("Pair-after-install capabilities:", file=output)
    for item in PAIR_AFTER_INSTALL_FEATURES:
        print(f"  - {item}", file=output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Jarvis's guided first-run and upgrade setup")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.summary:
            _summary(sys.stdout)
            return 0
        if not args.interactive or not bool(getattr(sys.stdin, "isatty", lambda: False)()):
            raise InstallerError("Guided setup requires an interactive terminal. Run setup.bat.")
        run_guided_install()
        return 0
    except (InstallerError, ProviderSetupError, ProviderSetupRequired, OSError, ValueError) as exc:
        print(f"Jarvis guided setup stopped safely: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
