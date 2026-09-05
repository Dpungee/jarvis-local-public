"""Deterministic extraction of one operator-authored project fact from a turn.

The extractor never writes memory.  It proposes the exact governed command that
would store the fact so the operator can confirm it with one paste or one
confirmation reply, and it lets the runtime say truthfully that nothing was
stored.  Every proposal has already passed
:func:`jarvis.governed_memory.parse_explicit_project_fact`, so the proposal can
only ever describe a fact the governed write path would accept.

Rules, not a model: the operator's own words are the only source, the split
into subject / predicate / value follows a small closed grammar, and anything
outside that grammar yields no proposal rather than a guess.  Questions,
imperatives, reported, hedged or hypothetical speech, conditionals, pronoun
subjects without an antecedent in the same turn, personal-relation subjects,
special-category personal data about a named person, control-plane subjects,
and anything the governed parser rejects all yield ``None``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .governed_memory import GovernedMemoryCommandError, parse_explicit_project_fact

MAX_EXTRACTION_CHARS = 2_000
_MAX_SUBJECT_TOKENS = 5

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\r?\n+")
_CLAUSE_SPLIT = re.compile(r"\s*;\s*")
_QUESTION_START = re.compile(
    r"\A\s*(?:what|which|when|where|who|whom|whose|why|how|is|are|was|were|do|"
    r"does|did|can|could|should|would|will|shall|may|might|has|have|had|"
    r"tell\s+me|show\s+me|list|find|search|look\s+up)\b",
    re.I,
)
_IMPERATIVE_START = re.compile(
    r"\A\s*(?:please\s+)?(?:tell|write|update|set|change|make|add|remove|send|"
    r"check|run|open|create|delete|put|let|ask|ping|email|call|go|search|find|"
    r"show|list|look|give|help|ensure|fix|use|switch|move|draft|summari[sz]e|"
    r"explain|describe|generate|build|deploy|restart|stop|start|kill|install|"
    r"configure|edit|rename|refactor|review|test|try|read|fetch|download|"
    r"upload|save|store|forget|keep|schedule|remind|book|order|buy|pay|"
    r"translate|convert|compute|calculate|print|copy|paste|format|sort|filter|"
    r"merge|rebase|commit|push|pull|clone|publish|post|share|notify|alert|"
    r"warn|migrate|bump|rotate|point|relocate|reassign|replace|upgrade|"
    r"downgrade|retire|promote|roll|assign|attach|detach|mount|bind|route|"
    r"redirect|scale|provision|decommission|please)\b",
    re.I,
)
_CONDITIONAL_START = re.compile(
    r"\A\s*(?:if|suppose|supposing|assuming|assume|imagine|unless|whether|"
    r"when|whenever|in\s+case|hypothetically|what\s+if|say|pretend(?:ing)?|"
    r"let'?s\s+say|lets\s+say|for\s+argument'?s\s+sake|in\s+theory|"
    r"theoretically|ideally|wondering|curious|doubt(?:ing)?|hop(?:e|ing)|"
    r"betting|guess(?:ing)?|thinking|wish(?:ing)?|considering|"
    r"fingers\s+crossed|rumou?r\s+has\s+it|any\s+idea|no\s+idea|"
    r"back\s+in\s+(?:19|20)\d{2}|in\s+my\s+dream)\b",
    re.I,
)
_REPORTED_OR_UNCERTAIN = re.compile(
    r"\b(?:according\s+to|reportedly|apparently|allegedly|supposedly|"
    r"presumably|maybe|perhaps|probably|possibly|hopefully|ideally|"
    r"theoretically|i\s+(?:think|believe|guess|suspect|assume|hope|heard|"
    r"wonder|doubt|want|need|wish|bet)|not\s+sure|unsure|no\s+idea|any\s+idea|"
    r"rumou?r\s+has\s+it|fingers\s+crossed|my\s+guess|for\s+argument'?s\s+sake|"
    r"would\s+be\s+(?:great|nice|good|ideal)\s+if|could\s+be\s+worth|"
    r"we\s+should|(?:might|may|could)\s+(?:be|have)|"
    r"(?:he|she|they|someone|somebody|everyone|people|docs?|documentation|"
    r"readme|wiki|vendor|support|the\s+team|the\s+docs?)\s+"
    r"(?:said|says?|claims?|claimed|thinks?|thought|mentioned|reported|"
    r"suggests?|suggested|told|tells|wrote|writes|recommends?))\b",
    re.I,
)
_UPDATE_CUE = re.compile(
    r"\b(?:now|no\s+longer|from\s+now\s+on|going\s+forward|changed|moved|"
    r"switched|updated?|renamed|became|becomes|instead\s+of|these\s+days|"
    r"nowadays|currently|henceforth|at\s+the\s+moment|"
    r"bumped|migrated|relocated|reassigned|replaced|upgraded|downgraded|"
    r"retired|promoted|rolled\s+(?:back|out|over)|"
    r"as\s+of\s+(?:today|now|this\s+(?:week|month|release|sprint)))\b|"
    r"\bnot\b[^.]{0,30}\banymore\b",
    re.I,
)
_STATEMENT_CUE = re.compile(
    r"\A\s*(?:(?:please\s+)?(?:note|remember|record|keep\s+in\s+mind)(?:\s+that)?\b[,:]?|"
    r"for\s+the\s+record[,:]?|fyi[,:]?|by\s+the\s+way[,:]?|btw[,:]?|"
    r"just\s+so\s+you\s+know[,:]?|heads[\s-]+up[,:]?|update[,:]|reminder[,:]?|"
    r"as\s+a\s+reminder[,:]?|quick\s+note[,:]?|psa[,:]?|correction[,:]|"
    r"clarification[,:]|note[,:])\s*",
    re.I,
)
_LEADING_UPDATE_CUE = re.compile(
    r"\A\s*(?:going\s+forward|from\s+now\s+on|"
    r"as\s+of\s+(?:today|now|this\s+(?:week|month|release|sprint)|the\s+latest\s+release)|"
    r"effective\s+(?:immediately|today|now)|henceforth|these\s+days|nowadays|"
    r"currently|now|starting\s+(?:today|now|this\s+(?:week|month|sprint)))[,:]?\s+",
    re.I,
)
_DISCOURSE_OPENER = re.compile(
    r"\A(?:(?:also|and|so|oh|well|plus|additionally|anyway|ok|okay|yes|right|"
    r"actually|alright|thanks|thank\s+you|great|cool|good|fine|sure)[,:!.]?\s+)+",
    re.I,
)
_DETERMINERS = frozenset({
    "the", "a", "an", "our", "my", "this", "that", "these", "those", "your",
    "its", "his", "her", "their", "whose",
})
_PRONOUN_SUBJECTS = frozenset({
    "i", "we", "you", "he", "she", "it", "they", "this", "that", "these",
    "those", "there", "here", "everyone", "someone", "nothing", "something",
    "everything", "anyone", "me", "us", "them", "let", "i'm", "we're",
})
_RESOLVABLE_PRONOUNS = frozenset({"it", "they", "this", "that"})
_PERSONAL_RELATIONS = frozenset({
    "mother", "mom", "mum", "father", "dad", "parent", "parents", "wife",
    "husband", "spouse", "partner", "girlfriend", "boyfriend", "fiance",
    "fiancee", "son", "daughter", "kid", "kids", "child", "children", "baby",
    "sister", "brother", "sibling", "grandma", "grandmother", "grandpa",
    "grandfather", "aunt", "uncle", "cousin", "niece", "nephew", "friend",
    "friends", "buddy", "roommate", "neighbor", "neighbour", "doctor", "dentist",
    "therapist", "lawyer", "landlord",
})
# A subject that names the control plane is not a project fact; storing
# "assistant / name / Friday" or "system / mode / developer" would let a
# stored row read like configuration.
_CONTROL_SUBJECTS = frozenset({
    "assistant", "agent", "model", "system", "runtime", "memory", "prompt",
    "persona", "jarvis", "ai", "bot", "chatbot", "llm", "you", "operator",
    "instructions", "instruction", "rules", "rule", "policy",
})
# Reserved governed namespaces; the parser rejects them as predicate prefixes
# and the extractor refuses them as subject or predicate heads so a reserved
# word cannot migrate into the subject ("identity / provider / Okta").
_RESERVED_WORDS = frozenset({
    "identity", "permission", "permissions", "preference", "preferences", "safety",
})
# Special-category personal data is never proposed, whoever the subject is.
_SENSITIVE_PREDICATE = re.compile(
    r"\b(?:ssn|social\s+security|national\s+id|passport|driver'?s?\s+licen[cs]e|"
    r"licen[cs]e\s+number|date\s+of\s+birth|dob|birth\s*date|home\s+address|"
    r"street\s+address|mailing\s+address|residential|diagnosis|diagnoses|"
    r"medication|prescription|blood\s+(?:type|pressure|sugar)|medical|"
    r"health\s+(?:condition|status|issue|record|problem)|therapy|therapist|"
    r"disability|pregnan\w*|religion|faith|sexuality|sexual|orientation|"
    r"gender\s+identity|ethnicity|race|political|party\s+affiliation|"
    r"union\s+member\w*|criminal|arrest|conviction|visa|immigration|"
    r"citizenship|net\s+worth|salary|salaries|income|wage|wages|credit\s+score|"
    r"bank\s+account|account\s+number|routing\s+number|iban|card\s+number|"
    r"passcode|pin|otp|totp|seed\s+phrase|recovery\s+(?:code|phrase|key)|"
    r"backup\s+code|signing\s+key|private\s+key|key\s+id|passphrase|"
    r"security\s+(?:question|answer)|mfa|2fa|biometric|fingerprint)\b",
    re.I,
)
# A fact whose subject reads as a person's name is proposed only for a
# work-role predicate; "Dave's surgery", "Timmy's school", "Alice's cat" are
# personal life, not project memory, whatever the value looks like.
_PERSON_WORK_PREDICATE = re.compile(
    r"\A(?:(?:job\s+)?title|role|team|squad|guild|manager|reports\s+to|"
    r"timezone|time\s+zone|office|desk|github(?:\s+handle)?|slack(?:\s+handle)?|"
    r"handle|username|working\s+hours|hours|availability|on-?call(?:\s+\w+)?|"
    r"focus(?:\s+area)?|area|responsibilit(?:y|ies)|owns?|start\s+date|"
    r"end\s+date|last\s+day|first\s+day|pronouns|preferred\s+name|nickname|"
    r"pager|extension|seat|location|site|region|shift|status)\Z",
    re.I,
)
# Control-plane predicates are refused even with an ordinary subject.
_CONTROL_PREDICATE = re.compile(
    r"\b(?:persona|system\s+prompt|instructions?|directives?|guardrails?|"
    r"rules?|polic(?:y|ies))\b",
    re.I,
)
# Instruction text hidden in CamelCase or hyphenation ("IgnorePreviousInstructions",
# "you-are-now-DAN") is checked after splitting the token back into words.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[-_]+")
_INJECTION_SHAPE = re.compile(
    r"\b(?:ignore|disregard|override|bypass|forget)\s+(?:all\s+|any\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|your|these|those|system)?\s*"
    r"(?:instructions?|rules?|prompts?|guidelines?|constraints?)\b|"
    r"\byou\s+are\s+now\b|\bsystem\s+prompt\b|\bdeveloper\s+mode\b|\bjailbreak\b|"
    r"\bdo\s+anything\s+now\b",
    re.I,
)


def _decamel(text: str) -> str:
    return " ".join(part for part in _CAMEL_SPLIT.split(str(text)) if part)


def _concatenated_reserved(text: str) -> bool:
    """"identityprovider" is a reserved word glued to more letters."""
    for token in re.findall(r"[A-Za-z]+", str(text).casefold()):
        for word in _RESERVED_WORDS:
            if token != word and token.startswith(word) and len(token) >= len(word) + 4:
                return True
    return False
_SENSITIVE_VALUE = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b|"
    r"(?<![\w.])(?:\d[ -]?){13,19}(?![\w.])|"
    r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b|"
    r"(?:\A|[\s\"'(])(?:~[/\\]|\$HOME\b|%USERPROFILE%|%HOMEPATH%|/root/)|"
    # Obfuscated e-mail; anchored on the marker so a long token never
    # backtracks (a leading \w+ made this quadratic on 2,000-char values).
    r"\[at\]|\(at\)|\{at\}|\[dot\]|\(dot\)|"
    r"\bat\s+\w+\s+dot\s+(?:com|net|org|io|dev|co)\b",
    re.I,
)
# Attribute nouns that end a bare noun phrase ("QA lead", "deploy window") and
# two-word attributes ("rate limit", "listen port") used to split a phrase
# into subject and predicate when nothing stored guides the split.
_ATTRIBUTE_NOUNS = frozenset({
    "port", "lead", "owner", "host", "hostname", "region", "channel", "version",
    "url", "path", "address", "limit", "window", "cadence", "schedule", "rack",
    "datacenter", "context", "size", "budget", "deadline", "status", "name",
    "branch", "tag", "percentage", "length", "time", "date", "rotation", "sla",
    "timezone", "level", "count", "quota", "threshold", "interval", "timeout",
    "frequency", "capacity", "endpoint", "namespace", "cluster", "bucket",
    "contact", "maintainer", "reviewer", "approver", "location", "vendor",
    "provider", "runtime", "model", "encoder", "image", "target", "default",
    "environment", "stage", "tier", "plan", "codename", "alias", "id",
    "identifier", "slug", "prefix", "suffix", "format", "encoding", "locale",
    "currency", "unit", "duration", "retention", "ttl", "priority", "severity",
    "standup", "retro", "demo", "release", "milestone", "sprint", "cycle",
    "standby", "fallback", "backup", "mirror", "replica", "primary", "leader",
    "manager", "director", "architect", "engineer", "team", "squad", "guild",
})
_COMPOUND_ATTRIBUTES = frozenset({
    "rate limit", "listen port", "base url", "tech lead", "start time",
    "end time", "release channel", "default region", "kube context",
    "config path", "deploy window", "phone number", "time zone", "end date",
    "due date", "target date", "start date", "log level", "batch size",
    "retry count", "max connections", "listening port", "admin port",
    "metrics port", "health endpoint", "docker image", "base image",
    "build target", "cache size", "pool size", "queue depth", "team lead",
    "project lead", "product owner", "release manager", "on-call lead",
    "release date", "ship date", "cutoff date", "freeze date", "api version",
    "schema version", "node version", "python version", "go version",
    "default branch", "main branch", "release branch", "storage bucket",
    "container registry", "artifact registry", "package registry",
    "home directory", "working directory", "data directory", "log directory",
    "sprint length", "cycle time", "deploy cadence", "release cadence",
})
# "X is now in/on/at <place-noun> <value>" only makes sense for place-like
# attributes; "Bob is now on paternity leave" must not become a triple.
_LOCATION_ATTRIBUTES = frozenset({
    "rack", "room", "slot", "bay", "floor", "region", "datacenter", "zone",
    "building", "cluster", "namespace", "bucket", "branch", "channel", "tier",
    "stage", "environment", "folder", "directory", "repo", "repository", "org",
    "organization", "workspace", "project", "queue", "pool", "lane", "shard",
    "partition", "segment", "vlan", "subnet", "cabinet", "cage", "hall",
    "aisle", "port", "host", "node", "server", "box", "site", "cell",
})
_TAIL_CLAUSE = re.compile(
    r"\s*(?:,|;|\(|—|–|\s-\s)\s*(?:not\b|instead\s+of\b|rather\s+than\b|"
    r"was\b|previously\b|formerly\b|it\s+was\b|up\s+from\b|down\s+from\b|"
    r"no\s+longer\b|which\b|because\b|since\b|so\b|as\s+of\b|until\b|but\b).*\Z",
    re.I | re.S,
)
_BARE_TAIL_CLAUSE = re.compile(
    r"\s+(?:instead\s+of|rather\s+than|previously|formerly|up\s+from|"
    r"down\s+from|because|since|until|as\s+of|but|didn't|did\s+not|doesn't|"
    r"does\s+not|isn't|wasn't|though|although|however|yet|except|unless)\b.*\Z",
    re.I | re.S,
)
_TRAILING_TEMPORAL = re.compile(
    r"\s+(?:now|these\s+days|nowadays|currently|at\s+the\s+moment|"
    r"going\s+forward|from\s+now\s+on|as\s+of\s+(?:today|now)|from\s+today|"
    r"today|henceforth)\s*\Z",
    re.I,
)
_TRAILING_PUNCTUATION = re.compile(r"[\s.!,;:]+\Z")
_LEADING_ARTICLE = re.compile(r"\A(?:the|a|an)\s+(?=\S)", re.I)
_LEADING_TIME_PREPOSITION = re.compile(r"\A(?:at|on|in|around|by)\s+(?=\d)", re.I)
_VALUE_SHAPE = re.compile(r"[0-9A-Z/:@.#_\-+%]")
_QUOTE_CHARACTERS = "\"'`“”‘’"
_NEGATION = re.compile(
    r"\b(?:no\s+longer|not|never|isn't|aren't|doesn't|don't|won't|wasn't|"
    r"weren't|hasn't|haven't|didn't|did\s+not|does\s+not|is\s+not|are\s+not|"
    r"was\s+not|were\s+not|has\s+not|have\s+not|cannot|can't)\b",
    re.I,
)

# Noun-phrase tokens never start with a determiner, pronoun, preposition or
# hedge verb, and never contain a function word, a negation or a temporal
# adverb (those end the phrase).
# The exclusions must match whole words only: "on-call" and "in-memory" are
# tokens, "on" and "in" are not, so the lookahead ends at whitespace or the
# end of the text rather than at a word boundary.
_WORD_END = r"(?=\s|\Z)"
_FIRST_TOKEN = (
    r"(?!(?:the|a|an|our|my|this|that|these|those|your|its|his|her|their|whose|"
    r"i|we|you|he|she|it|they|there|here|no|not|never|in|on|at|by|for|from|"
    r"with|to|of|back|once|after|before|during|since|until|over|under|"
    r"wondering|pretend|pretending|doubt|doubting|hope|hoping|betting|"
    r"guessing|guess|thinking|assuming|imagining|considering|wishing|rumou?r|"
    r"fingers|any|some|every|nothing|lets|maybe|perhaps|hopefully|ideally|"
    rf"theoretically|curious|unsure|sure|last|next|yesterday|today|tomorrow){_WORD_END})"
    r"[A-Za-z][\w.\-/]*"
)
_NEXT_TOKEN = (
    r"(?!(?:no|not|never|now|currently|if|whether|that|which|who|whom|it|its|"
    r"has|have|had|is|are|was|were|be|been|being|and|or|but|so|than|then|as|"
    r"to|for|from|with|by|on|in|into|onto|of|per|at|the|a|an|my|our|your|"
    rf"his|her|their|did|didn't|does|do){_WORD_END})[A-Za-z0-9][\w.\-/]*"
)
_NOUN_PHRASE = rf"(?P<subject>{_FIRST_TOKEN}(?:\s+{_NEXT_TOKEN}){{0,4}}?)"
_NOUN_PHRASE_GREEDY = rf"(?P<subject>{_FIRST_TOKEN}(?:\s+{_NEXT_TOKEN}){{0,4}})"
_PHRASE = rf"(?P<phrase>{_FIRST_TOKEN}(?:\s+{_NEXT_TOKEN}){{1,6}}?)"
_DETERMINER = r"(?:the\s+|our\s+|my\s+|this\s+|your\s+|its\s+)?"
_FROM_TO = r"(?:changed|went|moved|switched|bumped|jumped|dropped)\s+from\s+\S+(?:\s+\S+){0,3}?\s+to"
_TRANSITION = (
    rf"(?:{_FROM_TO}|is\s+now|are\s+now|was\s+changed\s+to|changed\s+to|"
    r"has\s+changed\s+to|moved\s+to|has\s+moved\s+to|switched\s+to|"
    r"was\s+switched\s+to|updated\s+to|was\s+updated\s+to|set\s+to|"
    r"was\s+set\s+to|became|becomes|will\s+be|will\s+now\s+be|"
    r"is\s+going\s+to\s+be|is|are)"
)
_STRONG_TRANSITION = (
    rf"(?:{_FROM_TO}|is\s+now|are\s+now|changed\s+to|has\s+changed\s+to|"
    r"moved\s+to|has\s+moved\s+to|switched\s+to|updated\s+to|"
    r"was\s+updated\s+to|set\s+to|became|becomes|will\s+now\s+be)"
)
_RELATIONS = (
    r"(?:listens?\s+on|runs?\s+on|uses?|lives?\s+(?:at|in|on)|sits?\s+(?:at|in|on)|"
    r"belongs?\s+to|reports?\s+to|targets?|ships?\s+(?:on|in)|deploys?\s+to|"
    r"points?\s+to|resolves?\s+to|maps?\s+to|goes\s+to|takes?|costs?|"
    r"(?:has\s+)?moved\s+to|is\s+owned\s+by|is\s+hosted\s+(?:on|at)|"
    r"is\s+served\s+from|is\s+deployed\s+(?:on|to)|is\s+located\s+(?:in|at)|"
    r"is\s+called|is\s+named|depends?\s+on|requires?|defaults?\s+to|"
    r"is\s+managed\s+by|is\s+maintained\s+by|is\s+led\s+by|is\s+assigned\s+to|"
    r"is\s+monitored\s+by|is\s+written\s+in|is\s+built\s+with|is\s+tested\s+with|"
    r"is\s+due\s+(?:on|by)|ends?\s+(?:on|at)|starts?\s+(?:on|at)|"
    r"is\s+scheduled\s+(?:for|at|on)|goes\s+live\s+(?:on|at)|"
    r"is\s+pinned\s+(?:to|at)|is\s+stored\s+(?:in|at|on)|is\s+backed\s+up\s+to|"
    r"expires?\s+(?:on|at))"
).replace(r"is\s+", r"is\s+(?:now\s+|currently\s+)?")
_MOVEMENT_RELATION = re.compile(r"\bmoved\s+to\b", re.I)
_UNIT_NOUN = r"(?:\s+(?P<unit>port|rack|version|host|region|branch|channel|release|slot|bay|room|floor))?"
_POSSESSIVE_FORM = re.compile(
    rf"\A{_DETERMINER}{_NOUN_PHRASE}(?:'s|’s)\s+"
    r"(?P<predicate>[A-Za-z][A-Za-z\s\-]{1,40}?)\s+"
    rf"{_TRANSITION}\s+(?P<value>.+)\Z",
    re.I | re.S,
)
# The colon forms need whitespace after the colon so a URL scheme ("https://")
# or a "key:value" token inside a sentence never counts as the separator.
_POSSESSIVE_COLON_FORM = re.compile(
    rf"\A{_DETERMINER}{_NOUN_PHRASE}(?:'s|’s)\s+"
    r"(?P<predicate>[A-Za-z][A-Za-z\s\-]{1,40}?)\s*:\s+(?P<value>\S.*)\Z",
    re.I | re.S,
)
_PHRASE_COLON_FORM = re.compile(
    rf"\A{_DETERMINER}{_PHRASE}\s*:\s+(?P<value>\S.*)\Z",
    re.I | re.S,
)
_ARROW = r"\s*(?:->|=>|→)\s*"
_ARROW_FORM = re.compile(
    rf"\A(?P<subject>[^>=|]{{1,80}}?){_ARROW}(?P<predicate>[^>=|]{{1,80}}?){_ARROW}"
    r"(?P<value>[^>=|]+?)\s*\Z",
    re.S,
)
_PLAIN_PHRASE = re.compile(r"\A[A-Za-z][\w.\-]*(?:\s+[A-Za-z0-9][\w.\-]*){0,4}\Z")
_TWO_LETTERS = re.compile(r"[A-Za-z]{2}")
_RENAME_FORM = re.compile(
    rf"\A(?:(?:we|they|i|the\s+team)\s+)?(?:have\s+|just\s+)?renamed\s+{_DETERMINER}"
    rf"{_NOUN_PHRASE}\s+(?:to|as)\s+(?P<value>.+)\Z",
    re.I | re.S,
)
_RELATIONAL_FORM = re.compile(
    rf"\A{_DETERMINER}{_NOUN_PHRASE}\s+(?:now\s+|currently\s+)?"
    rf"(?P<predicate>{_RELATIONS}){_UNIT_NOUN}\s+(?:now\s+)?(?P<value>.+)\Z",
    re.I | re.S,
)
_COPULA_PREPOSITION_FORM = re.compile(
    rf"\A{_DETERMINER}{_NOUN_PHRASE_GREEDY}\s+(?:is|are)\s+now\s+"
    r"(?:on|at|in|under|behind|inside)\s+"
    r"(?P<predicate>[A-Za-z][A-Za-z\-]{1,30})\s+(?P<value>\S.*)\Z",
    re.I | re.S,
)
_BARE_TRANSITION_FORM = re.compile(
    rf"\A{_DETERMINER}{_PHRASE}\s+{_STRONG_TRANSITION}\s+(?P<value>.+)\Z",
    re.I | re.S,
)
_BARE_COPULA_FORM = re.compile(
    rf"\A{_DETERMINER}{_PHRASE}\s+(?:is|are)\s+(?:now\s+|currently\s+)?(?P<value>.+)\Z",
    re.I | re.S,
)
_NEGATED_SUBJECT = re.compile(
    rf"\A{_DETERMINER}{_NOUN_PHRASE}\s+(?:no\s+longer|not|never|isn't|aren't|"
    r"doesn't|don't|won't|wasn't|weren't|hasn't|haven't|didn't|does\s+not|"
    r"is\s+not|are\s+not|has\s+not|did\s+not)\b",
    re.I,
)
_NOUN_PHRASE_START = re.compile(rf"\A{_DETERMINER}{_NOUN_PHRASE_GREEDY}\b", re.I)
_PREDICATE_STOPWORDS = frozenset({
    "is", "are", "the", "a", "an", "on", "in", "at", "to", "of", "by", "for",
    "now", "with", "its", "their", "our", "has", "have",
})
_MEMORY_WRITE_CLAIM = re.compile(
    r"\b(?:i(?:'ve| have|'ll| will)?|jarvis(?: has)?|this has been|it has been|"
    r"that has been|has been)\s+(?:now\s+|just\s+|successfully\s+)?"
    r"(?:updated|stored|saved|noted|recorded|remembered|logged|persisted|"
    r"written|committed|added|changed)\b[^.\n]{0,80}"
    r"\b(?:project\s+facts?|(?:to|in|into)\s+(?:my\s+|your\s+|the\s+|long[-\s]term\s+)?"
    r"memory|claim\s+record|claim\s+ledger|version\s+history|memory\s+record|as\s+a\s+fact)\b|"
    r"\bclaim\s+record\s+#\d+|"
    r"\b(?:the\s+)?(?:previous|prior|old)\s+value\b[^.\n]{0,40}\bversion\s+history\b|"
    r"\bi(?:'ll| will)\s+(?:remember\s+(?:that|this|it)\b|keep\s+(?:that|this|it)\s+in\s+mind|"
    r"make\s+a\s+note\s+of\s+(?:that|this|it)|note\s+(?:that|this|it)\s+down)|"
    r"\bi(?:'ve| have)\s+(?:made\s+a\s+note|persisted)\b|"
    r"\b(?:noted|recorded|saved|logged|stored|filed)\b[^.\n]{0,60}"
    r"\bfor\s+(?:future|later|next\s+time|the\s+record|reference|future\s+reference)\b|"
    r"\b(?:saved|stored|written|logged|persisted|recorded|committed)\s+"
    r"(?:to|in|into)\s+(?:the\s+|your\s+|my\s+)?(?:memory|claim\s+ledger|ledger|project\s+facts?)\b|"
    r"\bmemory\s+(?:has\s+been\s+|is\s+|was\s+)?updated\b|"
    r"\bfacts?\s+(?:has\s+been\s+|have\s+been\s+|is\s+|was\s+)?(?:recorded|stored|saved|updated|persisted)\b|"
    r"\A\s*remembered[.!]?\s*\Z|"
    r"\bconsider\s+(?:it|that|this)\s+(?:noted|recorded|saved|stored|remembered)\b|"
    r"\bproject\s+facts?\s+(?:now\s+)?(?:shows?|reflects?|lists?|includes?|holds?)\b|"
    r"\bclaim\s+(?:has\s+been|was|is)\s+(?:superseded|updated|stored|created|recorded)\b|"
    r"\bpersisted\s+the\s+(?:change|update|fact|value)\b|"
    r"\bi(?:'ll| will)\s+(?:remember|keep)\s+(?:the|this|that|your|it)\b|"
    r"\b(?:that|this|it)(?:'s|\s+is)\s+(?:now\s+)?(?:saved|stored|recorded|remembered|on\s+file)\b|"
    r"\bpart\s+of\s+my\s+(?:project\s+)?(?:knowledge|memory)\b|"
    r"\A\s*(?:got\s+it|ok(?:ay)?|done|sure|understood)[,.!\s-]*(?:stored|saved|recorded|remembered)[.!]?\s*\Z",
    re.I,
)


def _clean_value(value: str) -> str:
    text = " ".join(str(value).split())
    text = _TAIL_CLAUSE.sub("", text)
    text = _BARE_TAIL_CLAUSE.sub("", text)
    text = _TRAILING_PUNCTUATION.sub("", text).strip()
    text = _TRAILING_TEMPORAL.sub("", text).strip()
    text = _TRAILING_PUNCTUATION.sub("", text).strip()
    while len(text) >= 2 and text[0] in _QUOTE_CHARACTERS and text[-1] in _QUOTE_CHARACTERS:
        text = text[1:-1].strip()
    text = _LEADING_ARTICLE.sub("", text)
    text = _LEADING_TIME_PREPOSITION.sub("", text)
    return text.strip()


def _value_shape_ok(value: str) -> bool:
    """A fact value names something: it carries a digit, a capital, a
    structural character, or is short.  Long all-lowercase prose ("much
    cleaner than before") is commentary, not a value."""
    if not value:
        return False
    if _VALUE_SHAPE.search(value):
        return True
    return len(value.split()) <= 3


def _clean_subject(subject: str) -> str | None:
    text = " ".join(str(subject).split()).strip(" ,;:")
    tokens = text.split()
    while tokens and tokens[0].casefold() in _DETERMINERS:
        tokens = tokens[1:]
    if not tokens or len(tokens) > _MAX_SUBJECT_TOKENS:
        return None
    head = tokens[0].casefold()
    if head in _PRONOUN_SUBJECTS or head in _PERSONAL_RELATIONS or head in _RESERVED_WORDS:
        return None
    if tokens[-1].casefold() in _PERSONAL_RELATIONS and len(tokens) <= 2:
        return None
    if len(tokens) == 1 and head in _CONTROL_SUBJECTS:
        return None
    text = " ".join(tokens)
    if not any(character.isalpha() for character in text):
        return None
    return text


def _person_like(subject: str) -> bool:
    """One or two capitalised alphabetic tokens read as a person's name."""
    tokens = subject.split()
    if not tokens or len(tokens) > 2:
        return False
    return all(
        token[:1].isupper() and token[1:].isalpha() and token[1:] == token[1:].lower()
        for token in tokens
    )


def _clean_predicate(predicate: str) -> str | None:
    text = " ".join(str(predicate).casefold().split())
    text = re.sub(r"\A(?:is|are|was|were|has|have)\s+(?:now\s+|currently\s+)?", "", text)
    text = text.strip(" ,;:-")
    if not text or len(text) > 160:
        return None
    tokens = text.split()
    if all(token in _PREDICATE_STOPWORDS for token in tokens):
        return None
    if tokens[0] in _RESERVED_WORDS:
        return None
    return text


def _stem(token: str) -> str:
    token = token.casefold()
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _predicate_stems(predicate: str) -> set[str]:
    return {
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", predicate.casefold())
        if token not in _PREDICATE_STOPWORDS
    }


def _governed_command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _candidate(subject: str, predicate: str, value: str) -> dict[str, str] | None:
    cleaned_subject = _clean_subject(subject)
    cleaned_predicate = _clean_predicate(predicate)
    cleaned_value = _clean_value(value)
    if cleaned_subject is None or cleaned_predicate is None or not cleaned_value:
        return None
    if not _value_shape_ok(cleaned_value):
        return None
    if _QUESTION_START.match(cleaned_value) and cleaned_value.endswith("?"):
        return None
    key_text = f"{cleaned_subject} {cleaned_predicate}"
    if _SENSITIVE_PREDICATE.search(key_text) or _SENSITIVE_VALUE.search(cleaned_value):
        return None
    if _CONTROL_PREDICATE.search(cleaned_predicate) or _concatenated_reserved(key_text):
        return None
    if _INJECTION_SHAPE.search(_decamel(f"{key_text} {cleaned_value}")):
        return None
    if _person_like(cleaned_subject) and not _PERSON_WORK_PREDICATE.match(cleaned_predicate):
        return None
    command = _governed_command(cleaned_subject, cleaned_predicate, cleaned_value)
    try:
        parsed = parse_explicit_project_fact(command)
    except GovernedMemoryCommandError:
        return None
    if parsed is None:
        return None
    return {
        "subject": parsed["subject"],
        "predicate": parsed["predicate"],
        "value": parsed["value"],
    }


def _normalized_known(known_subjects: Sequence[str]) -> set[str]:
    known: set[str] = set()
    for subject in known_subjects:
        text = " ".join(str(subject or "").casefold().split())
        if text:
            known.add(text)
    return known


def _split_phrase(
    phrase: str, known: set[str], *, require_known: bool = False
) -> tuple[str, str] | None:
    """Split a bare noun phrase into (subject, predicate).

    Preference order: a stored subject that prefixes the phrase, a two-word
    attribute ending the phrase, then a one-word attribute noun.  Anything
    else has no deterministic split and yields ``None``.  With
    ``require_known`` only the stored-subject split counts.
    """
    tokens = " ".join(str(phrase).split()).split()
    while tokens and tokens[0].casefold() in _DETERMINERS:
        tokens = tokens[1:]
    count = len(tokens)
    if count < 2:
        return None
    folded = [token.casefold() for token in tokens]
    for split in range(count - 1, 0, -1):
        if " ".join(folded[:split]) in known:
            return " ".join(tokens[:split]), " ".join(tokens[split:])
    if require_known:
        return None
    if count >= 3 and " ".join(folded[-2:]) in _COMPOUND_ATTRIBUTES:
        return " ".join(tokens[:-2]), " ".join(tokens[-2:])
    if folded[-1] in _ATTRIBUTE_NOUNS:
        return " ".join(tokens[:-1]), tokens[-1]
    # No stored subject and no property noun: "our primary database is now
    # Postgres 16" has no deterministic split, so it yields no proposal rather
    # than a guess the operator would have to notice and repair.
    return None


def _relation_predicate(match: re.Match[str]) -> str:
    predicate = str(match.group("predicate"))
    unit = match.groupdict().get("unit")
    return f"{predicate} {unit}" if unit else predicate


_TECHNICAL_VALUE = re.compile(r"[0-9/:#@.]")


def _movement_is_technical(subject: str, value: str, known: set[str]) -> bool:
    """"X moved to Y" is a project fact when X is a stored subject or a named
    system, or Y is an address (port, URL, path, channel, host); "the party
    moved to Dave's place" is neither."""
    subject_key = " ".join(str(subject).casefold().split())
    tokens = subject_key.split()
    while tokens and tokens[0] in _DETERMINERS:
        tokens = tokens[1:]
    if " ".join(tokens) in known:
        return True
    if any(character.isdigit() or character.isupper() for character in str(subject)):
        return True
    return _TECHNICAL_VALUE.search(str(value)) is not None


def _arrow_candidate(body: str) -> dict[str, str] | None:
    """``S -> P -> V`` with plain phrases only, never code or markdown."""
    match = _ARROW_FORM.match(body)
    if match is None:
        return None
    subject = " ".join(match.group("subject").split())
    predicate = " ".join(match.group("predicate").split())
    if not _PLAIN_PHRASE.match(subject) or not _PLAIN_PHRASE.match(predicate):
        return None
    if not _TWO_LETTERS.search(subject) or not _TWO_LETTERS.search(predicate):
        return None
    if subject.casefold() == predicate.casefold():
        return None
    return _candidate(subject, predicate, match.group("value"))


def _clause_candidate(
    body: str,
    *,
    licensed: bool,
    known: set[str],
) -> dict[str, str] | None:
    """Try each closed form on one clause body, structured forms first."""
    candidate = _arrow_candidate(body)
    if candidate is not None:
        return candidate
    match = _RENAME_FORM.match(body)
    if match is not None:
        return _candidate(match.group("subject"), "name", match.group("value"))
    match = _POSSESSIVE_COLON_FORM.match(body)
    if match is not None:
        candidate = _candidate(
            match.group("subject"), match.group("predicate"), match.group("value")
        )
        if candidate is not None:
            return candidate
    match = _PHRASE_COLON_FORM.match(body)
    if match is not None and "://" not in match.group("phrase"):
        # "Dinner time: 7pm" is a note, not a fact.  A bare phrase before a
        # colon needs a cue or a stored subject to count.
        split = _split_phrase(match.group("phrase"), known, require_known=not licensed)
        if split is not None:
            candidate = _candidate(split[0], split[1], match.group("value"))
            if candidate is not None:
                return candidate
    if not licensed:
        return None
    match = _POSSESSIVE_FORM.match(body)
    if match is not None:
        candidate = _candidate(
            match.group("subject"), match.group("predicate"), match.group("value")
        )
        if candidate is not None:
            return candidate
    # A phrase that splits on a stored subject or a property noun before a
    # strong transition ("release candidate freeze date moved to ...") beats a
    # movement relation with the whole phrase as subject.
    match = _BARE_TRANSITION_FORM.match(body)
    if match is not None:
        split = _split_phrase(match.group("phrase"), known)
        if split is not None:
            candidate = _candidate(split[0], split[1], match.group("value"))
            if candidate is not None:
                return candidate
    match = _RELATIONAL_FORM.match(body)
    if match is not None:
        predicate = _relation_predicate(match)
        value = match.group("value")
        if not _MOVEMENT_RELATION.search(predicate) or _movement_is_technical(
            match.group("subject"), _clean_value(value), known
        ):
            candidate = _candidate(match.group("subject"), predicate, value)
            if candidate is not None:
                return candidate
    match = _COPULA_PREPOSITION_FORM.match(body)
    if match is not None:
        predicate = match.group("predicate").casefold()
        if predicate in _LOCATION_ATTRIBUTES or predicate in _ATTRIBUTE_NOUNS:
            candidate = _candidate(
                match.group("subject"), match.group("predicate"), match.group("value")
            )
            if candidate is not None:
                return candidate
    return None


def _copula_candidate(body: str, known: set[str]) -> dict[str, str] | None:
    """Plain "X Y is V" is only a fact when a cue licensed the sentence."""
    match = _BARE_COPULA_FORM.match(body)
    if match is None:
        return None
    split = _split_phrase(match.group("phrase"), known)
    if split is None:
        return None
    return _candidate(split[0], split[1], match.group("value"))


def _resolve_pronoun(body: str, last_subject: str | None) -> str:
    tokens = body.split(maxsplit=1)
    if not tokens or tokens[0].casefold() not in _RESOLVABLE_PRONOUNS:
        return body
    if last_subject is None:
        return body
    rest = tokens[1] if len(tokens) > 1 else ""
    return f"{last_subject} {rest}".strip()


def _licensed_body(sentence: str) -> tuple[str, bool, bool] | None:
    """Return ``(body, licensed, copula_licensed)`` for one sentence, or
    ``None`` when the sentence is a question, code, reported or uncertain
    speech, or otherwise not an operator assertion.

    ``licensed`` means an update cue or a statement cue is present, so the
    sentence may state a fact; ``copula_licensed`` means an explicit cue
    licenses even a plain "X is Y".  Shared by the grammar and by
    :func:`licensed_statements`, so the model-assisted proposer can only ever
    see sentences the grammar would also have considered.
    """
    text = " ".join(str(sentence).split())
    if not text or "{" in text or "}" in text or "```" in text or "`" in text:
        return None
    if text.endswith("?") or _QUESTION_START.match(text):
        return None
    text = _DISCOURSE_OPENER.sub("", text)
    statement_cue = _STATEMENT_CUE.match(text)
    body = text[statement_cue.end():] if statement_cue else text
    body = _DISCOURSE_OPENER.sub("", body.strip(" ,;:")).strip(" ,;:")
    leading_cue = _LEADING_UPDATE_CUE.match(body)
    if leading_cue is not None:
        body = body[leading_cue.end():].strip(" ,;:")
    if not body:
        return None
    if _REPORTED_OR_UNCERTAIN.search(body):
        return None
    sentence_update_cue = _UPDATE_CUE.search(body) is not None
    licensed = bool(statement_cue or leading_cue or sentence_update_cue)
    copula_licensed = bool(statement_cue or leading_cue)
    return body, licensed, copula_licensed


_POSSESSIVE_OPENERS = frozenset({"my", "our", "his", "her", "their", "your"})


def grounding_clauses(statement: str) -> list[str]:
    """The parts of a statement a value may be grounded in.

    Tail clauses that name what a value is *not* ("..., not Talon box",
    "instead of", "because Talon box died"), negated segments, and further
    ``;``-clauses are removed, so a model-proposed value cannot be taken from
    the alternative or the negation the operator ruled out.  Used at proposal
    time and at confirmation time alike.
    """
    text = " ".join(str(statement or "").split())
    clauses: list[str] = []
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip(" ,;:")
        if not clause:
            continue
        clause = _TAIL_CLAUSE.sub("", clause)
        clause = _BARE_TAIL_CLAUSE.sub("", clause)
        head = re.split(r"[,(]", clause, maxsplit=1)[0].strip()
        if not head or _NEGATION.search(head):
            continue
        clauses.append(head)
    return clauses


def licensed_statements(prompt: str) -> list[str]:
    """Sentence bodies of ``prompt`` that are licensed operator assertions but
    that the grammar could not turn into a proposal.

    A model-assisted proposer may look only at these: each carries an update
    or statement cue, is not a question, an imperative, a conditional, or
    reported speech, and contains no code.  Anything else is never shown to
    a model as a candidate fact.
    """
    text = str(prompt or "")
    if not text.strip() or len(text) > MAX_EXTRACTION_CHARS:
        return []
    bodies: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        parsed = _licensed_body(sentence)
        if parsed is None:
            continue
        body, licensed, _copula = parsed
        if not licensed:
            continue
        clauses = [clause.strip(" ,;:") for clause in _CLAUSE_SPLIT.split(body)]
        if any(
            _IMPERATIVE_START.match(clause) or _CONDITIONAL_START.match(clause)
            for clause in clauses
            if clause
        ):
            continue
        first = body.split(maxsplit=1)[0].casefold().strip(",.;:!")
        if (
            first in _PRONOUN_SUBJECTS
            or first in _PERSONAL_RELATIONS
            or first in _POSSESSIVE_OPENERS
            or first in {"i'm", "i've", "i'll", "we're", "we've", "we'll", "let's"}
        ):
            # A pronoun, possessive, or personal-relation subject can never
            # become a project fact; do not spend a model call on it.
            continue
        if _sentence_candidate(sentence, set()) is not None:
            continue
        bodies.append(body)
    return bodies


def _sentence_candidate(sentence: str, known: set[str]) -> dict[str, str] | None:
    parsed = _licensed_body(sentence)
    if parsed is None:
        return None
    body, licensed, copula_licensed = parsed
    last_subject: str | None = None
    for clause in _CLAUSE_SPLIT.split(body):
        clause = clause.strip(" ,;:")
        if not clause:
            continue
        if _IMPERATIVE_START.match(clause) or _CONDITIONAL_START.match(clause):
            return None
        clause = _resolve_pronoun(clause, last_subject)
        negated = _NEGATED_SUBJECT.match(clause)
        if negated is not None:
            last_subject = _clean_subject(negated.group("subject")) or last_subject
            continue
        head = re.split(r"[,(]", clause, maxsplit=1)[0]
        if _NEGATION.search(head):
            continue
        candidate = _clause_candidate(clause, licensed=licensed, known=known)
        if candidate is None and copula_licensed:
            candidate = _copula_candidate(clause, known)
        if candidate is not None:
            return candidate
        subject_match = _NOUN_PHRASE_START.match(clause)
        if subject_match is not None:
            last_subject = _clean_subject(subject_match.group("subject")) or last_subject
    return None


def extract_project_fact(
    prompt: str,
    known_subjects: Sequence[str] = (),
) -> dict[str, str] | None:
    """Return a governed-parser-validated subject/predicate/value or ``None``.

    Only declarative statements carrying an update cue ("now", "changed to",
    "instead of", ...) or an explicit statement cue ("note that", "for the
    record", ...) are considered, plus explicitly structured forms (``S -> P
    -> V`` and ``S's P: V``).  ``known_subjects`` are subjects already stored
    for the caller's scope; when one prefixes a bare noun phrase the split
    follows it so the proposal updates that subject instead of forking a new
    spelling.  Questions, imperatives, reported, hedged or hypothetical
    speech, conditionals, pronoun subjects, special-category personal data,
    control-plane subjects, JSON, code, and anything the governed parser would
    reject yield ``None``.
    """
    text = str(prompt or "")
    if not text.strip() or len(text) > MAX_EXTRACTION_CHARS:
        return None
    known = _normalized_known(known_subjects)
    for sentence in _SENTENCE_SPLIT.split(text):
        candidate = _sentence_candidate(sentence, known)
        if candidate is not None:
            return candidate
    return None


def adopt_stored_predicate(
    proposal: Mapping[str, str],
    stored_claims: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Align a proposal with an existing claim so a paste updates, not forks.

    ``stored_claims`` are active claims already visible to the caller (for
    example ``Memory.current_claims`` output).  When one shares the proposal's
    normalized subject and its predicate overlaps on a meaningful stem, the
    stored subject spelling and predicate are adopted so the governed write
    supersedes that claim instead of creating a sibling key.
    """
    subject_key = " ".join(str(proposal["subject"]).casefold().split())
    proposal_stems = _predicate_stems(str(proposal["predicate"]))
    best: tuple[int, str, Mapping[str, Any]] | None = None
    for claim in stored_claims:
        try:
            stored_subject = str(claim["subject"])
            stored_predicate = str(claim["predicate"])
        except (KeyError, TypeError):
            continue
        if " ".join(stored_subject.casefold().split()) != subject_key:
            continue
        overlap = len(proposal_stems & _predicate_stems(stored_predicate))
        if overlap == 0:
            continue
        updated_at = str(claim.get("updated_at") or "")
        key = (overlap, updated_at, claim)
        if best is None or key[:2] > best[:2]:
            best = key
    if best is None:
        return dict(proposal)
    _overlap, _updated, claim = best
    adopted = _candidate(str(claim["subject"]), str(claim["predicate"]), str(proposal["value"]))
    return adopted if adopted is not None else dict(proposal)


def validate_proposal(subject: str, predicate: str, value: str) -> dict[str, str] | None:
    """Clean and parser-validate one triple exactly as the grammar would.

    Used by the model-assisted proposer so that nothing a model returns can
    reach the operator unless the deterministic path would accept it.
    """
    return _candidate(subject, predicate, value)


def predicate_stems(predicate: str) -> set[str]:
    """Meaningful, lightly stemmed words of a predicate or sentence."""
    return _predicate_stems(str(predicate or ""))


def proposal_command(proposal: Mapping[str, str]) -> str:
    return _governed_command(
        str(proposal["subject"]), str(proposal["predicate"]), str(proposal["value"])
    )


_NEGATED_WRITE_CLAIM = re.compile(
    r"\b(?:no|not|nothing|never|none|isn't|aren't|wasn't|weren't|hasn't|"
    r"haven't|don't|doesn't|didn't|cannot|can't|won't|without|neither|nor|"
    r"unable\s+to)\b",
    re.I,
)


def claims_memory_write(text: str) -> bool:
    """True when assistant text asserts that memory was durably written.

    A negated statement ("No fact is recorded for the Osprey relay", "that is
    not stored") is an honest abstention, not a write claim, so a match with a
    negation word in the two dozen characters before it does not count.
    """
    content = str(text or "")
    for match in _MEMORY_WRITE_CLAIM.finditer(content):
        prefix = content[max(0, match.start() - 24):match.start()]
        if _NEGATED_WRITE_CLAIM.search(prefix):
            continue
        return True
    return False
