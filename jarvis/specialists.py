from __future__ import annotations

import re
from dataclasses import dataclass

from .security_expertise import classify_security_expertise


@dataclass(frozen=True)
class SpecialistDefinition:
    key: str
    name: str
    purpose: str
    model_profile: str
    families: tuple[str, ...]
    tool_allowlist: frozenset[str]


_WEB = frozenset({"web_search", "web_fetch", "research_question"})
_READ = frozenset({
    "list_files", "read_file", "read_files", "search_files", "detect_project",
})
_WRITE = frozenset({
    "write_file", "edit_file", "make_directory", "copy_path", "move_path",
    "trash_path", "build_document",
})
_PROCESS = frozenset({
    "install_project_dependencies", "run_process", "start_process",
    "process_status", "process_logs", "stop_process", "http_health",
    "launch_artifact",
})
_SKILLS = frozenset({
    "skill_list", "skill_read", "skill_create", "skill_update", "skill_github_sync",
})
_PRIVATE_NETWORK = frozenset({"network_inventory"})

# Scheduled tasks are materialized without a live model turn, so topical routing
# alone is not enough: the selected specialist must also be able to reach the
# tool surface implied by the durable job.  Each group below represents
# alternatives that can satisfy one required surface (for example, a private-file
# job may need any one of the bounded ``computer_*`` readers).  This deliberately
# errs toward leaving Jarvis as owner when a scheduled prompt asks for a surface
# specialists do not have; it never grants a specialist an additional tool.
_PRIVATE_FILE_TOOLS = frozenset({
    "computer_list_files", "computer_read_file", "computer_search_files",
    "computer_storage_report",
})
_GOOGLE_DRIVE_TOOLS = frozenset({
    "google_drive_status", "google_drive_authenticate", "google_drive_list_files",
    "google_drive_inventory", "google_drive_create_folder",
    "google_drive_upload_file", "google_drive_download_file",
    "google_drive_organize_files",
})
_GITHUB_TOOLS = frozenset({
    "github_cli_status", "github_auth_status", "github_repository_status",
    "github_list_repositories", "github_create_repository", "github_push",
})
_VERCEL_TOOLS = frozenset({
    "vercel_status", "vercel_list_projects", "vercel_project_status",
    "vercel_deploy", "vercel_deployment_status", "vercel_build_logs",
    "vercel_runtime_logs", "vercel_discover_databases", "vercel_list_databases",
})
_CONNECTOR_TOOLS = frozenset({
    "connector_list", "connector_describe", "connector_validate",
    "connector_install", "connector_call", "google_workspace_status",
    "prepare_email_draft", "prepare_calendar_event",
})
_DESKTOP_TOOLS = frozenset({
    "desktop_interact", "windows_launch_app", "windows_app_diagnose",
    "windows_app_repair", "windows_open_url", "photoshop_remove_background",
})

_PRIVATE_FILE_INTENT = re.compile(
    r"(?:\b(?:read|inspect|review|check|audit|scan|search|find|list|organize|"
    r"clean(?:\s+up)?|summari[sz]e|copy|move|rename|delete|trash|report|"
    r"back\s*up)\b[^.!?\r\n]{0,140}"
    r"\b(?:my\s+)?(?:computer|pc|downloads?|desktop|documents?|pictures?|"
    r"home\s+folder|user\s+profile|private\s+files?|[a-z]:\\|%userprofile%)|"
    r"\b(?:computer|pc|downloads?|desktop|documents?|pictures?|home\s+folder|"
    r"user\s+profile|private\s+files?|[a-z]:\\|%userprofile%)"
    r"[^.!?\r\n]{0,140}\b(?:read|inspect|review|check|audit|scan|search|"
    r"find|list|organize|clean(?:\s+up)?|summari[sz]e|copy|move|rename|"
    r"delete|trash|report|back\s*up)\b|"
    r"\b(?:disk\s+(?:usage|space)|storage\s+report|free\s+space)\b)",
    re.I,
)
_GOOGLE_DRIVE_INTENT = re.compile(
    r"(?:\bgoogle\s+drive\b[^.!?\r\n]{0,120}\b(?:status|authenticate|authorize|"
    r"list|inventory|organize|clean(?:\s+up)?|create|upload|download|move|"
    r"rename|trash|sync|back\s*up)\b|"
    r"\b(?:status|authenticate|authorize|list|inventory|organize|clean|create|"
    r"upload|download|move|rename|trash|sync|back\s*up)\b"
    r"[^.!?\r\n]{0,120}\bgoogle\s+drive\b)",
    re.I,
)
_GITHUB_INTENT = re.compile(
    r"(?:\bgithub\b[^.!?\r\n]{0,120}\b(?:status|list|create|push|publish|"
    r"upload|sync|clone|pull)\b|\b(?:status|list|create|push|publish|upload|"
    r"sync|clone|pull)\b[^.!?\r\n]{0,120}\bgithub\b)",
    re.I,
)
_VERCEL_INTENT = re.compile(
    r"(?:\bvercel\b[^.!?\r\n]{0,120}\b(?:status|list|deploy|redeploy|logs?|"
    r"database|publish)\b|\b(?:status|list|deploy|redeploy|logs?|publish)\b"
    r"[^.!?\r\n]{0,120}"
    r"\bvercel\b)",
    re.I,
)
_CONNECTOR_INTENT = re.compile(
    r"(?:\b(?:gmail|google\s+calendar|email|calendar|connector|slack|discord|"
    r"twitter|x\s+account|youtube|social\s+media)\b"
    r"[^.!?\r\n]{0,120}\b(?:status|connect|authenticate|authorize|draft|prepare|"
    r"send|post|create|update|delete|install|call)\b|"
    r"\b(?:status|connect|authenticate|authorize|draft|prepare|send|create|"
    r"post|update|delete|install|call)\b[^.!?\r\n]{0,120}"
    r"\b(?:gmail|google\s+calendar|email|calendar|connector|slack|discord|"
    r"twitter|x\s+account|youtube|social\s+media)\b)",
    re.I,
)
_DESKTOP_INTENT = re.compile(
    r"\b(?:click|type|scroll|drag|use\s+(?:the\s+)?(?:mouse|keyboard)|"
    r"open|launch|diagnose|repair)\b[^.!?\r\n]{0,120}"
    r"\b(?:desktop|screen|window|app|application|photoshop|browser)\b|"
    r"\b(?:photoshop|desktop\s+app|installed\s+app)\b",
    re.I,
)
_BLUETOOTH_INTENT = re.compile(
    r"(?:\bbluetooth\b[^.!?\r\n]{0,100}\b(?:scan|inventory|list|show|find|"
    r"discover|connected|nearby|pair|profile|trust)\b|"
    r"\b(?:scan|inventory|list|show|find|discover|pair|profile|trust)\b"
    r"[^.!?\r\n]{0,100}\bbluetooth\b)",
    re.I,
)
_HOME_DEVICE_INTENT = re.compile(
    r"(?:\b(?:home\s+assistant|smart\s+(?:home|device|light|lock|thermostat)|"
    r"paired\s+home\s+device)\b[^.!?\r\n]{0,100}\b(?:status|show|list|"
    r"turn|set|lock|unlock|open|close|control)\b|"
    r"\b(?:status|show|list|turn|set|lock|unlock|open|close|control)\b"
    r"[^.!?\r\n]{0,100}\b(?:home\s+assistant|smart\s+(?:home|device|light|"
    r"lock|thermostat)|paired\s+home\s+device)\b)",
    re.I,
)


SPECIALISTS: tuple[SpecialistDefinition, ...] = (
    SpecialistDefinition(
        "coding",
        "Forge",
        "software implementation, debugging, refactoring, and verification only",
        "coding",
        ("code_build", "code_fix", "code_refactor", "code_test"),
        _WEB | _READ | _WRITE | _PROCESS | _SKILLS,
    ),
    SpecialistDefinition(
        "research",
        "Archivist",
        "source-grounded public research and learning briefs only",
        "reasoning",
        ("deep_research", "learning_brief"),
        _WEB | _SKILLS,
    ),
    SpecialistDefinition(
        "cybersecurity",
        "Sentinel",
        "defensive cybersecurity analysis, hardening, and incident response only",
        "deep",
        ("security_analysis",),
        _WEB | _READ | _WRITE | _PROCESS | _SKILLS | _PRIVATE_NETWORK
        | frozenset({"system_snapshot"}),
    ),
    SpecialistDefinition(
        "network",
        "Relay",
        "network architecture, diagnostics, and engineering analysis only",
        "deep",
        ("security_analysis",),
        _WEB | _READ | _SKILLS | _PRIVATE_NETWORK
        | frozenset({"system_snapshot", "run_process"}),
    ),
    SpecialistDefinition(
        "operations",
        "Steward",
        "bounded local workspace file operations only",
        "reasoning",
        ("file_ops", "desktop_file_ops"),
        _READ | _WRITE | _SKILLS,
    ),
)

SPECIALIST_BY_KEY = {item.key: item for item in SPECIALISTS}

_CODING = re.compile(
    r"\b(?:build|implement|code|debug|fix|repair|refactor|test|compile|patch|"
    r"function|class|api|app|application|website|script|module|package|repo(?:sitory)?)\b|"
    r"\b[\w.-]+\.(?:py|js|jsx|ts|tsx|java|rs|go|cs|cpp|c|h|html|css|json|toml|yaml|yml)\b",
    re.I,
)
_RESEARCH = re.compile(
    r"\b(?:research|investigate|compare sources|literature|current|latest|"
    r"authoritative sources?|citations?|learning brief)\b",
    re.I,
)
_OPERATIONS = re.compile(
    r"\b(?:copy|move|rename|organize|create|edit|write|read|find|list|search)\b"
    r"[^.!?\r\n]{0,100}\b(?:files?|folders?|director(?:y|ies)|documents?|workspace)\b",
    re.I,
)

_INTERNAL_CONSULTATION = re.compile(
    r"\AJARVIS specialist consultation \(read-only; no mutations or process execution\)\.\r?\n"
    r"Assigned family: (?P<family>code_build|code_fix|code_refactor|code_test|"
    r"deep_research|learning_brief|security_analysis)\. Specialist purpose: "
    r"(?P<purpose>[^\r\n]{1,300})\.\r?\n"
    r"Work classification: [^\r\n]{1,500}\r?\n"
    r"(?P<body>.*)\Z",
    re.S,
)


def specialist_for_consultation_prompt(
    prompt: str,
) -> SpecialistDefinition | None:
    """Honor the family in one exact, runtime-generated consultation envelope.

    The advisory envelope is already forced read-only by the agent runtime.  Its
    declared family therefore remains the authoritative identity signal; stale
    conversation text inside ``<operator_task>`` must not reclassify a software
    consultation as a network or security assignment.
    """
    match = _INTERNAL_CONSULTATION.fullmatch(str(prompt).strip())
    if match is None:
        return None
    family = match.group("family")
    if family == "security_analysis":
        operator_match = re.search(
            r"<operator_task>\s*(.*?)\s*</operator_task>\s*\Z",
            match.group("body"),
            re.S,
        )
        operator_task = (
            operator_match.group(1) if operator_match is not None else match.group("body")
        )
        expertise = classify_security_expertise(operator_task)
        selected = (
            SPECIALIST_BY_KEY["network"]
            if expertise.network_engineering and not expertise.cybersecurity
            else SPECIALIST_BY_KEY["cybersecurity"]
        )
    else:
        selected = next(
            (
                specialist
                for specialist in SPECIALISTS
                if family in specialist.families
            ),
            None,
        )
    if selected is None or match.group("purpose") != selected.purpose:
        return None
    return selected


def specialist_for_prompt(prompt: str) -> SpecialistDefinition | None:
    """Select one single-purpose specialist using deterministic local evidence."""
    text = str(prompt).strip()
    declared = specialist_for_consultation_prompt(text)
    if declared is not None:
        return declared
    expertise = classify_security_expertise(text)
    if expertise.network_engineering and not expertise.cybersecurity:
        return SPECIALIST_BY_KEY["network"]
    if expertise.cybersecurity:
        return SPECIALIST_BY_KEY["cybersecurity"]
    if _CODING.search(text):
        return SPECIALIST_BY_KEY["coding"]
    if _RESEARCH.search(text):
        return SPECIALIST_BY_KEY["research"]
    if _OPERATIONS.search(text):
        return SPECIALIST_BY_KEY["operations"]
    return None


def scheduled_prompt_required_tool_surfaces(
    prompt: str,
) -> tuple[frozenset[str], ...]:
    """Return conservative tool alternatives implied by one scheduled prompt.

    This is a containment classifier, not an authority grant.  A false positive
    merely keeps the task with Jarvis; a false negative is still constrained by
    the ordinary runtime policy and approval gates.
    """
    text = str(prompt).strip()
    surfaces: list[frozenset[str]] = []
    for pattern, alternatives in (
        (_PRIVATE_FILE_INTENT, _PRIVATE_FILE_TOOLS),
        (_GOOGLE_DRIVE_INTENT, _GOOGLE_DRIVE_TOOLS),
        (_GITHUB_INTENT, _GITHUB_TOOLS),
        (_VERCEL_INTENT, _VERCEL_TOOLS),
        (_CONNECTOR_INTENT, _CONNECTOR_TOOLS),
        (_DESKTOP_INTENT, _DESKTOP_TOOLS),
        (_BLUETOOTH_INTENT, frozenset({"bluetooth_inventory"})),
        (_HOME_DEVICE_INTENT, frozenset({
            "home_device_status", "home_device_control",
        })),
    ):
        if pattern.search(text):
            surfaces.append(alternatives)
    return tuple(surfaces)


def specialist_for_scheduled_prompt(prompt: str) -> SpecialistDefinition | None:
    """Route a due job only when its specialist can satisfy every tool surface."""
    selected = specialist_for_prompt(prompt)
    if selected is None:
        return None
    required_surfaces = scheduled_prompt_required_tool_surfaces(prompt)
    if any(not (surface & selected.tool_allowlist) for surface in required_surfaces):
        return None
    return selected


def specialist_for_family(family: str, prompt: str = "") -> SpecialistDefinition | None:
    """Select a specialist for a measured family, preserving cyber/network specificity."""
    if family == "security_analysis":
        selected = specialist_for_prompt(prompt)
        if selected and selected.key in {"cybersecurity", "network"}:
            return selected
        return SPECIALIST_BY_KEY["cybersecurity"]
    for specialist in SPECIALISTS:
        if family in specialist.families:
            return specialist
    return None


def specialist_contract(specialist: SpecialistDefinition) -> str:
    """Return the peer-blind chain-of-command contract for one specialist run."""
    return (
        f"You are {specialist.name}, an isolated single-purpose specialist for "
        f"{specialist.purpose}. The human operator is the ultimate authority. JARVIS is "
        "your sole orchestrator and assigned this bounded task on the operator's behalf. "
        "Report the result to JARVIS; do not address the operator as if you are JARVIS. "
        "You receive no roster, identity, task, memory, output, or communication channel "
        "for any peer specialist. Do not infer, search for, contact, coordinate with, or "
        "claim knowledge of other agents. Never delegate work. Refuse work outside your "
        "single purpose and state that it must be returned to JARVIS for reassignment."
    )


def orchestrator_contract() -> str:
    return (
        "The human operator is your boss. JARVIS alone uses delegate_specialist and "
        "specialist_reports. Specialists are peer-blind and cannot delegate or gain authority."
    )
