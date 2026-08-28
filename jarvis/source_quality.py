from __future__ import annotations

from urllib.parse import unquote, urlsplit


_AUTHORITATIVE_DOMAINS = frozenset({
    "acm.org",
    "anthropic.com",
    "arxiv.org",
    "cyber.gov.au",
    "deepmind.google",
    "ietf.org",
    "ieee.org",
    "mitre.org",
    "nist.gov",
    "nodejs.org",
    "nvidia.com",
    "ollama.com",
    "openai.com",
    "owasp.org",
    "python.org",
    "pytorch.org",
    "qwen.ai",
    "rfc-editor.org",
    "sqlite.org",
    "git-scm.com",
})
_AUTHORITATIVE_EXACT_HOSTS = frozenset({
    "ai.google.dev",
    "aws.amazon.com",
    "cloud.google.com",
    "developers.google.com",
    "docs.aws.amazon.com",
    "docs.github.com",
    "huggingface.co",
    "learn.microsoft.com",
    "qwenlm.github.io",
})
_OFFICIAL_GITHUB_ORGS = frozenset({
    "huggingface",
    "microsoft",
    "nist",
    "nvidia",
    "ollama",
    "openai",
    "owasp",
    "python",
    "pytorch",
    "qwenlm",
})
_OFFICIAL_HUGGINGFACE_ORGS = frozenset({
    "microsoft",
    "nvidia",
    "openai",
    "qwen",
})


def is_authoritative_source(url: str) -> bool:
    """Return whether a fetched URL is on a conservative primary-source allowlist."""
    try:
        parsed = urlsplit(str(url).strip())
        host = (parsed.hostname or "").rstrip(".").casefold()
        path_parts = [
            unquote(part).casefold()
            for part in parsed.path.split("/")
            if part
        ]
    except (TypeError, ValueError, UnicodeError):
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    if host.endswith(".gov") or host.endswith(".edu"):
        return True
    if any(host == domain or host.endswith(f".{domain}") for domain in _AUTHORITATIVE_DOMAINS):
        return True
    if host in _AUTHORITATIVE_EXACT_HOSTS:
        if host == "huggingface.co":
            return bool(
                path_parts
                and (
                    path_parts[0] in {"blog", "docs", "papers"}
                    or path_parts[0] in _OFFICIAL_HUGGINGFACE_ORGS
                )
            )
        return True
    if host == "github.com":
        return bool(path_parts and path_parts[0] in _OFFICIAL_GITHUB_ORGS)
    return False


def authoritative_sources(urls: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    return sorted({str(url) for url in urls if is_authoritative_source(str(url))})


def prefer_authoritative_sources(
    urls: list[str] | set[str] | tuple[str, ...],
) -> list[str]:
    """Prefer recognized primary sources without erasing an otherwise valid result set.

    The allowlist is deliberately conservative, so an empty authoritative subset is
    not proof that every fetched page is unofficial.  Callers may therefore use this
    helper after their own relevance checks: it removes lower-quality alternatives
    only when at least one recognized primary source is actually available.
    """
    bounded = sorted({str(url) for url in urls if str(url).strip()})
    primary = authoritative_sources(bounded)
    return primary or bounded
