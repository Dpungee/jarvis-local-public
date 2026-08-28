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
