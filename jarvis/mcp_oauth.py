"""OAuth 2.1 sign-in for remote MCP servers (the "Connect" button).

Follows the MCP authorization spec: the server's 401 names its protected-resource metadata,
which names the authorization server; JARVIS reads that server's metadata, registers itself
as a public client (dynamic client registration) when it has no client id, and sends the
operator to the authorization page with PKCE (S256). The Hub's loopback callback exchanges the
code for tokens; refresh tokens renew them. The operator signs in on the provider's own page;
JARVIS never sees a password.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .mcp_client import MCPError, _http_origin, _open_http

TIMEOUT = 20.0
USER_AGENT = "JARVIS-Agent-Hub/1.0 (MCP client)"


class OAuthError(RuntimeError):
    pass


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(_https(url), headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with _open_http(request, timeout=TIMEOUT) as response:
            value = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(f"Could not read {url}: {getattr(exc, 'reason', exc)}") from None
    if not isinstance(value, dict):
        raise OAuthError(f"{url} did not return JSON metadata.")
    return value


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(_https(url), data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        with _open_http(request, timeout=TIMEOUT) as response:
            return json.loads(response.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(400).decode("utf-8", errors="replace")
        raise OAuthError(f"The sign-in server refused ({exc.code}): {detail[:200]}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(f"Could not reach the sign-in server: {getattr(exc, 'reason', exc)}") from None


def _https(url: str) -> str:
    try:
        _http_origin(url)
    except MCPError:
        raise OAuthError("Sign-in endpoints must use HTTPS (or loopback HTTP), without URL credentials or fragments.") from None
    return url


def discover(server_url: str, www_authenticate: str = "") -> dict[str, Any]:
    """Authorization-server metadata for this MCP server."""
    match = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "")
    parts = urllib.parse.urlsplit(server_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    resource_urls = [match.group(1)] if match else [
        f"{origin}/.well-known/oauth-protected-resource{parts.path.rstrip('/')}",
        f"{origin}/.well-known/oauth-protected-resource"]
    authorization_server = origin
    resource = server_url
    for url in resource_urls:
        try:
            meta = _get_json(_https(url))
        except OAuthError:
            continue
        servers = meta.get("authorization_servers") or []
        if servers:
            authorization_server = str(servers[0]).rstrip("/")
        resource = str(meta.get("resource") or server_url)
        break
    as_parts = urllib.parse.urlsplit(authorization_server)
    as_origin = f"{as_parts.scheme}://{as_parts.netloc}"
    as_path = as_parts.path.rstrip("/")
    candidates = [f"{as_origin}/.well-known/oauth-authorization-server{as_path}",
                  f"{as_origin}/.well-known/openid-configuration{as_path}",
                  f"{authorization_server}/.well-known/openid-configuration",
                  f"{as_origin}/.well-known/oauth-authorization-server"]
    for url in dict.fromkeys(candidates):
        try:
            meta = _get_json(_https(url))
        except OAuthError:
            continue
        if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            return {"authorization_endpoint": _https(str(meta["authorization_endpoint"])),
                    "token_endpoint": _https(str(meta["token_endpoint"])),
                    "registration_endpoint": (_https(str(meta["registration_endpoint"]))
                                              if meta.get("registration_endpoint") else None),
                    "scopes_supported": meta.get("scopes_supported") or [],
                    "resource": resource}
    raise OAuthError("This server did not publish OAuth sign-in details. Add a token instead.")


def register(metadata: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
    endpoint = metadata.get("registration_endpoint")
    if not endpoint:
        raise OAuthError("This server does not allow automatic app registration; add a token instead.")
    request = urllib.request.Request(_https(endpoint), data=json.dumps({
        "client_name": "JARVIS Agent Hub", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST")
    try:
        with _open_http(request, timeout=TIMEOUT) as response:
            client = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"App registration was refused ({exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(f"App registration failed: {getattr(exc, 'reason', exc)}") from None
    if not client.get("client_id"):
        raise OAuthError("App registration returned no client id.")
    return {"client_id": str(client["client_id"]), "client_secret": client.get("client_secret"),
            "redirect_uri": redirect_uri}


def begin(metadata: dict[str, Any], client: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The authorization URL and the pending state to keep until the callback."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": client["redirect_uri"],
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
             "resource": metadata.get("resource") or ""}
    scopes = [s for s in metadata.get("scopes_supported") or [] if isinstance(s, str)]
    if scopes:
        query["scope"] = " ".join(scopes[:20])
    endpoint = _https(metadata["authorization_endpoint"])
    url = endpoint + ("&" if "?" in endpoint else "?") + \
        urllib.parse.urlencode({k: v for k, v in query.items() if v})
    return url, {"state": state, "verifier": verifier, "created": time.time()}


def _tokens(answer: dict[str, Any], previous_refresh: str | None = None) -> dict[str, Any]:
    if not answer.get("access_token"):
        raise OAuthError("The sign-in server returned no access token.")
    expires = answer.get("expires_in")
    return {"access_token": str(answer["access_token"]),
            "refresh_token": str(answer.get("refresh_token") or previous_refresh or "") or None,
            "expires_at": time.time() + float(expires) - 60 if isinstance(expires, (int, float)) else None,
            "token_type": str(answer.get("token_type") or "Bearer")}


def exchange(metadata: dict[str, Any], client: dict[str, Any], code: str, verifier: str) -> dict[str, Any]:
    fields = {"grant_type": "authorization_code", "code": code, "redirect_uri": client["redirect_uri"],
              "client_id": client["client_id"], "code_verifier": verifier}
    if metadata.get("resource"):
        fields["resource"] = metadata["resource"]
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    return _tokens(_post_form(metadata["token_endpoint"], fields))


def refresh(metadata: dict[str, Any], client: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    if not tokens.get("refresh_token"):
        raise OAuthError("The sign-in expired; connect again.")
    fields = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
              "client_id": client["client_id"]}
    if metadata.get("resource"):
        fields["resource"] = metadata["resource"]
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    return _tokens(_post_form(metadata["token_endpoint"], fields), tokens.get("refresh_token"))
