from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .moltbook_adapter import MoltbookAdapter, OfflineMoltbookAdapter, public_value
from .public_presence_service import PublicPresenceService


_FORBIDDEN_NAME_TOKENS = frozenset({
    "publish",
    "follow",
    "like",
    "vote",
    "message",
    "delete",
    "shell",
    "command",
    "file",
    "browser",
    "computer",
    "credential",
    "trade",
    "wallet",
    "purchase",
    "deploy",
    "send",
    "external",
})

PUBLIC_TOOL_NAMES = (
    "public_presence_status",
    "public_presence_health",
    "moltbook_status",
    "moltbook_read_feed",
    "moltbook_read_thread",
    "moltbook_search",
    "moltbook_get_profile",
    "moltbook_draft_post",
    "moltbook_draft_reply",
)


@dataclass(frozen=True)
class PublicToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _copy_json_value(self.parameters),
            },
        }


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(child) for child in value]
    return value


def _object_schema(
    properties: Mapping[str, Any] | None = None,
    *,
    required: tuple[str, ...] = (),
) -> Mapping[str, Any]:
    return MappingProxyType({
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    })


_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50}
_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_DRAFT = {"type": "string", "minLength": 1, "maxLength": 4_000}


_SPECS = (
    PublicToolSpec(
        "public_presence_status",
        "Read the isolated Public Presence lifecycle and control state.",
        _object_schema(),
    ),
    PublicToolSpec(
        "public_presence_health",
        "Read the isolated Public Presence readiness summary.",
        _object_schema(),
    ),
    PublicToolSpec(
        "moltbook_status",
        "Read the disconnected Moltbook adapter state.",
        _object_schema(),
    ),
    PublicToolSpec(
        "moltbook_read_feed",
        "Read bounded untrusted records from an offline Moltbook fixture.",
        _object_schema({"limit": _LIMIT}),
    ),
    PublicToolSpec(
        "moltbook_read_thread",
        "Read one bounded untrusted thread from an offline Moltbook fixture.",
        _object_schema({"thread_id": _ID}, required=("thread_id",)),
    ),
    PublicToolSpec(
        "moltbook_search",
        "Search bounded untrusted records in an offline Moltbook fixture.",
        _object_schema(
            {"query": {"type": "string", "minLength": 1, "maxLength": 500}, "limit": _LIMIT},
            required=("query",),
        ),
    ),
    PublicToolSpec(
        "moltbook_get_profile",
        "Read one bounded untrusted profile from an offline Moltbook fixture.",
        _object_schema({"profile_id": _ID}, required=("profile_id",)),
    ),
    PublicToolSpec(
        "moltbook_draft_post",
        "Create a local, unapproved, non-deliverable public-content draft.",
        _object_schema({"body": _DRAFT}, required=("body",)),
    ),
    PublicToolSpec(
        "moltbook_draft_reply",
        "Create a local, unapproved, non-deliverable reply draft.",
        _object_schema({"thread_id": _ID, "body": _DRAFT}, required=("thread_id", "body")),
    ),
)


def _validate_specs(specs: tuple[PublicToolSpec, ...]) -> None:
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise RuntimeError("public tool names must be unique")
    if tuple(names) != PUBLIC_TOOL_NAMES:
        raise RuntimeError("public tool registry must match its closed allowlist exactly")
    for name in names:
        lowered = name.casefold()
        if set(lowered.split("_")) & _FORBIDDEN_NAME_TOKENS:
            raise RuntimeError(f"forbidden Public Presence tool name: {name}")


_validate_specs(_SPECS)


class PublicToolRegistry:
    """Closed offline registry. Arbitrary tools cannot be registered at runtime."""

    def __init__(self, service: PublicPresenceService, adapter: MoltbookAdapter) -> None:
        if type(adapter) is not OfflineMoltbookAdapter:
            raise PermissionError("the foundation registry accepts the sealed offline adapter only")
        self._service = service
        self._adapter = adapter
        self._specs = MappingProxyType({spec.name: spec for spec in _SPECS})

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @property
    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(spec.schema() for spec in self._specs.values())

    @staticmethod
    def _arguments(
        arguments: Mapping[str, Any] | None,
        *,
        allowed: frozenset[str],
        required: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        if arguments is None:
            values: dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            values = dict(arguments)
        else:
            raise ValueError("public tool arguments must be an object")
        supplied = set(values)
        if supplied - allowed:
            raise ValueError("public tool arguments contain unsupported fields")
        if required - supplied:
            raise ValueError("public tool arguments are missing required fields")
        return values

    def execute(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        if name not in self._specs:
            raise KeyError(f"unknown public tool: {name}")
        if name == "public_presence_status":
            self._arguments(arguments, allowed=frozenset())
            return self._service.status()
        if name == "public_presence_health":
            self._arguments(arguments, allowed=frozenset())
            return self._service.health()
        if name == "moltbook_status":
            self._arguments(arguments, allowed=frozenset())
            return dict(self._adapter.status())

        required_mode = "suggest" if name in {
            "moltbook_draft_post", "moltbook_draft_reply"
        } else "observe"
        self._service.require_operational(required_mode=required_mode)
        if name == "moltbook_read_feed":
            values = self._arguments(arguments, allowed=frozenset({"limit"}))
            return public_value(tuple(self._adapter.read_feed(limit=values.get("limit", 20))))
        if name == "moltbook_read_thread":
            values = self._arguments(
                arguments,
                allowed=frozenset({"thread_id"}),
                required=frozenset({"thread_id"}),
            )
            return public_value(tuple(self._adapter.read_thread(values["thread_id"])))
        if name == "moltbook_search":
            values = self._arguments(
                arguments,
                allowed=frozenset({"query", "limit"}),
                required=frozenset({"query"}),
            )
            return public_value(tuple(self._adapter.search(
                values["query"], limit=values.get("limit", 20)
            )))
        if name == "moltbook_get_profile":
            values = self._arguments(
                arguments,
                allowed=frozenset({"profile_id"}),
                required=frozenset({"profile_id"}),
            )
            return public_value(self._adapter.get_profile(values["profile_id"]))
        if name == "moltbook_draft_post":
            values = self._arguments(
                arguments,
                allowed=frozenset({"body"}),
                required=frozenset({"body"}),
            )
            return public_value(self._adapter.draft_post(values["body"]))
        if name == "moltbook_draft_reply":
            values = self._arguments(
                arguments,
                allowed=frozenset({"thread_id", "body"}),
                required=frozenset({"thread_id", "body"}),
            )
            return public_value(self._adapter.draft_reply(
                values["thread_id"], values["body"]
            ))
        raise AssertionError("closed public tool dispatch is incomplete")
