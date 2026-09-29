from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config
from .security_expertise import classify_security_expertise


CODING_ACTION_PATTERNS = (
    r"\b(?:build|implement|debug|fix|refactor|compile|deploy|develop|edit|modify|patch|replace|remove|delete|rename)\b.{0,100}"
    r"(?:\b(?:app|application|api|site|website|software|code|tests?|bugs?|functions?|methods?|classes?|regex(?:es)?|regular expressions?|queries?|migrations?|project|files?|repo(?:sitory)?|script|module|package|library|program|database|python|javascript|typescript|react|node|rust|golang|java|swift|kotlin|sql|html|css)\b|"
    r"\b[\w.-]+\.(?:py|js|jsx|ts|tsx|java|rs|go|cs|cpp|c|h|html|css|json|toml|yaml|yml|md)\b)",
    r"\b(?:create|add|change|update|write|make)\b.{0,100}"
    r"(?:\b(?:app|application|api|website|software|source code|unit tests?|integration tests?|functions?|methods?|classes?|regex(?:es)?|regular expressions?|queries?|migrations?|project files?|repository|script|module|package|program|database schema|python|javascript|typescript|react|node|rust|golang|java|swift|kotlin|sql|html|css)\b|"
    r"\b[\w.-]+\.(?:py|js|jsx|ts|tsx|java|rs|go|cs|cpp|c|h|html|css|json|toml|yaml|yml)\b)",
)

DEEP_REASONING_PATTERNS = (
    r"\b(?:deep|thorough|rigorous|detailed)\s+(?:analysis|reasoning|investigation|evaluation|comparison|review|dive)\b",
    r"\b(?:analy[sz]e|reason|think|investigate|evaluate)\s+(?:very\s+)?(?:deeply|carefully|thoroughly|rigorously)\b",
    r"\b(?:think hard|reason step[- ]by[- ]step|use (?:the )?reasoning (?:model|profile)|deep dive)\b",
    r"\b(?:root cause|prove|derive|weighted (?:average|percentage)|least common multiple|lcm|algorithmic reasoning|counterexample|edge cases?|threat model|formal verification)\b",
    r"\b(?:strict|deterministic)\s+(?:rubric|evaluation|reasoning)\b",
)

RESEARCH_PATTERNS = (
    r"\b(?:deep|thorough|rigorous|comprehensive|exhaustive)\s+research\b",
    r"\b(?:research|investigate|compare)\b.{0,120}\b(?:current|latest|web|internet|sources?|citations?)\b",
    r"\b(?:primary|authoritative)\s+sources?\b",
    r"\bcross[- ]check\b.{0,80}\bsources?\b",
)
EVALUATION_PATTERNS = (
    r"\bstrict deterministic rubric\b",
    r"\bdo not use tools\b",
    r"\breturn only (?:one )?(?:valid )?json\b",
)

FAST_PATTERNS = (
    r"^(hi|hello|hey|thanks|thank you|good morning|good evening)[.! ]*$",
    r"\b(summarize|rewrite|translate|brainstorm|explain simply)\b",
)

_LIGHTWEIGHT_CODE_UNIT = re.compile(
    r"\b(?:function|method|class|unit\s+tests?|test\s+cases?|regex|"
    r"regular\s+expression|query|snippet)\b",
    re.I,
)
_BROAD_CODE_SCOPE = re.compile(
    r"\b(?:app|application|api|site|website|service|system|architecture|"
    r"repository|repo|project|package|library|database|schema|migration|"
    r"deployment|deploy|integration|end[- ]to[- ]end|multi[- ]file|"
    r"multiple\s+files?)\b",
    re.I,
)
_CODE_FILE_TARGET = re.compile(
    r"\b[\w.-]+\.(?:py|js|jsx|ts|tsx|java|rs|go|cs|cpp|c|h|html|css|"
    r"json|toml|yaml|yml)\b",
    re.I,
)

_NEGATED_CODING_CLAUSE = re.compile(
    r"\b(?:do\s+not|don['’]t|never|without)\s+"
    r"(?:(?:create|add|change|update|write|make|build|implement|debug|fix|refactor|"
    r"compile|deploy|develop|edit|modify|patch|replace|remove|delete|rename)(?:ing)?\b"
    r"(?:\s+(?:or|and)\s+)?)+[^.?!;\n]*",
    re.I,
)


def coding_intent_text(prompt: str) -> str:
    """Remove explicit negative constraints before classifying coding intent."""
    return _NEGATED_CODING_CLAUSE.sub("", prompt)


def lightweight_coding_intent(prompt: str) -> bool:
    """Identify bounded code units that can start on the fast profile.

    This deliberately uses scope rather than named products or memorized prompt
    phrases.  Broad artifacts, long specifications, and multi-file targets stay
    on the full coding profile.  A lightweight route retains normal coding
    tools and verification, and Agent escalation promotes it after repeated
    tool failures.
    """

    text = re.sub(r"\s+", " ", coding_intent_text(prompt)).strip()
    if not text or len(text.split()) > 48:
        return False
    if _LIGHTWEIGHT_CODE_UNIT.search(text) is None:
        return False
    if _BROAD_CODE_SCOPE.search(text) is not None:
        return False
    return len(_CODE_FILE_TARGET.findall(text)) <= 1


class RoutedModelReference(str):
    """A wire-compatible model name carrying deterministic routing metadata.

    Provider clients still receive an ordinary string value.  The private
    attribute exists only long enough for local-only request preparation and
    is never encoded into a provider request.
    """

    def __new__(
        cls,
        value: str,
        profile: str,
        coding_intent: bool,
    ) -> RoutedModelReference:
        instance = str.__new__(cls, value)
        instance.jarvis_route_profile = str(profile)
        instance.jarvis_coding_intent = bool(coding_intent)
        return instance


@dataclass(frozen=True)
class Route:
    profile: str
    model: str
    reason: str
    fallback: bool = False
    coding_intent: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        value = str(self.model)
        coding_intent = bool(self.coding_intent or self.profile == "coding")
        object.__setattr__(self, "coding_intent", coding_intent)
        prefix, separator, _remainder = value.partition(":")
        if separator and prefix.casefold() in {
            "openai", "xai", "anthropic", "codex-cli", "claude-cli"
        }:
            return
        object.__setattr__(
            self,
            "model",
            RoutedModelReference(value, self.profile, coding_intent),
        )


class ModelRouter:
    PROFILES = {"fast", "reasoning", "coding", "deep"}

    def __init__(self, config: Config, available_models: list[str]) -> None:
        self.config = config
        self.available_models = available_models

    def _configured(self, profile: str) -> str:
        return {
            "fast": self.config.fast_model,
            "reasoning": self.config.reasoning_model,
            "coding": self.config.coding_model,
            "deep": self.config.deep_model,
        }[profile]

    def _installed_name(self, wanted: str) -> str | None:
        if wanted in self.available_models:
            return wanted
        prefix, separator, provider_model = wanted.partition(":")
        if separator and prefix.casefold() in {
            "openai", "xai", "anthropic", "codex-cli", "claude-cli", "openrouter"
        } and provider_model:
            if any(
                item.startswith(f"{prefix.casefold()}:")
                for item in self.available_models
            ):
                return wanted
        if separator and prefix.casefold() == "ollama" and provider_model:
            if provider_model in self.available_models:
                return wanted
        if ":" not in wanted:
            for name in self.available_models:
                if name.split(":", 1)[0] == wanted:
                    return name
        return None

    @staticmethod
    def _same_model(first: str, second: str) -> bool:
        def canonical(value: str) -> tuple[str, str]:
            prefix, separator, remainder = value.partition(":")
            if separator and prefix.casefold() in {
                "openai", "xai", "anthropic", "codex-cli", "claude-cli", "openrouter",
                "ollama",
            }:
                return prefix.casefold(), remainder
            return "ollama", value
        return canonical(first) == canonical(second)

    def update_models(self, available_models: list[str]) -> None:
        self.available_models = list(dict.fromkeys(available_models))

    def _fallback_profiles(self, profile: str) -> tuple[str, ...]:
        return {
            "deep": ("coding", "reasoning", "fast"),
            "coding": ("reasoning", "fast"),
            "reasoning": ("coding", "fast"),
            "fast": ("reasoning", "coding"),
        }.get(profile, ("reasoning", "coding", "fast"))

    @staticmethod
    def _vision_capable(model: str) -> bool:
        provider, separator, provider_model = str(model).partition(":")
        if not separator:
            return False
        provider = provider.casefold()
        value = provider_model.casefold()
        if provider == "codex-cli":
            return bool(value)
        if provider == "openai":
            return bool(re.match(r"(?:gpt-5(?:\.|-|$)|gpt-4o(?:-|$)|gpt-4\.1(?:-|$))", value))
        if provider == "xai":
            return value.startswith("grok-")
        if provider in {"anthropic", "claude-cli"}:
            return value.startswith("claude-")
        if provider == "openrouter":
            # OpenRouter's catalogue says per model whether it accepts images.
            from .openrouter import model_info

            info = model_info(provider_model)
            return bool(info and info.get("vision"))
        return False

    def is_vision_capable(self, model: str) -> bool:
        return self._vision_capable(model)

    def _vision_route(self, override: str | None) -> Route:
        requested = str(override or self.config.model or "auto").strip()
        choice = requested.casefold()
        profile_order: list[str] = []
        if choice in self.PROFILES:
            profile_order.append(choice)
        elif choice != "auto" and self._vision_capable(requested):
            installed = self._installed_name(requested)
            if installed is not None:
                return Route("custom", installed, "manual vision-capable model")
        profile_order.extend(("fast", "reasoning", "deep", "coding"))
        for profile in dict.fromkeys(profile_order):
            wanted = self._configured(profile)
            installed = self._installed_name(wanted)
            if installed is not None and self._vision_capable(installed):
                return Route(
                    profile,
                    installed,
                    "image input requires a vision-capable cloud model",
                    choice not in {"auto", profile},
                )
        for model in self.available_models:
            if self._vision_capable(model):
                return Route(
                    "custom",
                    model,
                    "image input requires a vision-capable cloud model",
                    True,
                )
        raise ValueError("image input requires a configured vision model")

    def _with_fallback(
        self,
        profile: str,
        reason: str,
        *,
        coding_intent: bool = False,
    ) -> Route:
        coding_intent = bool(coding_intent or profile == "coding")
        wanted = self._configured(profile)
        installed = self._installed_name(wanted)
        if installed:
            return Route(profile, installed, reason, coding_intent=coding_intent)

        for fallback_profile in self._fallback_profiles(profile):
            installed = self._installed_name(self._configured(fallback_profile))
            if installed:
                return Route(
                    fallback_profile,
                    installed,
                    f"{reason}; {wanted} is not installed, using {installed}",
                    True,
                    coding_intent,
                )
        if self.available_models:
            model = self.available_models[0]
            return Route(
                "custom", model, f"No configured models installed; using {model}",
                True, coding_intent,
            )
        return Route(profile, wanted, reason, True, coding_intent)

    def failover(self, current: Route, reason: str) -> Route:
        candidates = self.failover_candidates(current, reason)
        return candidates[0] if candidates else current

    def failover_candidates(self, current: Route, reason: str) -> list[Route]:
        """Return every distinct ready fallback in deterministic preference order."""
        candidates: list[Route] = []

        def append(profile: str, model: str) -> None:
            if self._same_model(model, current.model) or any(
                self._same_model(model, item.model) for item in candidates
            ):
                return
            candidates.append(Route(
                profile,
                model,
                f"{reason}; failing over from {current.model}",
                True,
                current.coding_intent,
            ))

        for profile in self._fallback_profiles(current.profile):
            installed = self._installed_name(self._configured(profile))
            if installed:
                append(profile, installed)
        for model in self.available_models:
            append("custom", model)
        return candidates

    def select(
        self,
        prompt: str,
        override: str | None = None,
        *,
        requires_vision: bool = False,
    ) -> Route:
        if requires_vision:
            return self._vision_route(override)
        requested = override or self.config.model or "auto"
        choice = requested.strip().lower()
        if choice != "auto":
            if choice in self.PROFILES:
                return self._with_fallback(choice, f"manual {choice} profile")
            installed = self._installed_name(requested)
            if installed is None:
                raise ValueError(f"Requested Ollama model is not installed: {requested}")
            return Route("custom", installed, "manual model")

        text = coding_intent_text(prompt).lower()
        coding_score = sum(
            bool(re.search(pattern, text, re.I | re.S))
            for pattern in CODING_ACTION_PATTERNS
        )
        reasoning_score = sum(
            bool(re.search(pattern, text, re.I | re.S))
            for pattern in DEEP_REASONING_PATTERNS
        )
        evaluation_score = sum(
            bool(re.search(pattern, text, re.I | re.S))
            for pattern in EVALUATION_PATTERNS
        )
        research_score = sum(
            bool(re.search(pattern, text, re.I | re.S))
            for pattern in RESEARCH_PATTERNS
        )
        specialist = classify_security_expertise(prompt)
        fast_score = sum(bool(re.search(pattern, text, re.I | re.S)) for pattern in FAST_PATTERNS)

        if evaluation_score >= 2:
            return self._with_fallback("reasoning", f"deterministic evaluation (score {evaluation_score})")
        if coding_score >= 1:
            if lightweight_coding_intent(text):
                return self._with_fallback(
                    "fast",
                    "bounded coding unit; automatic coding escalation remains available",
                    coding_intent=True,
                )
            return self._with_fallback("coding", f"coding task (score {coding_score})")
        if research_score >= 1:
            return self._with_fallback("reasoning", f"research task (score {research_score})")
        if specialist.active:
            return self._with_fallback(
                "deep",
                f"{specialist.label} specialist task",
            )
        if reasoning_score >= 1:
            return self._with_fallback("reasoning", f"explicit deep reasoning (score {reasoning_score})")
        reason = "simple task" if fast_score else "quick/general task"
        return self._with_fallback("fast", reason)

    def escalate(self, current: Route, prompt: str) -> Route:
        coding = any(
            re.search(pattern, coding_intent_text(prompt), re.I | re.S)
            for pattern in CODING_ACTION_PATTERNS
        )
        if current.profile == "fast":
            target = "coding" if coding else "reasoning"
            return self._with_fallback(target, f"escalated after repeated tool failures on {current.model}")
        if current.profile == "reasoning" and coding:
            return self._with_fallback("coding", f"escalated coding work after repeated failures on {current.model}")
        return current
