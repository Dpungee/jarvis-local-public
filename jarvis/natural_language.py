from __future__ import annotations

import re
import unicodedata
from html import unescape


_LOCAL_PATH_PREFIX = (
    r"(?:[A-Za-z]:[\\/]|"
    # A UNC share root is itself a complete local target; a trailing path
    # separator is not required. Keep the server/share grammar bounded so a
    # prose backslash cannot absorb arbitrary text.
    r"\\\\[^\\/\s<>:\"|?*]+[\\/][^\\/\s<>:\"|?*]+|"
    r"(?:\.\.?|~)[\\/]|"
    # Protect root-level POSIX/WSL files and directories as well as nested
    # paths. Protocol-relative URLs are excluded, while HTTP(S) URLs are
    # consumed by the URL alternative before local-path matching.
    r"/(?!/)(?=[^\s<>\"'])"
    r")"
)


_EXPLICIT_PUBLIC_ACTION = (
    r"(?:(?:[ \t]+|[ \t]*[,;][ \t]*)(?:then|and[ \t]+then)[ \t]+|"
    r"[ \t]*[.!?;][ \t]*)"
    r"(?:research|search(?:[ \t]+for)?|look[ \t]+up|check)\b"
)
_EXPLICIT_NEXT_LOCAL_PATH = (
    r"[ \t]+and[ \t]+(?=" + _LOCAL_PATH_PREFIX + r")"
)
_BARE_LOCAL_ACTION = r"(?:read|open|inspect|edit|delete|rename|move|copy|load|review|analyze)"
_BARE_LOCAL_FILENAME = r"[^\s<>:\"/\\|?*]{1,200}\.[A-Za-z0-9]{1,12}"
_IMPLICIT_LOCAL_FILENAME = (
    r"[^\s<>:\"/\\|?*]{1,200}\.(?:cfg|conf|csv|docx?|env|ini|json|log|md|"
    r"pdf|pptx?|py|toml|tsv|txt|xlsx?|ya?ml)"
)
_RELATIVE_LOCAL_PATH = (
    r"(?:[A-Za-z0-9._-]{1,100}[\\/]){1,20}"
    r"[A-Za-z0-9._-]{1,200}(?:\.[A-Za-z0-9]{1,12})?"
)
_INERT_FENCED_TEXT = (
    r"(?:```[\s\S]{0,50000}?(?:```|\Z)|~~~[\s\S]{0,50000}?(?:~~~|\Z))"
)
_INERT_INLINE_CODE = r"`[^`\r\n]{0,50000}(?:`|$)"
_INERT_QUOTED_TEXT = (
    r'(?:"[^\"]{0,50000}(?:"|$)|“[^”]{0,50000}(?:”|$)|'
    r"‘[^’]{0,50000}(?:’|$)|«[^»]{0,50000}(?:»|$)|"
    r"(?<!\w)'[^']{1,50000}(?:'(?!\w)|$))"
)
_INERT_BLOCKQUOTE = (
    r"(?m:^[ \t]*>[^\r\n]{0,50000}"
    r"(?:\r?\n(?![ \t]*$)(?![ \t]*>)[^\r\n]{0,50000}){0,100})"
)
_INERT_INDENTED_TEXT = r"(?m:^(?: {4}|\t)[^\r\n]{0,50000})"
_INERT_HTML_CONTAINER = (
    r"<(?P<html_tag>code|blockquote|kbd|pre|samp|script|style|textarea)"
    r"\b[^>]{0,500}>"
    r"[\s\S]{0,50000}?(?:</(?P=html_tag)\s*>|\Z)"
)
_INERT_MARKDOWN_LINK = (
    r"\[[^\]\r\n]{0,50000}\]"
    r"\((?:[^()\r\n]|\([^()\r\n]{0,50000}\)){0,50000}\)"
)
_INERT_LABELED_EXAMPLE = (
    r"(?mi:^[ \t]*(?:for[ \t]+example|example|hypothetical(?:ly)?|prompt|"
    r"sample|test[ \t]+case)[ \t]*[:,][ \t]*(?:\r?\n)?"
    r"[^\r\n]{0,50000})"
)


_CLASSIFICATION_PROTECTED_SPAN = re.compile(
    r"(?P<fenced_text>" + _INERT_FENCED_TEXT + r")|"
    r"(?P<html_container>" + _INERT_HTML_CONTAINER + r")|"
    r"(?P<inline_code>" + _INERT_INLINE_CODE + r")|"
    r"(?P<markdown_link>" + _INERT_MARKDOWN_LINK + r")|"
    r"(?P<quoted_path>[\"']" + _LOCAL_PATH_PREFIX + r"[^\"'\r\n]*[\"'])|"
    r"(?P<quoted_text>" + _INERT_QUOTED_TEXT + r")|"
    r"(?P<blockquote>" + _INERT_BLOCKQUOTE + r")|"
    r"(?P<indented_text>" + _INERT_INDENTED_TEXT + r")|"
    r"(?P<labelled_example>" + _INERT_LABELED_EXAMPLE + r")|"
    r"(?P<url>(?:https?://|www\.)[^\s<>\"']+)|"
    r"(?P<bare_path>\b" + _BARE_LOCAL_ACTION + r"[ \t]+" +
    _BARE_LOCAL_FILENAME + r"(?=$|[\s,;.!?)\]}]))|"
    # Classification does not authorize public routing, so it can stop a
    # spaced path at a bounded extension and continue normalizing the suffix.
    # Without such a boundary it still protects the rest of the line.
    r"(?P<spaced_local_path>(?<!\w)" + _LOCAL_PATH_PREFIX +
    r"(?=[^<>:\"|?*\r\n]{0,500}[ \t])(?:"
    r"[^<>:\"|?*\r\n]{0,500}?\.[A-Za-z0-9]{1,12}"
    r"(?=$|[\s,;.!?)\]}])|[^<>:\"|?*\r\n]{0,500}))|"
    r"(?P<path>(?<!\w)" + _LOCAL_PATH_PREFIX + r"[^\s<>\"']*)|"
    r"(?P<relative_path>(?<![\w:/\\])" + _RELATIVE_LOCAL_PATH +
    r"(?=$|[\s,;.!?)\]}]))|"
    r"(?P<implicit_file>(?<![\w/\\])" + _IMPLICIT_LOCAL_FILENAME +
    r"(?=$|[\s,;.!?)\]}]))",
    re.I,
)


_ROUTING_PROTECTED_SPAN = re.compile(
    r"(?P<fenced_text>" + _INERT_FENCED_TEXT + r")|"
    r"(?P<html_container>" + _INERT_HTML_CONTAINER + r")|"
    r"(?P<inline_code>" + _INERT_INLINE_CODE + r")|"
    r"(?P<markdown_link>" + _INERT_MARKDOWN_LINK + r")|"
    r"(?P<quoted_path>[\"']" + _LOCAL_PATH_PREFIX + r"[^\"'\r\n]*[\"'])|"
    r"(?P<quoted_text>" + _INERT_QUOTED_TEXT + r")|"
    r"(?P<blockquote>" + _INERT_BLOCKQUOTE + r")|"
    r"(?P<indented_text>" + _INERT_INDENTED_TEXT + r")|"
    r"(?P<labelled_example>" + _INERT_LABELED_EXAMPLE + r")|"
    r"(?P<url>(?:https?://|www\.)[^\s<>\"']+)|"
    # A bare filename is a local target only in an explicit file-operation
    # grammar. Without a clear new public-action clause, mask the ambiguous
    # remainder too so words inside it cannot authorize network routing.
    r"(?P<bare_path>\b" + _BARE_LOCAL_ACTION + r"[ \t]+(?=" +
    _BARE_LOCAL_FILENAME + r")(?:(?:" + _BARE_LOCAL_FILENAME + r")"
    r"(?=" + _EXPLICIT_PUBLIC_ACTION + r")|[^\r\n]{1,500}))|"
    # An unquoted local path containing spaces is grammatically ambiguous:
    # whitespace could separate a path component or the next instruction. If
    # it has a bounded extension followed by an explicit new public-action
    # clause, stop there; otherwise protect the rest of the line. Routing may
    # lose an ambiguous suffix, but it must never leak one.
    r"(?P<spaced_local_path>(?<!\w)" + _LOCAL_PATH_PREFIX +
    r"(?=[^<>:\"|?*\r\n]{0,500}[ \t])(?:"
    r"[^<>:\"|?*\r\n]{0,500}?\.[A-Za-z0-9]{1,12}"
    r"(?=(?:" + _EXPLICIT_PUBLIC_ACTION + r"|" +
    _EXPLICIT_NEXT_LOCAL_PATH + r"))|"
    r"[^<>:\"|?*\r\n]{0,500}))|"
    r"(?P<path>(?<!\w)" + _LOCAL_PATH_PREFIX + r"[^\s<>\"']*)|"
    r"(?P<relative_path>(?<![\w:/\\])" + _RELATIVE_LOCAL_PATH +
    r"(?=$|[\s,;.!?)\]}]))|"
    r"(?P<implicit_file>(?<![\w/\\])" + _IMPLICIT_LOCAL_FILENAME +
    r"(?=$|[\s,;.!?)\]}]))",
    re.I,
)
_WORD = re.compile(r"\b[A-Za-z][A-Za-z'’]*\b")
_CURRENT_TIME = re.compile(
    r"\b(?:today|tonight|right\s+now|currently|current|latest|newest|"
    r"this\s+(?:week|month|year)|next\s+(?:week|month|year)|yet)\b",
    re.I,
)
_CURRENT_PUBLIC_STATE = re.compile(
    r"\b(?:available|availability|released?|release|out|version|versions?|"
    r"schedule|scheduled|tour|touring|concerts?|price|stock|news|headlines?|"
    r"weather|forecast)\b",
    re.I,
)
_QUESTION_OR_LOOKUP = re.compile(
    r"(?:\?|^\s*(?:(?:hey|hi|hello|yo|okay|ok|so|well|please)[,\s]+){0,3}"
    r"(?:is|are|was|were|has|have|do|does|did|will|when|where|what|"
    r"which|check|find|look\s+up|search)\b)",
    re.I,
)
_PUBLIC_ROUTING_CLAUSE = re.compile(r"(?<=[.!?;\r\n])|\bbut\b", re.I)
_EXPLICIT_PUBLIC_ROUTING = re.compile(
    r"\b(?:browse|research|look\s+up|search(?:\s+(?:the\s+)?(?:web|internet))?|"
    r"check\s+(?:online|the\s+web))\b",
    re.I,
)
_LOCAL_THEN_PUBLIC_ROUTING = re.compile(
    r"\[local-path\][^.!?;\r\n]{0,80}\b(?:then|and\s+then)\s+"
    r"(?:browse|research|look\s+up|search(?:\s+(?:the\s+)?(?:web|internet))?|"
    r"check\s+(?:online|the\s+web))\b",
    re.I,
)
_PUBLIC_RESEARCH_WITH_LOCAL_OUTPUT = re.compile(
    r"\b(?:browse|research|look\s+up|search(?:\s+(?:the\s+)?(?:web|internet))?)\b"
    r"[^.!?;\r\n]{1,400}\b(?:create|export|generate|produce|save|write)\b"
    r"[^.!?;\r\n]{0,180}\[local-path\]",
    re.I,
)

# Tool exposure is an authority decision, not ordinary topic classification.
# Quoted/code/blockquoted material is already replaced by ``intent_routing_text``.
# This second pass removes an explicitly negated action through the next clear
# clause boundary so examples such as ``do not delete files`` cannot expose a
# mutation tool merely because they contain an action verb.  A later positive
# clause (``but create a backup``) survives and is evaluated independently.
_AUTHORITY_ACTION = (
    r"(?:add|build|change|clean|compile|copy|create|debug|delete|design|develop|"
    r"draw|edit|enhance|execute|export|fix|generate|implement|improve|install|"
    r"launch|make|modify|move|open|patch|recolor|redesign|refactor|refine|"
    r"remember|remove|rename|repair|replace|restart|restore|retouch|run|save|"
    r"start|stop|store|test|trash|update|upscale|verify|write)"
)
_NEGATED_AUTHORITY_ACTION = re.compile(
    r"(?:\b(?:do|should|must|will|can|could|would|may|might)\s+"
    r"(?:[*_]{0,3})not(?:[*_]{0,3})\b|"
    r"\b(?:cannot|can['’]?t|don['’]?t|dont)\b|"
    r"\bnever\b|\bavoid\b|\bwithout\b|\binstead\s+of\b)"
    r"(?=(?:(?!\b(?:but|however|instead|then)\b|[,!?;]).){0,240}"
    r"\b" + _AUTHORITY_ACTION + r"\b)"
    r"(?:(?!\b(?:but|however|instead|then)\b|[,!?;]).){0,500}",
    re.I | re.S,
)
_ADVISORY_AUTHORITY_ACTION = re.compile(
    r"(?:(?<=^)|(?<=[.!?;\r\n]))[ \t]*"
    r"(?:please\s+)?(?:"
    r"how\s+(?:do|can|could|would|should)\s+(?:i|we|one|you)\b|"
    r"(?:should|can|could|may|might|do|did)\s+(?:i|we)\b|"
    r"(?:do|did|have)\s+you\b|"
    r"should\s+you\b|"
    r"(?:what|where|when|why|who|which)\s+"
    r"(?:do|does|did|can|could|would|should|will|are|is|was|were|have|has)\b|"
    r"what\s+(?:if|happens\b|(?:would|will|could|might)\s+happen\s+if\b)|"
    r"(?:if|when)\s+(?:i|we|you|it|they)\b|"
    r"is\s+it\s+(?:okay|safe|wise|possible|advisable)\s+to\b|"
    r"(?:explain|describe|show\s+me|tell\s+me)\b[^.!?;\r\n]{0,80}\b"
    r"(?:how|why|where|when|what|whether)\b|"
    r"(?:can|could|would)\s+you\s+"
    r"(?:explain|describe|show\s+me|tell\s+me)\b"
    r"[^.!?;\r\n]{0,80}\b(?:how|why|where|when|what|whether)\b|"
    r"i\s+(?:want|would\s+like)\s+to\s+know\b[^.!?;\r\n]{0,80}\b"
    r"(?:how|why|where|when|what|whether)\b"
    r")"
    r"(?=(?:(?![.!?;\r\n]).){0,240}\b" + _AUTHORITY_ACTION + r"\b)"
    r"(?:(?![!?;\r\n]|\.(?=[ \t]|$)).){0,500}",
    re.I,
)
_AUTHORITY_ACTION_TOKEN = re.compile(r"\b" + _AUTHORITY_ACTION + r"\b", re.I)
_AFFIRMATIVE_AUTHORITY_CLAUSE = re.compile(
    r"(?:^|(?<=[!?;,\r\n])|(?<!\d\.)(?<=\.)|"
    r"\b(?:but|however|instead|then)\b)[ \t]*"
    r"(?:jarvis[ \t]*[,:\-]?[ \t]*)?"
    r"(?:"
    r"(?:inspect|read|review|check|examine)[ \t]+"
    r"(?:(?![!?;\r\n]|\.(?=[ \t]|$)).){0,120}"
    r"\b(?:and|then)[ \t]+" + _AUTHORITY_ACTION + r"\b|"
    r"(?:(?:please|now|just|then|okay|ok)[ \t]+)*"
    r"(?:go[ \t]+ahead(?:[ \t]+and)?[ \t]+)?"
    r"" + _AUTHORITY_ACTION + r"\b|"
    r"(?:can|could|would|will)[ \t]+you[ \t]+(?:please[ \t]+)?"
    r"" + _AUTHORITY_ACTION + r"\b|"
    r"i[ \t]+(?:want|need|would[ \t]+like)[ \t]+"
    r"(?:you|jarvis)[ \t]+to[ \t]+(?:please[ \t]+)?"
    r"(?:go[ \t]+(?:ahead[ \t]+)?and[ \t]+)?"
    r"" + _AUTHORITY_ACTION + r"\b|"
    r"let['’]?s[ \t]+" + _AUTHORITY_ACTION + r"\b|"
    r"(?:you|jarvis)[ \t]+(?:must|should|need[ \t]+to)[ \t]+"
    r"" + _AUTHORITY_ACTION + r"\b"
    r")"
    r"(?:(?![!?;\r\n]|\.(?=[ \t]|$)).){0,500}",
    re.I,
)
_ACTION_RETRACTION = re.compile(
    r"^\?[ \t]*(?:no\b|never[ \t]+mind\b|"
    r"actually\b[^.!?;\r\n]{0,40}\bnot\b)",
    re.I,
)

# These are conversational abbreviations, not task or capability names.  The
# normalized text is only a read-only view for intent classification; callers
# must retain the exact operator text for targets, permissions, tool arguments,
# approvals, writes, execution, and external actions.
_SHORTHAND = {
    "abt": "about",
    "b4": "before",
    "cuz": "because",
    "idk": "i do not know",
    "nxt": "next",
    "pls": "please",
    "plz": "please",
    "rn": "right now",
    "tho": "though",
    "thx": "thanks",
    "u": "you",
    "ur": "your",
    "wanna": "want to",
    "whats": "what is",
    "wht": "what",
    "wuts": "what is",
    "ya": "you",
    "yr": "year",
    "yrs": "years",
}

# A deliberately small grammar/recency vocabulary supports conservative
# single-edit correction.  It contains no products, domains, apps, people,
# files, tool names, or action-authority terms.
_INTENT_VOCABULARY = frozenset({
    "about",
    "available",
    "availability",
    "browse",
    "check",
    "concert",
    "current",
    "currently",
    "forecast",
    "latest",
    "news",
    "opinion",
    "release",
    "research",
    "schedule",
    "think",
    "today",
    "tour",
    "touring",
    "version",
    "weather",
})


def _distance_at_most_one(left: str, right: str) -> bool:
    """Return whether two bounded ASCII tokens have Levenshtein distance <= 1."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = 0
    long_index = 0
    skipped = False
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        if skipped:
            return False
        skipped = True
        long_index += 1
    return True


def _correct_intent_token(token: str) -> str:
    folded = token.casefold().replace("’", "'")
    replacement = _SHORTHAND.get(folded)
    if replacement is not None:
        return replacement
    # Three-letter ordinary words have too many one-edit neighbors (for
    # example ``new`` -> ``news``). Shorthand remains handled explicitly
    # above; fuzzy correction starts at four characters.
    if len(folded) < 4 or not folded.isascii() or not folded.isalpha():
        return token
    candidates = [
        candidate
        for candidate in _INTENT_VOCABULARY
        if candidate[0] == folded[0]
        and _distance_at_most_one(folded, candidate)
    ]
    if len(candidates) != 1:
        return token
    return candidates[0]


def _normalize_unprotected(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return _WORD.sub(lambda match: _correct_intent_token(match.group(0)), normalized)


def _inert_target_placeholder(value: str) -> str | None:
    """Preserve only a quoted/backticked target's extension for action routing."""

    text = str(value).strip()
    pairs = (
        ("`", "`"),
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("«", "»"),
    )
    body: str | None = None
    for left, right in pairs:
        if (
            text.startswith(left)
            and text.endswith(right)
            and len(text) > len(left) + len(right)
        ):
            body = text[len(left):-len(right)].strip()
            break
    if body is None or "\n" in body or "\r" in body:
        return None
    extension = re.search(r"\.([A-Za-z0-9]{1,12})\s*$", body)
    if extension is None:
        return None
    return f"local-target.{extension.group(1).casefold()}"


def _classification_view(
    value: str,
    *,
    limit: int,
    mask_local_paths: bool,
    mask_inert: bool,
) -> str:
    """Build a read-only intent view without ever normalizing protected targets."""
    # Bound the raw operator text first. Normalizing the whole value before
    # finding targets can silently rewrite a URL or path (for example a
    # ligature inside a filename), which violates the target/approval trust
    # boundary even when today's caller uses the result only for routing.
    # Entity decoding is confined to this read-only classification view. It
    # prevents HTML-encoded quote delimiters from turning private content into
    # routing authority while never changing an actual target or tool argument.
    text = unescape(str(value)[: max(0, int(limit))])
    pieces: list[str] = []
    cursor = 0
    protected_span = (
        _ROUTING_PROTECTED_SPAN
        if mask_local_paths
        else _CLASSIFICATION_PROTECTED_SPAN
    )
    for match in protected_span.finditer(text):
        pieces.append(_normalize_unprotected(text[cursor:match.start()]))
        if match.lastgroup == "bare_path":
            action, target = re.split(r"[ \t]+", match.group(0), maxsplit=1)
            if mask_local_paths:
                pieces.append(f"{_normalize_unprotected(action)} [local-path] ")
            else:
                pieces.append(_normalize_unprotected(action) + " " + target)
        elif match.lastgroup == "markdown_link":
            public_url = re.search(r"https?://[^\s<>)\"']+", match.group(0), re.I)
            if public_url is not None:
                pieces.append(f" {public_url.group(0)} ")
            elif mask_inert:
                pieces.append(" [inert-text] ")
            else:
                pieces.append(match.group(0))
        elif match.lastgroup in {
            "fenced_text",
            "html_container",
            "inline_code",
            "quoted_text",
            "blockquote",
            "indented_text",
            "labelled_example",
        }:
            if mask_inert:
                placeholder = (
                    _inert_target_placeholder(match.group(0))
                    if match.lastgroup in {"inline_code", "quoted_text"}
                    else None
                )
                pieces.append(f" {placeholder or '[inert-text]'} ")
            else:
                pieces.append(match.group(0))
        elif mask_local_paths and match.lastgroup != "url":
            pieces.append(" [local-path] ")
        else:
            pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(_normalize_unprotected(text[cursor:]))
    return re.sub(r"[ \t]+", " ", "".join(pieces)).strip()


def intent_classification_text(value: str, *, limit: int = 50_000) -> str:
    """Return a conservative conversational view for read-only classification.

    URLs and local-looking paths remain byte-for-byte unchanged.  This helper
    deliberately does not return targets or authority, and callers must never
    use its output as a tool argument or approval resource.
    """
    return _classification_view(
        value,
        limit=limit,
        mask_local_paths=False,
        mask_inert=False,
    )


def intent_routing_text(value: str, *, limit: int = 50_000) -> str:
    """Return an intent-only view with local targets masked from route signals.

    HTTP(S) URLs remain visible because they are public-web evidence candidates.
    Local drive, UNC, home, relative, and explicit bare-file targets are
    replaced before any ``research``/``latest``/``news`` regex can mistake a
    filename for permission to send the operator's private target to a public
    provider. Quoted, code, and blockquoted data is inert for the same reason.
    """
    return _classification_view(
        value,
        limit=limit,
        mask_local_paths=True,
        mask_inert=True,
    )


def operator_action_text(value: str, *, limit: int = 50_000) -> str:
    """Return a fail-closed read-only view for deciding tool exposure.

    The returned text is suitable only for action classification. It is never
    a target, tool argument, permission, or approval resource. Inert examples
    and explicitly negated action clauses are masked while a distinct positive
    clause remains visible.
    """

    text = _classification_view(
        value,
        limit=limit,
        mask_local_paths=False,
        mask_inert=True,
    )
    text = _NEGATED_AUTHORITY_ACTION.sub(" [negated-action] ", text)
    affirmative_spans: list[tuple[int, int]] = []
    for match in _AFFIRMATIVE_AUTHORITY_CLAUSE.finditer(text):
        if _ACTION_RETRACTION.match(text[match.end():match.end() + 100]):
            continue
        affirmative_spans.append(match.span())

    def retain_affirmative_action(match: re.Match[str]) -> str:
        if any(start <= match.start() < end for start, end in affirmative_spans):
            return match.group(0)
        return "[non-directive-action]"

    text = _AUTHORITY_ACTION_TOKEN.sub(retain_affirmative_action, text)
    return re.sub(
        r"[ \t]+",
        " ",
        text,
    ).strip()


def has_current_public_information_shape(value: str) -> bool:
    """Recognize generic current-public-fact grammar without naming domains.

    This remains an intent signal only.  It neither proves that the subject is
    public nor authorizes a tool; callers should combine it with their existing
    lane, scope, and tool gates.
    """
    text = intent_routing_text(value)
    return bool(
        _CURRENT_TIME.search(text)
        and _CURRENT_PUBLIC_STATE.search(text)
        and _QUESTION_OR_LOOKUP.search(text)
    )


def public_web_evidence_boundary_allows(value: str) -> bool:
    """Reject public-web evidence derived only from private/inert containers.

    This is a routing trust-boundary check, not a positive tool authorization.
    With no protected span it remains neutral. If local or inert material is
    present, a distinct operator-authored public clause (or an explicit
    ``then research`` transition) must survive masking.
    """

    text = intent_routing_text(value)
    if "[local-path]" not in text and "[inert-text]" not in text:
        return True
    if _LOCAL_THEN_PUBLIC_ROUTING.search(text):
        return True
    if _PUBLIC_RESEARCH_WITH_LOCAL_OUTPUT.search(text):
        return True
    for clause in _PUBLIC_ROUTING_CLAUSE.split(text):
        if not clause or "[local-path]" in clause:
            continue
        visible = clause.replace("[inert-text]", " ")
        if _EXPLICIT_PUBLIC_ROUTING.search(visible):
            return True
        if (
            _CURRENT_TIME.search(visible)
            and _CURRENT_PUBLIC_STATE.search(visible)
            and _QUESTION_OR_LOOKUP.search(visible)
        ):
            return True
    return False


__all__ = [
    "has_current_public_information_shape",
    "intent_classification_text",
    "intent_routing_text",
    "operator_action_text",
    "public_web_evidence_boundary_allows",
]
