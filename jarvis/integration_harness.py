"""Closed, non-executing integration contracts for the agent harness.

These types record configuration and evaluate authority. They never connect to an
MCP server, load a plugin, inspect applications, retrieve credentials, browse,
call an API, or sign/broadcast a transaction.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from enum import Enum
from urllib.parse import urlparse


class IntegrationKind(str, Enum):
    MCP = "MCP"
    PLUGIN = "PLUGIN"
    DESKTOP = "DESKTOP"
    BROWSER = "BROWSER"
    API = "API"
    WALLET = "WALLET"


class IntegrationState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    DISABLED = "DISABLED"
    READY = "READY"


@dataclass(frozen=True)
class IntegrationDescriptor:
    integration_id: str
    kind: IntegrationKind
    label: str
    state: IntegrationState = IntegrationState.UNCONFIGURED
    capability_prefix: str = ""
    simulated: bool = False
    detail: str = "No connection or authority configured."

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class ScopedGrant:
    agent_id: str
    capability: str
    resource: str
    destinations: tuple[str, ...] = ()
    max_amount_minor: int | None = None
    max_fee_minor: int | None = None

    def permits(self, *, agent_id: str, capability: str, resource: str) -> bool:
        return (
            self.agent_id == agent_id
            and self.capability == capability
            and self.resource == resource
        )


class IntegrationRegistry:
    """In-memory registry with no auto-discovery or activation side effects."""

    def __init__(self) -> None:
        self._items: dict[str, IntegrationDescriptor] = {}

    def register(self, descriptor: IntegrationDescriptor) -> None:
        if descriptor.integration_id in self._items:
            raise ValueError("integration id already registered")
        if not descriptor.integration_id or not descriptor.capability_prefix:
            raise ValueError("integration id and capability prefix are required")
        self._items[descriptor.integration_id] = descriptor

    def list_public(self) -> list[dict[str, object]]:
        return [self._items[key].public_dict() for key in sorted(self._items)]


class CredentialBrokerPolicy:
    """Validate a brokered request without ever accepting raw secret material."""

    def authorize(
        self,
        *,
        agent_id: str,
        capability: str,
        credential_ref: str,
        destination: str,
        resource: str,
        grant: ScopedGrant,
        supplied_secret: str | None = None,
    ) -> str:
        if supplied_secret is not None:
            raise PermissionError("raw credential material is forbidden")
        if not credential_ref.startswith("cred_"):
            raise ValueError("opaque credential reference is required")
        parsed = urlparse(destination)
        if parsed.scheme != "https" or not parsed.hostname:
            raise PermissionError("credential destinations require HTTPS")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
            raise PermissionError("private and local destinations are forbidden")
        if not grant.permits(agent_id=agent_id, capability=capability, resource=resource):
            raise PermissionError("request is outside the scoped grant")
        if parsed.hostname.casefold() not in {host.casefold() for host in grant.destinations}:
            raise PermissionError("destination is outside the scoped grant")
        return credential_ref


@dataclass(frozen=True)
class WalletIntent:
    wallet_ref: str
    chain: str
    purpose: str
    destination: str
    amount_minor: int
    fee_minor: int
    approval_effects: tuple[str, ...] = ()


class WalletSignerPolicy:
    """Fail-closed intent validation only; no signing implementation exists."""

    def authorize(self, *, agent_id: str, intent: WalletIntent, grant: ScopedGrant) -> None:
        if not intent.wallet_ref.startswith("wallet_"):
            raise ValueError("opaque wallet reference is required")
        if not grant.permits(
            agent_id=agent_id, capability="wallet.sign", resource=intent.wallet_ref
        ):
            raise PermissionError("wallet is outside the scoped grant")
        if intent.destination not in grant.destinations:
            raise PermissionError("wallet destination is outside the scoped grant")
        if intent.amount_minor < 0 or intent.fee_minor < 0:
            raise ValueError("amount and fee must be non-negative")
        if grant.max_amount_minor is None or intent.amount_minor > grant.max_amount_minor:
            raise PermissionError("amount exceeds the scoped budget")
        if grant.max_fee_minor is None or intent.fee_minor > grant.max_fee_minor:
            raise PermissionError("fee exceeds the scoped budget")
        if intent.approval_effects:
            raise PermissionError("token approval effects are not authorized")


def default_registry() -> IntegrationRegistry:
    registry = IntegrationRegistry()
    for descriptor in (
        IntegrationDescriptor("mcp", IntegrationKind.MCP, "MCP servers", capability_prefix="mcp."),
        IntegrationDescriptor("plugins", IntegrationKind.PLUGIN, "Plugins", capability_prefix="plugin."),
        IntegrationDescriptor("desktop", IntegrationKind.DESKTOP, "Desktop applications", capability_prefix="desktop."),
        IntegrationDescriptor("browser", IntegrationKind.BROWSER, "Browser and web", capability_prefix="browser."),
        IntegrationDescriptor("api", IntegrationKind.API, "Credential broker", capability_prefix="api."),
        IntegrationDescriptor("wallet", IntegrationKind.WALLET, "Isolated wallet signer", capability_prefix="wallet."),
    ):
        registry.register(descriptor)
    return registry
