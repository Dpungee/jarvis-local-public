from __future__ import annotations

import re

from .research_support import _URL_IN_TEXT, research_reports_no_finding
from .source_quality import is_authoritative_source


TRAINING_QUALITY_CONTRACT_VERSION = 1
LEARNING_MEMORY_QUALITY_CONTRACT_TAG = (
    f"jarvis-quality-contract:{TRAINING_QUALITY_CONTRACT_VERSION}"
)
_DATE_PLACEHOLDER_RE = re.compile(
    r"(?i)\b(?:20\d{2}|yyyy)[-/](?:xx|mm|\d{2})[-/](?:xx|dd)\b"
)


def learning_memory_record_allowed(*, content: str, source: str) -> bool:
    """Return whether a learned record is safe for model-facing recall.

    This check intentionally lives below the agent layer. Ranking, ambiguity,
    and lexical-shadow decisions must not be influenced by a research record
    that the model would later be forbidden to see.
    """
    normalized_content = str(content)
    normalized_source = str(source)
    if LEARNING_MEMORY_QUALITY_CONTRACT_TAG not in normalized_source.splitlines():
        return False
    if research_reports_no_finding(normalized_content) or _DATE_PLACEHOLDER_RE.search(
        normalized_content
    ):
        return False
    source_urls = {
        raw.rstrip(".,;:!?)]}*_`")
        for raw in _URL_IN_TEXT.findall(normalized_source)
    }
    return any(is_authoritative_source(url) for url in source_urls)
