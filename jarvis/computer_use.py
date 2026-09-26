"""Fail-closed contracts for future, operator-scoped computer use.

This module deliberately contains no native desktop adapter.  It can only drive an
adapter explicitly injected by a trusted host, which makes the default build unable
to observe a screen or send input.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol, Sequence


class BrokerState(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    AUTHORIZED = "AUTHORIZED"


class ActionType(str, Enum):
    MOVE_POINTER = "MOVE_POINTER"
    CLICK = "CLICK"
    TYPE_TEXT = "TYPE_TEXT"
    PRESS_KEY = "PRESS_KEY"


class ObservationType(str, Enum):
    WINDOW_METADATA = "WINDOW_METADATA"
    SCREENSHOT = "SCREENSHOT"
    OCR_TEXT = "OCR_TEXT"
    APPLICATION_TEXT = "APPLICATION_TEXT"


class ObservationRelease(str, Enum):
    METADATA_ONLY = "METADATA_ONLY"
    LOCAL_PROCESSING = "LOCAL_PROCESSING"


class ActionOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ComputerUseError(RuntimeError):
    """Base error with a stable, non-sensitive public reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ComputerUseDenied(ComputerUseError):
    pass


class ComputerUseStopped(ComputerUseError):
    pass


class AmbiguousAction(ComputerUseError):
    pass


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        return self.width > 0 and self.height > 0 and (
            self.left <= x < self.left + self.width
            and self.top <= y < self.top + self.height
        )


@dataclass(frozen=True)
class WindowTarget:
    application_id: str
    window_id: str
    isolated_session_id: str
    bounds: Bounds
    generation: str

    def same_surface(self, other: "WindowTarget") -> bool:
        return self == other


@dataclass(frozen=True)
class OperatorAuthorization:
    """Host-created proof; free-form model or screen text is never accepted here."""

    operator_id: str
    authorization_id: str
    issued_at: float
    trusted_host_assertion: bool


@dataclass(frozen=True)
class ComputerUseGrant:
    grant_id: str
    actor_id: str
    application_id: str
    window_id: str
    isolated_session_id: str
    actions: frozenset[ActionType]
    observations: frozenset[ObservationType]
    resource_scopes: frozenset[str]
    expires_at: float
    issued_by: str


@dataclass(frozen=True)
class Observation:
    observation_id: str
    kind: ObservationType
    target: WindowTarget
    captured_at: float
    payload: bytes | str | dict[str, object]
    untrusted: bool = True


@dataclass(frozen=True)
class ComputerAction:
    action_id: str
    kind: ActionType
    target: WindowTarget
    resource_scope: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    observation_id: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    outcome: ActionOutcome
    detail_code: str


class ComputerUseAdapter(Protocol):
    """Native adapters must implement this contract outside the public foundation."""

    def current_target(self, window_id: str) -> WindowTarget | None: ...

    def observe(self, target: WindowTarget, kind: ObservationType) -> Observation: ...

    def perform(self, action: ComputerAction) -> AdapterResult: ...


@dataclass(frozen=True)
class AuditEntry:
    event: str
    actor_id: str
    grant_id: str
    target_label: str
    action_type: str
    outcome: str
    timestamp: float


@dataclass
class EmergencyStop:
    _event: threading.Event = field(default_factory=threading.Event)

    def trigger(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


_SENSITIVE_RESOURCE_PREFIXES = (
    "credential",
    "secret",
    "wallet",
    "transaction",
    "purchase",
    "payment",
    "publish",
    "key-entry",
)


class ComputerUseBroker:
    """Validate authority and target identity immediately around every adapter call."""

    def __init__(
        self,
        adapter: ComputerUseAdapter | None = None,
        *,
        enabled: bool = False,
        clock: Callable[[], float] = time.time,
        max_actions: int = 32,
        observation_ttl_seconds: float = 5.0,
    ) -> None:
        if max_actions < 1 or max_actions > 256:
            raise ValueError("max_actions must be between 1 and 256")
        if observation_ttl_seconds <= 0 or observation_ttl_seconds > 60:
            raise ValueError("observation_ttl_seconds must be between 0 and 60")
        self._adapter = adapter
        self._enabled = enabled
        self._clock = clock
        self._max_actions = max_actions
        self._observation_ttl_seconds = observation_ttl_seconds
        self._grants: dict[str, ComputerUseGrant] = {}
        self._revoked: set[str] = set()
        self._observations: dict[str, Observation] = {}
        self._audit: list[AuditEntry] = []
        self.stop = EmergencyStop()

    @property
    def state(self) -> BrokerState:
        if self._adapter is None:
            return BrokerState.UNAVAILABLE
        if not self._enabled or self.stop.is_set():
            return BrokerState.DISABLED
        return BrokerState.AUTHORIZED if self._active_grants() else BrokerState.DISABLED

    def status(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "native_adapter_included": False,
            "active_grants": len(self._active_grants()),
            "emergency_stop": self.stop.is_set(),
            "detail": "No native adapter is included." if self._adapter is None else "Injected adapter only.",
        }

    def add_grant(
        self, grant: ComputerUseGrant, authorization: OperatorAuthorization
    ) -> None:
        now = self._clock()
        if not self._enabled or self._adapter is None:
            raise ComputerUseDenied("computer use is unavailable or disabled")
        if not authorization.trusted_host_assertion:
            raise ComputerUseDenied("trusted operator authorization is required")
        if authorization.operator_id != grant.issued_by:
            raise ComputerUseDenied("grant issuer does not match operator authorization")
        if not authorization.authorization_id or authorization.issued_at > now:
            raise ComputerUseDenied("operator authorization is invalid")
        if grant.expires_at <= now:
            raise ComputerUseDenied("grant is expired")
        if not all((grant.grant_id, grant.actor_id, grant.application_id, grant.window_id,
                    grant.isolated_session_id)):
            raise ComputerUseDenied("grant scope is incomplete")
        if not grant.actions or not grant.resource_scopes:
            raise ComputerUseDenied("grant contains no usable action scope")
        if any(self._sensitive(scope) for scope in grant.resource_scopes):
            raise ComputerUseDenied("sensitive operations require a separate capability")
        self._grants[grant.grant_id] = grant

    def revoke(self, grant_id: str) -> None:
        self._revoked.add(grant_id)

    def observe(
        self,
        *,
        actor_id: str,
        grant_id: str,
        target: WindowTarget,
        kind: ObservationType,
        release: ObservationRelease,
        cancel_event: threading.Event | None = None,
    ) -> Observation:
        grant = self._require_grant(actor_id, grant_id, cancel_event)
        self._require_target(grant, target)
        if kind not in grant.observations:
            raise ComputerUseDenied("observation type is outside the grant")
        if kind is not ObservationType.WINDOW_METADATA and release is not ObservationRelease.LOCAL_PROCESSING:
            raise ComputerUseDenied("screen content may only be released for local processing")
        current = self._validated_current_target(target)
        self._check_interrupt(grant_id, cancel_event)
        observation = self._adapter_required().observe(current, kind)
        if observation.target != current or observation.kind is not kind:
            raise ComputerUseDenied("adapter returned an observation for a different target")
        self._observations[observation.observation_id] = observation
        self._record("OBSERVE", grant, target, kind.value, "CAPTURED")
        return observation

    def execute(
        self,
        *,
        actor_id: str,
        grant_id: str,
        actions: Sequence[ComputerAction],
        cancel_event: threading.Event | None = None,
    ) -> tuple[AdapterResult, ...]:
        if not actions or len(actions) > self._max_actions:
            raise ComputerUseDenied("action batch is empty or exceeds the bound")
        results: list[AdapterResult] = []
        for action in actions:
            grant = self._require_grant(actor_id, grant_id, cancel_event)
            self._validate_action(grant, action)
            current = self._validated_current_target(action.target)
            self._check_interrupt(grant_id, cancel_event)
            # Re-read immediately before input, closing the observation/action race.
            if not current.same_surface(self._validated_current_target(action.target)):
                raise ComputerUseDenied("target changed immediately before action")
            result = self._adapter_required().perform(action)
            self._record("ACTION", grant, action.target, action.kind.value, result.outcome.value)
            results.append(result)
            if result.outcome is ActionOutcome.UNKNOWN:
                raise AmbiguousAction("action outcome is unknown; automatic replay is forbidden")
            if result.outcome is not ActionOutcome.CONFIRMED:
                raise ComputerUseDenied("adapter rejected the action")
        return tuple(results)

    def audit_log(self) -> tuple[AuditEntry, ...]:
        return tuple(self._audit)

    def _validate_action(self, grant: ComputerUseGrant, action: ComputerAction) -> None:
        self._require_target(grant, action.target)
        if action.kind not in grant.actions:
            raise ComputerUseDenied("action type is outside the grant")
        if action.resource_scope not in grant.resource_scopes:
            raise ComputerUseDenied("resource is outside the grant")
        if self._sensitive(action.resource_scope):
            raise ComputerUseDenied("sensitive operations require a separate capability")
        if action.kind in {ActionType.CLICK, ActionType.MOVE_POINTER}:
            if action.x is None or action.y is None or not action.target.bounds.contains(action.x, action.y):
                raise ComputerUseDenied("pointer coordinates are outside the current target")
            if not action.observation_id:
                raise ComputerUseDenied("pointer actions require a fresh bound observation")
            observed = self._observations.get(action.observation_id)
            if observed is None or not observed.target.same_surface(action.target):
                raise ComputerUseDenied("pointer observation is missing or stale")
            age = self._clock() - observed.captured_at
            if age < 0 or age > self._observation_ttl_seconds:
                raise ComputerUseDenied("pointer observation is missing or stale")
        if action.kind is ActionType.TYPE_TEXT and action.text is None:
            raise ComputerUseDenied("text action is incomplete")
        if action.kind is ActionType.PRESS_KEY and not action.key:
            raise ComputerUseDenied("key action is incomplete")

    def _require_grant(
        self, actor_id: str, grant_id: str, cancel_event: threading.Event | None
    ) -> ComputerUseGrant:
        self._check_interrupt(grant_id, cancel_event)
        grant = self._grants.get(grant_id)
        if grant is None or grant.actor_id != actor_id:
            raise ComputerUseDenied("no matching computer-use grant")
        if grant.expires_at <= self._clock():
            raise ComputerUseDenied("computer-use grant expired")
        return grant

    def _check_interrupt(
        self, grant_id: str, cancel_event: threading.Event | None
    ) -> None:
        if self.stop.is_set():
            raise ComputerUseStopped("emergency stop is active")
        if cancel_event is not None and cancel_event.is_set():
            raise ComputerUseStopped("computer-use run was cancelled")
        if grant_id in self._revoked:
            raise ComputerUseStopped("computer-use grant was revoked")
        if not self._enabled or self._adapter is None:
            raise ComputerUseDenied("computer use is unavailable or disabled")

    @staticmethod
    def _require_target(grant: ComputerUseGrant, target: WindowTarget) -> None:
        if (
            target.application_id != grant.application_id
            or target.window_id != grant.window_id
            or target.isolated_session_id != grant.isolated_session_id
        ):
            raise ComputerUseDenied("application, window, or session is outside the grant")

    def _validated_current_target(self, expected: WindowTarget) -> WindowTarget:
        current = self._adapter_required().current_target(expected.window_id)
        if current is None or not current.same_surface(expected):
            raise ComputerUseDenied("target changed or is no longer available")
        return current

    def _adapter_required(self) -> ComputerUseAdapter:
        if self._adapter is None:
            raise ComputerUseDenied("no computer-use adapter is available")
        return self._adapter

    def _active_grants(self) -> list[ComputerUseGrant]:
        now = self._clock()
        return [
            grant for key, grant in self._grants.items()
            if key not in self._revoked and grant.expires_at > now
        ]

    @staticmethod
    def _sensitive(resource: str) -> bool:
        normalized = resource.strip().casefold().replace("_", "-")
        return normalized.startswith(_SENSITIVE_RESOURCE_PREFIXES)

    def _record(
        self,
        event: str,
        grant: ComputerUseGrant,
        target: WindowTarget,
        action_type: str,
        outcome: str,
    ) -> None:
        label = hashlib.sha256(
            f"{target.application_id}:{target.window_id}:{target.isolated_session_id}".encode()
        ).hexdigest()[:12]
        self._audit.append(
            AuditEntry(event, grant.actor_id, grant.grant_id, label, action_type, outcome, self._clock())
        )


def new_action_id() -> str:
    return f"action_{uuid.uuid4().hex}"
