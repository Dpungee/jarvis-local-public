from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .memory import Memory, training_prompt_split
from .redaction import is_sensitive_key, redact_secrets
from .source_quality import authoritative_sources


DATASET_FORMAT_VERSION = 4
TRAINING_QUALITY_CONTRACT_VERSION = 1
READINESS_MIN_QUALITY = 0.8
READINESS_MIN_VERIFIED_EXAMPLES = 100
READINESS_MIN_TRAIN_EXAMPLES = 70
READINESS_MIN_VALIDATION_EXAMPLES = 10
READINESS_MIN_TEST_EXAMPLES = 10
READINESS_MIN_ENABLED_EVALUATIONS = 10
READINESS_MIN_CAPABILITY_EXAMPLES = 10
READINESS_CAPABILITY_KINDS = (
    ("coding", frozenset({"coding"})),
    ("local", frozenset({"local"})),
    ("research", frozenset({"research", "learning"})),
)
LOCAL_TRAINING_OUTCOME_TOOLS = frozenset({
    "computer_write_file",
    "copy_path",
    "detect_project",
    "http_health",
    "install_project_dependencies",
    "launch_artifact",
    "make_directory",
    "move_path",
    "process_logs",
    "process_status",
    "run_process",
    "start_process",
    "stop_process",
    "system_snapshot",
    "trash_path",
})
_PLACEHOLDER_CONTENT = re.compile(
    r"(?i)\b(?:20\d{2}|yyyy)[-/](?:xx|mm|\d{2})[-/](?:xx|dd)\b|"
    r"\b20xx\b|\b(?:todo|tbd)\s*:\s*(?:date|source|finding)\b"
)
_RESEARCH_FAILURE_PREFIXES = (
    "couldn't locate any reliable",
    "could not locate any reliable",
    "couldn't find any reliable",
    "could not find any reliable",
    "unable to locate any reliable",
    "unable to find any reliable",
    "no reliable, up-to-date information",
    "no reliable up-to-date information",
)
_QUALITY_WORD_STOPWORDS = frozenset({
    "about", "could", "explain", "from", "have", "please", "should", "tell",
    "that", "their", "there", "these", "they", "this", "what", "when", "where",
    "which", "with", "would", "your",
})
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.I)


def _sanitize_evidence(value: Any, depth: int = 0) -> Any:
    if depth >= 6:
        return "[nested evidence clipped]"
    if isinstance(value, str):
        cleaned = redact_secrets(value)
        return cleaned[:8192]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            safe_key = redact_secrets(str(key))[:100]
            cleaned[safe_key] = (
                "[REDACTED]"
                if is_sensitive_key(str(key))
                else _sanitize_evidence(item, depth + 1)
            )
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_evidence(item, depth + 1) for item in value[:128]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_evidence(str(value), depth + 1)


def _decoded_evidence(encoded: str) -> dict[str, Any]:
    try:
        value = json.loads(encoded)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Training evidence is malformed") from exc
    if not isinstance(value, dict):
        raise ValueError("Training evidence must be a JSON object")
    sanitized = _sanitize_evidence(value)
    if not isinstance(sanitized, dict):
        raise ValueError("Training evidence must be a JSON object")
    return sanitized


def _passes_source_quality(item: dict[str, Any]) -> bool:
    if str(item.get("task_kind", "")).casefold() not in {"research", "learning"}:
        return True
    try:
        evidence = _decoded_evidence(str(item.get("evidence_json", "")))
    except ValueError:
        return False
    cited = evidence.get("cited_verified_urls", [])
    if not isinstance(cited, list):
        return False
    return bool(authoritative_sources([url for url in cited if isinstance(url, str)]))


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _prose_stats(content: str) -> tuple[int, int]:
    prose = _URL_IN_TEXT.sub(" ", content)
    prose = re.sub(r"(?im)^\s*(?:#+\s*)?sources?\s*:\s*$", " ", prose)
    prose = re.sub(r"[`*_#>|\[\](){}-]", " ", prose)
    words = re.findall(r"[A-Za-z][A-Za-z0-9.+-]*", prose)
    meaningful = {
        word.casefold()
        for word in words
        if len(word) >= 4 and word.casefold() not in _QUALITY_WORD_STOPWORDS
    }
    return len(words), len(meaningful)


def _current_quality_failure(item: dict[str, Any]) -> str | None:
    """Return a stable quarantine reason when current completion proof is absent."""
    try:
        evidence = _decoded_evidence(str(item.get("evidence_json", "")))
    except ValueError:
        return "malformed_evidence"
    if evidence.get("quality_contract_version") != TRAINING_QUALITY_CONTRACT_VERSION:
        return "legacy_quality_contract"
    verification = evidence.get("verification")
    if not isinstance(verification, dict) or verification.get("accepted_complete") is not True:
        return "completion_not_proven"

    response = str(item.get("response", "")).strip()
    if not response or len(response) < 24:
        return "undersized_response"
    if "\ufffd" in response:
        return "corrupted_response_text"
    if _PLACEHOLDER_CONTENT.search(response):
        return "placeholder_content"

    task_kind = str(item.get("task_kind", "")).casefold()
    tools = evidence.get("successful_tools", [])
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        return "malformed_tool_evidence"
    tool_names = set(tools)

    if task_kind in {"research", "learning"}:
        normalized_prefix = response[:600].casefold().replace("’", "'")
        if any(phrase in normalized_prefix for phrase in _RESEARCH_FAILURE_PREFIXES):
            return "no_research_finding"
        cited = evidence.get("cited_verified_urls", [])
        if not isinstance(cited, list) or not all(isinstance(url, str) for url in cited):
            return "malformed_citation_evidence"
        cited_urls = set(cited)
        words, distinct = _prose_stats(response)
        if task_kind == "learning":
            if words < 40 or distinct < 15:
                return "undersized_learning_brief"
            if len(cited_urls) < 2 or len({_origin(url) for url in cited_urls}) < 2:
                return "insufficient_learning_sources"
            if verification.get("research_topic_coverage_passed") is not True:
                return "topic_coverage_not_proven"
            if verification.get("deep_research_review_passed") is not True:
                return "semantic_review_not_proven"
        elif words < 8 or distinct < 4:
            return "undersized_research_answer"
        return None

    if task_kind == "coding":
        required = (
            "inspected_before_write",
            "content_write_completed",
            "inspected_after_write",
            "verified_after_write",
            "adversarial_probe_passed",
        )
        if not all(verification.get(field) is True for field in required):
            return "coding_verification_not_proven"
        return None

    if task_kind == "local":
        if not (tool_names & LOCAL_TRAINING_OUTCOME_TOOLS):
            return "local_outcome_not_proven"
        return None
    return "unsupported_task_kind"


def _classify_examples(
    examples: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    source_quarantined: list[dict[str, Any]] = []
    quality_quarantined: list[tuple[dict[str, Any], str]] = []
    eligible: list[dict[str, Any]] = []
    for item in examples:
        if not _passes_source_quality(item):
            source_quarantined.append(item)
            continue
        failure = _current_quality_failure(item)
        if failure:
            quality_quarantined.append((item, failure))
            continue
        eligible.append(item)
    return eligible, source_quarantined, quality_quarantined


def _effective_split(item: dict[str, Any]) -> str:
    return training_prompt_split(
        str(item.get("prompt", "")),
        str(item.get("task_kind", "")),
    )


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def dataset_status(memory: Memory) -> dict[str, Any]:
    examples = memory.list_training_examples(verified_only=False)
    verified = [item for item in examples if item["verified"]]
    quality_candidates = [
        item
        for item in verified
        if float(item["quality_score"]) >= READINESS_MIN_QUALITY
    ]
    eligible, source_quarantined, quality_quarantined = _classify_examples(
        quality_candidates
    )
    quarantine_reasons = Counter(reason for _item, reason in quality_quarantined)
    eligible_splits = Counter(
        _effective_split(item)
        for item in eligible
    )
    eligible_task_kinds = Counter(item["task_kind"] for item in eligible)
    evaluation_cases = memory.list_evaluation_cases()
    enabled_evaluations = sum(
        bool(case.get("enabled", 1)) for case in evaluation_cases
    )
    capability_counts = {
        capability: sum(eligible_task_kinds[kind] for kind in kinds)
        for capability, kinds in READINESS_CAPABILITY_KINDS
    }
    blockers: list[str] = []
    if len(eligible) < READINESS_MIN_VERIFIED_EXAMPLES:
        blockers.append(
            f"Need at least {READINESS_MIN_VERIFIED_EXAMPLES} verified examples "
            f"with quality >= {READINESS_MIN_QUALITY:.2f}; found {len(eligible)}."
        )
    if eligible_splits["train"] < READINESS_MIN_TRAIN_EXAMPLES:
        blockers.append(
            f"Need at least {READINESS_MIN_TRAIN_EXAMPLES} training examples; "
            f"found {eligible_splits['train']}."
        )
    if eligible_splits["validation"] < READINESS_MIN_VALIDATION_EXAMPLES:
        blockers.append(
            f"Need at least {READINESS_MIN_VALIDATION_EXAMPLES} validation examples; "
            f"found {eligible_splits['validation']}."
        )
    if eligible_splits["test"] < READINESS_MIN_TEST_EXAMPLES:
        blockers.append(
            f"Need at least {READINESS_MIN_TEST_EXAMPLES} test examples; "
            f"found {eligible_splits['test']}."
        )
    if enabled_evaluations < READINESS_MIN_ENABLED_EVALUATIONS:
        blockers.append(
            f"Need at least {READINESS_MIN_ENABLED_EVALUATIONS} enabled evaluation cases; "
            f"found {enabled_evaluations}."
        )
    deficient_capabilities = {
        capability: count
        for capability, count in capability_counts.items()
        if count < READINESS_MIN_CAPABILITY_EXAMPLES
    }
    if deficient_capabilities:
        details = ", ".join(
            f"{capability}={count}"
            for capability, count in deficient_capabilities.items()
        )
        blockers.append(
            f"Need at least {READINESS_MIN_CAPABILITY_EXAMPLES} verified examples "
            f"in each core capability; found {details}."
        )
    return {
        "total": len(examples),
        "verified": len(verified),
        "splits": dict(Counter(
            _effective_split(item)
            for item in verified
        )),
        "task_kinds": dict(Counter(item["task_kind"] for item in verified)),
        "evaluation_cases": len(evaluation_cases),
        "enabled_evaluation_cases": enabled_evaluations,
        "training_eligible": len(eligible),
        "source_quarantined": len(source_quarantined),
        "quality_quarantined": len(quality_quarantined),
        "quarantine_reasons": dict(quarantine_reasons),
        "eligible_splits": dict(eligible_splits),
        "eligible_task_kinds": dict(eligible_task_kinds),
        "capability_counts": capability_counts,
        "ready_for_candidate_training": not blockers,
        "readiness_blockers": blockers,
    }


def export_verified_dataset(
    memory: Memory,
    output_dir: Path,
    *,
    min_quality: float = 0.8,
    constitution_sha256: str | None = None,
) -> dict[str, Any]:
    if constitution_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", constitution_sha256
    ):
        raise ValueError("Constitution SHA-256 must be 64 lowercase hex characters")
    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
        raise ValueError("Training export target must be an ordinary directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = memory.list_training_examples(
        verified_only=True,
        min_quality=min_quality,
    )
    examples, source_quarantined, quality_quarantined = _classify_examples(candidates)
    quarantine_reasons = Counter(reason for _item, reason in quality_quarantined)
    rendered: dict[str, list[str]] = {"train": [], "validation": [], "test": []}
    for item in examples:
        record = {
            "messages": [
                {"role": "user", "content": item["prompt"]},
                {"role": "assistant", "content": item["response"]},
            ],
            "metadata": {
                "id": item["id"],
                "model": item["model"],
                "profile": item["profile"],
                "task_kind": item["task_kind"],
                "quality_score": item["quality_score"],
                "content_hash": item["content_hash"],
                "evidence": _decoded_evidence(item["evidence_json"]),
            },
        }
        split = _effective_split(item)
        rendered[split].append(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    files: dict[str, dict[str, Any]] = {}
    for split in ("train", "validation", "test"):
        content = "\n".join(rendered[split])
        if content:
            content += "\n"
        path = output_dir / f"{split}.jsonl"
        _atomic_text(path, content)
        files[split] = {
            "file": path.name,
            "examples": len(rendered[split]),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    manifest = {
        "format_version": DATASET_FORMAT_VERSION,
        "selection": {
            "verified_only": True,
            "minimum_quality": float(min_quality),
            "authoritative_web_sources": True,
            "current_quality_contract": TRAINING_QUALITY_CONTRACT_VERSION,
            "prompt_grouped_splits": True,
        },
        "candidate_examples": len(candidates),
        "quarantined": {
            "source_quality": len(source_quarantined),
            "current_quality_contract": len(quality_quarantined),
            "reasons": dict(quarantine_reasons),
        },
        "total_examples": len(examples),
        "files": files,
    }
    if constitution_sha256 is not None:
        manifest["constitution_sha256"] = constitution_sha256
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_text(output_dir / "manifest.json", manifest_text)
    return manifest


def parse_expected_terms(encoded: str) -> list[str]:
    value = json.loads(encoded)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Evaluation case expected text is malformed")
    return value
