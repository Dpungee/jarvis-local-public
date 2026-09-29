"""A small Model Context Protocol (MCP) client for the Agent Hub.

Speaks JSON-RPC 2.0 to one MCP server over any of the three transports servers use:

* ``stdio``: a local program the operator configured (newline-delimited JSON on its pipes);
* ``http``: Streamable HTTP (POST, JSON or event-stream answers, ``Mcp-Session-Id``);
* ``sse``: the older HTTP+SSE transport (a GET event stream that names a POST endpoint).

Only what agents need is implemented: ``initialize``, ``tools/list`` and ``tools/call``.
Results are bounded and returned as text; everything a server returns is untrusted data.
Authentication for remote servers is a bearer token or OAuth (see ``mcp_oauth``); a 401
raises ``AuthRequired`` carrying the server's ``WWW-Authenticate`` header.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "JARVIS Agent Hub", "version": "1.0"}
# Some hosts block Python's default User-Agent; say who is calling.
USER_AGENT = "JARVIS-Agent-Hub/1.0 (MCP client)"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_RESULT_CHARS = 60_000
DEFAULT_TIMEOUT = 60.0


class MCPError(RuntimeError):
    """The server could not be reached or answered with an error."""


class AuthRequired(MCPError):
    """The server needs credentials (401); ``www_authenticate`` says where to get them."""

    def __init__(self, message: str, www_authenticate: str = "") -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


def _rpc(method: str, params: dict[str, Any] | None, request_id: int | None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return message


def _unwrap(message: dict[str, Any]) -> Any:
    if "error" in message:
        error = message.get("error") or {}
        raise MCPError(f"Server error {error.get('code')}: {str(error.get('message'))[:300]}")
    return message.get("result")


# ------------------------------------------------------------------------------ stdio
class StdioTransport:
    def __init__(self, command: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        if not command:
            raise MCPError("No command configured.")
        environment = dict(os.environ)
        environment.update(env or {})
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise MCPError(f"Could not start {command[0]}: {exc}") from None
        self._responses: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True, name="mcp-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="mcp-stderr").start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            if len(raw) > MAX_MESSAGE_BYTES:
                continue
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # servers sometimes print logs on stdout; ignore non-JSON lines
            if isinstance(message, dict) and isinstance(message.get("id"), int):
                with self._lock:
                    box = self._responses.get(message["id"])
                if box is not None:
                    box.put(message)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw in self.process.stderr:
            self.stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip()[:300])
            del self.stderr_tail[:-20]

    def send(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        if self.process.poll() is not None:
            raise MCPError("The server program exited. " + " | ".join(self.stderr_tail[-3:]))
        box: "queue.Queue[dict[str, Any]] | None" = None
        if "id" in message:
            box = queue.Queue(maxsize=1)
            with self._lock:
                self._responses[message["id"]] = box
        try:
            assert self.process.stdin is not None
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as exc:
            raise MCPError(f"The server program stopped accepting input: {exc}") from None
        if box is None:
            return None
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"The server did not answer {message.get('method')} in {int(timeout)} s.") from None
        finally:
            with self._lock:
                self._responses.pop(message["id"], None)

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


# ------------------------------------------------------------------------------- http
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward credentials, session headers or request bodies to a redirect."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _http_origin(url: str) -> tuple[str, str, int]:
    """HTTPS endpoints, or explicit local HTTP servers; no URL credentials/schemes."""
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if (not host or parts.username is not None or parts.password is not None or parts.fragment
                or parts.scheme not in {"https", "http"}
                or parts.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError
        port = parts.port
        if port is not None and port < 1:
            raise ValueError
        return parts.scheme, host.casefold(), port if port is not None else (443 if parts.scheme == "https" else 80)
    except (TypeError, ValueError):
        raise MCPError("Endpoints must use HTTPS (or loopback HTTP), without URL credentials or fragments.") from None


def _open_http(request: urllib.request.Request, *, timeout: float) -> Any:
    """A shared no-redirect boundary for MCP transports and OAuth HTTP exchanges."""
    _http_origin(request.full_url)
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _sse_events(response: Any) -> Any:
    """Yield decoded JSON ``data`` payloads (and their event names) from an event stream."""
    event, data = "message", []
    total = 0
    for raw in response:
        total += len(raw)
        if total > MAX_MESSAGE_BYTES * 4:
            raise MCPError("The server's event stream was too large.")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


class HttpTransport:
    """Streamable HTTP. ``headers`` is called per request so refreshed tokens are used."""

    def __init__(self, url: str, headers: Callable[[], dict[str, str]] | None = None) -> None:
        _http_origin(url)
        self.url = url
        self._headers = headers or (lambda: {})
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def send(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": USER_AGENT, **self._headers()}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(self.url, data=json.dumps(message).encode("utf-8"), headers=headers,
                                         method="POST")
        try:
            response = _open_http(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthRequired("The server needs you to sign in or add a token.",
                                   exc.headers.get("WWW-Authenticate", "")) from None
            detail = exc.read(400).decode("utf-8", errors="replace")
            raise MCPError(f"The server answered HTTP {exc.code}: {detail[:200]}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"Could not reach the server: {getattr(exc, 'reason', exc)}") from None
        with response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if "id" not in message:
                return None
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            if kind == "text/event-stream":
                for _event, data in _sse_events(response):
                    try:
                        parsed = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict) and parsed.get("id") == message["id"]:
                        return parsed
                raise MCPError("The server's event stream ended without an answer.")
            body = response.read(MAX_MESSAGE_BYTES + 1)
            if len(body) > MAX_MESSAGE_BYTES:
                raise MCPError("The server's answer was too large.")
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                raise MCPError("The server answered with something that is not JSON.") from None
            if isinstance(parsed, list):
                parsed = next((m for m in parsed if isinstance(m, dict) and m.get("id") == message["id"]), {})
            return parsed

    def close(self) -> None:
        if not self.session_id:
            return
        try:
            request = urllib.request.Request(self.url, headers={"Mcp-Session-Id": self.session_id, **self._headers()},
                                             method="DELETE")
            _open_http(request, timeout=5).close()
        except (urllib.error.URLError, OSError):
            pass


class SseTransport:
    """The older HTTP+SSE transport: a GET stream announces the POST endpoint and carries answers."""

    def __init__(self, url: str, headers: Callable[[], dict[str, str]] | None = None, *, timeout: float = 30.0) -> None:
        origin = _http_origin(url)
        self.url = url
        self._headers = headers or (lambda: {})
        self._responses: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._lock = threading.Lock()
        self._endpoint: "queue.Queue[str]" = queue.Queue(maxsize=1)
        self._closed = False
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream", "User-Agent": USER_AGENT,
                                                       **self._headers()})
        try:
            self._stream = _open_http(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthRequired("The server needs you to sign in or add a token.",
                                   exc.headers.get("WWW-Authenticate", "")) from None
            raise MCPError(f"The server answered HTTP {exc.code}.") from None
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"Could not reach the server: {getattr(exc, 'reason', exc)}") from None
        threading.Thread(target=self._read, daemon=True, name="mcp-sse").start()
        try:
            endpoint = self._endpoint.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise MCPError("The server did not announce its message endpoint.") from None
        try:
            self.post_url = urllib.parse.urljoin(url, endpoint)
            if _http_origin(self.post_url) != origin:
                raise MCPError("The server pointed messages at a different origin; refused.")
        except (ValueError, MCPError):
            self.close()
            raise MCPError("The server pointed messages at an unsafe or different origin; refused.") from None

    def _read(self) -> None:
        try:
            for event, data in _sse_events(self._stream):
                if event == "endpoint":
                    if self._endpoint.empty():
                        self._endpoint.put(data.strip())
                    continue
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if isinstance(message, dict) and isinstance(message.get("id"), int):
                    with self._lock:
                        box = self._responses.get(message["id"])
                    if box is not None:
                        box.put(message)
        except (OSError, MCPError, ValueError):
            pass

    def send(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        box: "queue.Queue[dict[str, Any]] | None" = None
        if "id" in message:
            box = queue.Queue(maxsize=1)
            with self._lock:
                self._responses[message["id"]] = box
        request = urllib.request.Request(self.post_url, data=json.dumps(message).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                                  **self._headers()}, method="POST")
        try:
            _open_http(request, timeout=timeout).close()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthRequired("The server needs you to sign in again.", exc.headers.get("WWW-Authenticate", ""))
            raise MCPError(f"The server answered HTTP {exc.code}.") from None
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"Could not reach the server: {getattr(exc, 'reason', exc)}") from None
        if box is None:
            return None
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"The server did not answer {message.get('method')} in {int(timeout)} s.") from None
        finally:
            with self._lock:
                self._responses.pop(message["id"], None)

    def close(self) -> None:
        self._closed = True
        try:
            self._stream.close()
        except (OSError, AttributeError):
            pass


# ----------------------------------------------------------------------------- client
class MCPClient:
    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}
        self.instructions = ""

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        with self._lock:
            request_id = next(self._ids)
        answer = self.transport.send(_rpc(method, params, request_id), timeout)
        return _unwrap(answer or {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.transport.send(_rpc(method, params, None), 10.0)

    def initialize(self, timeout: float = 30.0) -> dict[str, Any]:
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": CLIENT_INFO}, timeout) or {}
        self.server_info = result.get("serverInfo") or {}
        self.instructions = str(result.get("instructions") or "")[:2000]
        version = result.get("protocolVersion")
        if isinstance(self.transport, HttpTransport) and isinstance(version, str):
            self.transport.protocol_version = version
        self.notify("notifications/initialized")
        return result

    def list_tools(self, limit: int = 200) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _page in range(20):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor or len(tools) >= limit:
                break
        return tools[:limit]

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout) or {}
        return {"ok": not result.get("isError"), "content": result_text(result)}

    def close(self) -> None:
        self.transport.close()


def result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or f"[resource {resource.get('uri')}]"))
        elif kind == "resource_link":
            parts.append(f"[link {item.get('name') or ''} {item.get('uri')}]")
        elif kind in {"image", "audio"}:
            parts.append(f"[{kind} {item.get('mimeType') or ''}, {len(str(item.get('data') or ''))} base64 chars]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
    text = "\n".join(parts)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n… (truncated)"


def connect(kind: str, *, url: str = "", command: list[str] | None = None, env: dict[str, str] | None = None,
            headers: Callable[[], dict[str, str]] | None = None, cwd: str | None = None) -> MCPClient:
    """Open and initialize one server connection. Remote ``auto`` tries Streamable HTTP, then SSE."""
    if kind == "stdio":
        client = MCPClient(StdioTransport(list(command or []), env=env, cwd=cwd))
        try:
            client.initialize()
        except MCPError:
            client.close()
            raise
        return client
    if kind not in {"http", "sse", "auto"}:
        raise MCPError("Unknown connection type.")
    attempts = ["sse"] if kind == "sse" else ["http"] if kind == "http" else (
        ["sse", "http"] if url.rstrip("/").endswith("/sse") else ["http", "sse"])
    last: MCPError | None = None
    for attempt in attempts:
        try:
            transport = HttpTransport(url, headers) if attempt == "http" else SseTransport(url, headers)
            client = MCPClient(transport)
            client.initialize()
            client.transport_kind = attempt  # type: ignore[attr-defined]
            return client
        except AuthRequired:
            raise
        except MCPError as exc:
            last = exc
    raise last or MCPError("Could not connect.")
