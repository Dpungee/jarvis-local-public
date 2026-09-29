"""The Agent Hub's Connections: MCP servers and app connectors the operator sets up once.

Each connection is one MCP server: a remote URL (Streamable HTTP or SSE) signed in with
OAuth or a token, or a local program started over stdio. The operator adds, tests, enables
and removes them in the Hub; agents granted "connections" get each connected server's tools.

Configuration (names, URLs, commands, tool lists) lives in ``<state>/connections/config.json``;
secrets (tokens, OAuth tokens and client ids, secret URLs, secret environment values) live in
``<state>/connections/secrets/<id>.json``, readable only by this Windows account. Secrets are
never returned by any API and never reach a prompt. Tool results are untrusted data.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Callable

from . import mcp_client, mcp_oauth
from .openrouter import _owner_only

ASK_MODES = ("changes", "always", "never")
MAX_CONNECTIONS = 40
PENDING_TTL = 900.0

# Curated presets. URLs are the vendors' own documented remote MCP endpoints; Zapier covers
# apps without an official server (Gmail, Outlook/Hotmail, YouTube, X, Google Calendar...).
PRESETS: list[dict[str, Any]] = [
    {"id": "zapier", "name": "Zapier (Gmail, Outlook/Hotmail, YouTube, X, Calendar, 8,000+ apps)",
     "category": "Everything", "kind": "auto", "url": "https://mcp.zapier.com/api/mcp/mcp", "auth": "choose",
     "setup": "Easiest: click Add, then sign in to Zapier and pick the apps and actions agents may use (Gmail, "
              "Outlook/Hotmail, YouTube, X, Google Calendar, Slack, HubSpot, Box...). Or paste a token or full "
              "server URL from mcp.zapier.com.",
     "docs": "https://help.zapier.com/hc/en-us/articles/36265392843917"},
    {"id": "github", "name": "GitHub", "category": "Developer", "kind": "http",
     "url": "https://api.githubcopilot.com/mcp/", "auth": "token",
     "setup": "Create a fine-grained personal access token at github.com/settings/tokens and paste it here."},
    {"id": "notion", "name": "Notion", "category": "Docs", "kind": "auto", "url": "https://mcp.notion.com/mcp",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Notion."},
    {"id": "linear", "name": "Linear", "category": "Projects", "kind": "auto", "url": "https://mcp.linear.app/mcp",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Linear."},
    {"id": "atlassian", "name": "Jira & Confluence (Atlassian)", "category": "Projects", "kind": "auto",
     "url": "https://mcp.atlassian.com/v2/mcp", "auth": "oauth", "setup": "Click Connect and approve JARVIS."},
    {"id": "asana", "name": "Asana", "category": "Projects", "kind": "auto", "url": "https://mcp.asana.com/sse",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Asana."},
    {"id": "airtable", "name": "Airtable", "category": "Data", "kind": "auto", "url": "https://mcp.airtable.com/mcp",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Airtable."},
    {"id": "canva", "name": "Canva", "category": "Design", "kind": "auto", "url": "https://mcp.canva.com/mcp",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Canva."},
    {"id": "figma", "name": "Figma", "category": "Design", "kind": "auto", "url": "https://mcp.figma.com/mcp",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Figma."},
    {"id": "stripe", "name": "Stripe", "category": "Payments", "kind": "auto", "url": "https://mcp.stripe.com/",
     "auth": "oauth", "setup": "Click Connect. Anything that moves money always asks you first."},
    {"id": "vercel", "name": "Vercel", "category": "Developer", "kind": "auto", "url": "https://mcp.vercel.com/",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in Vercel."},
    {"id": "supabase", "name": "Supabase", "category": "Developer", "kind": "auto",
     "url": "https://mcp.supabase.com/mcp", "auth": "oauth", "setup": "Click Connect and approve JARVIS."},
    {"id": "monday", "name": "monday.com", "category": "Projects", "kind": "auto", "url": "https://mcp.monday.com/sse",
     "auth": "oauth", "setup": "Click Connect and approve JARVIS in monday.com."},
    {"id": "custom-url", "name": "Any MCP server (URL)", "category": "Custom", "kind": "auto", "url": "",
     "auth": "choose", "setup": "Paste the server's MCP URL. Use a token if it gave you one, or Connect to sign in."},
    {"id": "custom-command", "name": "Any MCP server (local program)", "category": "Custom", "kind": "stdio",
     "url": "", "auth": "env", "setup": "The command that starts the server (for example npx -y <package>). "
     "It runs on this computer with your account's permissions, so only add programs you trust."},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}
_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("_", str(text).casefold()).strip("_")[:24] or "server"


def _is_read_only(tool: dict[str, Any]) -> bool:
    annotations = tool.get("annotations") or {}
    return annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True


class ConnectionManager:
    def __init__(self, state_dir: Path, *, callback_url: Callable[[], str] | None = None,
                 connect: Callable[..., Any] | None = None) -> None:
        self.root = Path(state_dir) / "connections"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "secrets").mkdir(exist_ok=True)
        self._config_path = self.root / "config.json"
        self._lock = threading.RLock()
        self._operation_locks: dict[str, Any] = {}
        self._clients: dict[str, Any] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._callback_url = callback_url or (lambda: "http://127.0.0.1:8790/api/connections/oauth/callback")
        self._connect = connect or mcp_client.connect

    # ---------------------------------------------------------------- storage
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self._config_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, config: dict[str, dict[str, Any]]) -> None:
        temporary = self._config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, indent=1), encoding="utf-8")
        os.replace(temporary, self._config_path)

    def _secret_path(self, connection_id: str) -> Path:
        if not re.fullmatch(r"conn_[0-9a-f]{12}", connection_id):
            raise KeyError("Unknown connection.")
        return self.root / "secrets" / f"{connection_id}.json"

    def _secrets(self, connection_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._secret_path(connection_id).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _put_secrets(self, connection_id: str, values: dict[str, Any]) -> None:
        path = self._secret_path(connection_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values), encoding="utf-8")
        _owner_only(temporary)
        os.replace(temporary, path)
        _owner_only(path)

    # ------------------------------------------------------------------ views
    @staticmethod
    def _public(entry: dict[str, Any], secret: dict[str, Any]) -> dict[str, Any]:
        tools = entry.get("tools") or []
        auth = entry.get("auth")
        return {
            "id": entry["id"], "name": entry["name"], "preset": entry.get("preset"), "kind": entry["kind"],
            "url": entry.get("url_display") or entry.get("url") or "", "command": entry.get("command") or [],
            "env_names": sorted((secret.get("env") or {}).keys()), "auth": auth,
            "signed_in": bool(secret.get("token") or secret.get("url")
                              or (secret.get("oauth_tokens") or {}).get("access_token")),
            "enabled": bool(entry.get("enabled")), "ask": entry.get("ask") or "changes",
            "status": entry.get("status") or "not tested", "error": entry.get("error"),
            "checked_at": entry.get("checked_at"), "server": entry.get("server"),
            "tools": [{"name": t["name"], "description": str(t.get("description") or "")[:200],
                       "read_only": _is_read_only(t)} for t in tools],
        }

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            config = self._load()
            return [self._public(entry, self._secrets(cid)) for cid, entry in
                    sorted(config.items(), key=lambda item: item[1].get("created_at", 0))]

    def get(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._load().get(connection_id)
            if entry is None:
                raise KeyError("Unknown connection.")
            return self._public(entry, self._secrets(connection_id))

    # ---------------------------------------------------------------- changes
    def add(self, spec: dict[str, Any]) -> dict[str, Any]:
        preset = PRESET_BY_ID.get(str(spec.get("preset") or "custom-url"))
        if preset is None:
            raise ValueError("Unknown preset.")
        name = " ".join(str(spec.get("name") or preset["name"]).split())[:60]
        kind = preset["kind"] if preset["kind"] != "auto" else str(spec.get("kind") or "auto")
        if kind not in {"auto", "http", "sse", "stdio"}:
            raise ValueError("Unknown connection type.")
        secret: dict[str, Any] = {}
        entry: dict[str, Any] = {"name": name, "preset": preset["id"], "kind": kind, "enabled": True,
                                 "ask": "changes", "created_at": time.time(), "tools": []}
        token = str(spec.get("token") or "").strip()
        if kind == "stdio":
            command = spec.get("command")
            if isinstance(command, str):
                command = command.split()
            if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
                raise ValueError("Give the command that starts the server.")
            entry["command"] = [c[:500] for c in command[:40]]
            env = spec.get("env") or {}
            if not isinstance(env, dict) or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(k))
                                                    for k in env):
                raise ValueError("Environment names must be letters, digits and underscores.")
            secret["env"] = {str(k): str(v) for k, v in env.items()}
            entry["auth"] = "env"
        else:
            url = str(spec.get("url") or preset.get("url") or "").strip()
            if url and "://" not in url:
                url = "https://" + url
            if not re.fullmatch(r"https://[^\s]{4,2000}", url) and not re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:\d+)?/[^\s]*", url):
                raise ValueError("Give the server's https URL.")
            # A URL with a key in it (Zapier's, for example) is a secret: keep it out of the config.
            if "?" in url or preset["id"] == "zapier" and url != preset["url"]:
                secret["url"] = url
                parts = url.split("/")
                entry["url_display"] = "/".join(parts[:3]) + "/… (key hidden)"
            else:
                entry["url"] = url
            signs_in = preset["auth"] == "oauth" or (preset["auth"] == "choose" and bool(preset.get("url")))
            entry["auth"] = "token" if token else ("oauth" if signs_in and not secret.get("url") else
                                                   "token" if preset["auth"] == "token" and not secret.get("url")
                                                   else "none")
            if token:
                secret["token"] = token
        with self._lock:
            config = self._load()
            if len(config) >= MAX_CONNECTIONS:
                raise ValueError("That is the most connections the Hub keeps.")
            connection_id = f"conn_{_secrets.token_hex(6)}"
            entry["id"] = connection_id
            entry["slug"] = self._unique_slug(config, slug(name))
            config[connection_id] = entry
            self._save(config)
            self._put_secrets(connection_id, secret)
        return self.get(connection_id)

    @staticmethod
    def _unique_slug(config: dict[str, dict[str, Any]], base: str) -> str:
        taken = {e.get("slug") for e in config.values()}
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}{n}", n + 1
        return candidate

    def update(self, connection_id: str, *, enabled: bool | None = None, ask: str | None = None,
               token: str | None = None) -> dict[str, Any]:
        with self._operation_lock(connection_id):
            return self._update(connection_id, enabled=enabled, ask=ask, token=token)

    def _update(self, connection_id: str, *, enabled: bool | None = None, ask: str | None = None,
                token: str | None = None) -> dict[str, Any]:
        with self._lock:
            config = self._load()
            entry = config.get(connection_id)
            if entry is None:
                raise KeyError("Unknown connection.")
            if enabled is not None:
                entry["enabled"] = bool(enabled)
            if ask is not None:
                if ask not in ASK_MODES:
                    raise ValueError("Ask before changes, always, or never.")
                entry["ask"] = ask
            if token is not None:
                secret = self._secrets(connection_id)
                secret["token"] = token.strip()
                self._put_secrets(connection_id, secret)
                entry["auth"] = "token"
            entry["policy_revision"] = int(entry.get("policy_revision", 0)) + 1
            self._save(config)
        if token is not None or enabled is False:
            self._drop_client(connection_id)
        return self.get(connection_id)

    def delete(self, connection_id: str) -> None:
        with self._operation_lock(connection_id):
            self._delete(connection_id)

    def _delete(self, connection_id: str) -> None:
        self._drop_client(connection_id)
        with self._lock:
            config = self._load()
            if config.pop(connection_id, None) is None:
                raise KeyError("Unknown connection.")
            self._save(config)
            try:
                self._secret_path(connection_id).unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------- connecting
    def _headers(self, connection_id: str) -> Callable[[], dict[str, str]]:
        def headers() -> dict[str, str]:
            secret = self._secrets(connection_id)
            oauth = secret.get("oauth_tokens") or {}
            if oauth.get("access_token"):
                if oauth.get("expires_at") and time.time() > float(oauth["expires_at"]) and oauth.get("refresh_token"):
                    oauth = mcp_oauth.refresh(secret["oauth_metadata"], secret["oauth_client"], oauth)
                    secret["oauth_tokens"] = oauth
                    self._put_secrets(connection_id, secret)
                return {"Authorization": f"Bearer {oauth['access_token']}"}
            if secret.get("token"):
                return {"Authorization": f"Bearer {secret['token']}"}
            return {}
        return headers

    def _drop_client(self, connection_id: str) -> None:
        with self._operation_lock(connection_id):
            with self._lock:
                client = self._clients.pop(connection_id, None)
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass

    def _client(self, connection_id: str) -> Any:
        with self._lock:
            client = self._clients.get(connection_id)
            if client is not None:
                return client
            entry = self._load().get(connection_id)
            if entry is None:
                raise KeyError("Unknown connection.")
            secret = self._secrets(connection_id)
        if entry["kind"] == "stdio":
            client = self._connect("stdio", command=entry["command"], env=secret.get("env") or {},
                                   cwd=str(self.root))
        else:
            client = self._connect(entry["kind"], url=secret.get("url") or entry.get("url") or "",
                                   headers=self._headers(connection_id))
        with self._lock:
            self._clients[connection_id] = client
        return client

    def _record(self, connection_id: str, **fields: Any) -> None:
        with self._operation_lock(connection_id), self._lock:
            config = self._load()
            if connection_id in config:
                config[connection_id].update(fields, checked_at=time.time())
                config[connection_id]["policy_revision"] = int(config[connection_id].get("policy_revision", 0)) + 1
                self._save(config)

    def test(self, connection_id: str) -> dict[str, Any]:
        """Connect, list the server's tools and remember them; the result says what happened."""
        with self._operation_lock(connection_id):
            return self._test(connection_id)

    def _test(self, connection_id: str) -> dict[str, Any]:
        self._drop_client(connection_id)
        try:
            client = self._client(connection_id)
            tools = client.list_tools()
        except mcp_client.AuthRequired as exc:
            with self._lock:
                entry = self._load().get(connection_id) or {}
            needs = "Sign in with Connect." if entry.get("auth") != "token" else "The token was refused."
            self._record(connection_id, status="needs sign-in", error=needs, www_authenticate=exc.www_authenticate)
            return self.get(connection_id)
        except (mcp_client.MCPError, mcp_oauth.OAuthError, KeyError) as exc:
            self._record(connection_id, status="error", error=str(exc)[:300])
            return self.get(connection_id)
        slim = [{"name": str(t["name"])[:64], "description": str(t.get("description") or "")[:1000],
                 "inputSchema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object"},
                 "annotations": t.get("annotations") if isinstance(t.get("annotations"), dict) else {}}
                for t in tools]
        self._record(connection_id, status="connected", error=None, tools=slim,
                     server=str((client.server_info or {}).get("name") or "")[:80])
        return self.get(connection_id)

    # ------------------------------------------------------------------ OAuth
    def oauth_start(self, connection_id: str) -> str:
        with self._lock:
            entry = self._load().get(connection_id)
        if entry is None or entry["kind"] == "stdio":
            raise ValueError("Only remote servers sign in with Connect.")
        secret = self._secrets(connection_id)
        url = secret.get("url") or entry.get("url") or ""
        metadata = mcp_oauth.discover(url, entry.get("www_authenticate") or "")
        redirect = self._callback_url()
        client = secret.get("oauth_client")
        if not client or client.get("redirect_uri") != redirect:
            client = mcp_oauth.register(metadata, redirect)
        authorize_url, pending = mcp_oauth.begin(metadata, client)
        secret.update(oauth_metadata=metadata, oauth_client=client)
        self._put_secrets(connection_id, secret)
        with self._lock:
            now = time.time()
            self._pending = {s: p for s, p in self._pending.items() if now - p["created"] < PENDING_TTL}
            self._pending[pending["state"]] = {**pending, "connection_id": connection_id}
        return authorize_url

    def oauth_callback(self, state: str, code: str) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.pop(str(state), None)
        if pending is None or time.time() - pending["created"] > PENDING_TTL:
            raise ValueError("This sign-in link expired or was already used. Click Connect again.")
        connection_id = pending["connection_id"]
        with self._operation_lock(connection_id):
            # Deleting a connection also invalidates an outstanding sign-in.
            self.get(connection_id)
            return self._oauth_complete(connection_id, str(code), pending["verifier"])

    def _oauth_complete(self, connection_id: str, code: str, verifier: str) -> dict[str, Any]:
        secret = self._secrets(connection_id)
        tokens = mcp_oauth.exchange(secret["oauth_metadata"], secret["oauth_client"], code, verifier)
        secret["oauth_tokens"] = tokens
        self._put_secrets(connection_id, secret)
        with self._lock:
            config = self._load()
            if connection_id in config:
                config[connection_id]["auth"] = "oauth"
                config[connection_id]["policy_revision"] = int(config[connection_id].get("policy_revision", 0)) + 1
                self._save(config)
        return self.test(connection_id)

    # ------------------------------------------------------------ agent tools
    def _operation_lock(self, connection_id: str) -> Any:
        """Fence a connection's configuration/credentials against an executing action.

        Different connections may run independently. An acknowledged update or deletion
        guarantees that the next action observes it; an already-dispatched action finishes
        before that update returns.
        """
        self._secret_path(connection_id)  # validate identity before retaining a lock
        with self._lock:
            return self._operation_locks.setdefault(connection_id, threading.RLock())

    @staticmethod
    def _tool_meta(entry: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
        ask = entry.get("ask") or "changes"
        if ask not in ASK_MODES:
            raise PermissionError("The connection's approval policy is invalid.")
        fingerprint = hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return {"connection_id": entry["id"], "connection": entry["name"], "slug": entry["slug"],
                "ask": ask, "tool": tool["name"], "description": tool.get("description") or "",
                "schema": tool.get("inputSchema") or {}, "read_only": _is_read_only(tool),
                "tool_fingerprint": fingerprint, "policy_revision": int(entry.get("policy_revision", 0))}

    def _current_tool(self, connection_id: str, tool: str) -> dict[str, Any]:
        with self._lock:
            entry = self._load().get(connection_id)
        if entry is None or not entry.get("enabled") or entry.get("status") != "connected":
            raise PermissionError("This connection is disabled, removed, or needs to be tested again.")
        matches = [item for item in entry.get("tools") or [] if item.get("name") == tool]
        if len(matches) != 1:
            raise PermissionError("The connected tool is missing or ambiguous; test the connection again.")
        return self._tool_meta(entry, matches[0])

    @contextmanager
    def execution_scope(self, expected: dict[str, Any]) -> Any:
        """Keep current identity, schema and policy stable through approval and dispatch."""
        with self._operation_lock(expected["connection_id"]):
            current = self._current_tool(expected["connection_id"], expected["tool"])
            if current["tool_fingerprint"] != expected.get("tool_fingerprint"):
                raise PermissionError("The connected tool changed; start a new turn to load its current schema.")
            yield current

    def agent_tools(self) -> list[dict[str, Any]]:
        """Every enabled, connected server's tools (from the last test; no network here)."""
        with self._lock:
            config = self._load()
        out = []
        for entry in config.values():
            if not entry.get("enabled") or entry.get("status") != "connected":
                continue
            for tool in entry.get("tools") or []:
                out.append(self._tool_meta(entry, tool))
        return out

    def call(self, connection_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._operation_lock(connection_id):
            self._current_tool(connection_id, tool)
            try:
                return self._client(connection_id).call_tool(tool, arguments)
            except mcp_client.AuthRequired:
                self._drop_client(connection_id)
                self._record(connection_id, status="needs sign-in", error="Sign in again with Connect.")
                raise
            except mcp_client.MCPError:
                self._drop_client(connection_id)
                # A timeout/error may follow a successful remote mutation. A transport retry
                # must never silently spend the same one-use approval a second time.
                raise mcp_client.MCPError("The connected action failed or its outcome is unknown. It was not "
                                          "retried; check the connected app before attempting it again.") from None

    def close(self) -> None:
        with self._lock:
            ids = list(self._clients)
        for connection_id in ids:
            self._drop_client(connection_id)


def presets() -> list[dict[str, Any]]:
    return [dict(p) for p in PRESETS]
