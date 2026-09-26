"""Authenticated local broker: a Windows named pipe restricted to the current user.

Loopback TCP cannot tell the operator's own processes apart from any other local
process, another Windows user on the same machine, or a sandboxed (AppContainer,
low-integrity) process. A named pipe with a discretionary ACL and a mandatory
integrity label can. This module gives Jarvis processes on one machine (Presence and
the Companion indicator today) a control channel that only the current Windows user,
at the server's integrity level or higher, can open, and that verifies the peer
process on both sides.

Boundaries, all deterministic and model-free:

- Windows only. Anywhere else ``LocalBrokerUnavailable`` is raised and callers keep
  their existing behaviour; nothing new is exposed.
- The pipe is created with a protected DACL granting access to the current user's
  SID and nobody else, a mandatory label that forbids lower-integrity processes from
  reading or writing it, and ``PIPE_REJECT_REMOTE_CLIENTS``.
- The server owns the only pipe instance (``FILE_FLAG_FIRST_PIPE_INSTANCE`` plus a
  maximum of one instance), so a pre-created pipe of the same name makes the
  server fail closed instead of talking to an impostor. Presence adds a per-start
  random suffix to the name so the name cannot be reserved in advance.
- After every connection both sides verify that the peer process runs under the
  same user SID and at an integrity level no lower than their own; a mismatch is
  refused and counted.
- Framing is a 4-byte big-endian length followed by one UTF-8 JSON object of at
  most ``max_frame_bytes`` and at most ``MAX_JSON_DEPTH`` nesting levels. One
  request and one response per connection.
- The broker enforces framing, size, and envelope shape only. Which request kinds
  are permitted, and with what authority, is decided by the host's handler. Any
  handler failure becomes a refusal carrying only the exception class name; the
  serving thread survives every client and handler error.

Known limit: I/O is blocking and connections are served one at a time. A client
that connects and never sends a frame delays later clients until it disconnects or
the server stops; ``stop()`` always returns because it cancels the parked I/O. The
label and DACL keep that within the operator's own processes at the same or higher
integrity level.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # ctypes.wintypes only imports cleanly on Windows
    import ctypes.wintypes as _wintypes
except (ImportError, ValueError):  # pragma: no cover - non-Windows hosts
    _wintypes = None

MAX_FRAME_BYTES_DEFAULT = 64 * 1024
MAX_FRAME_BYTES_CEILING = 1024 * 1024
MAX_KIND_CHARS = 64
MAX_JSON_DEPTH = 32
PIPE_PREFIX = "\\\\.\\pipe\\"

# Win32 constants (winbase.h / winnt.h / accctrl.h).
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
READ_CONTROL = 0x00020000
OPEN_EXISTING = 3
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
TOKEN_INTEGRITY_LEVEL_CLASS = 25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_TERMINATE = 0x0001
SE_FILE_OBJECT = 1
SE_KERNEL_OBJECT = 6
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
DACL_SECURITY_INFORMATION = 0x00000004
LABEL_SECURITY_INFORMATION = 0x00000010
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995
_PEER_GONE = {ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED, ERROR_OPERATION_ABORTED}
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

RequestHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class LocalBrokerError(RuntimeError):
    """A broker request could not be completed."""


class LocalBrokerUnavailable(LocalBrokerError):
    """The authenticated local broker cannot exist on this platform or host."""


class _PeerGone(LocalBrokerError):
    """The peer closed the pipe; a benign disconnect, not a malformed request."""


def _windows() -> bool:
    return os.name == "nt"


def broker_available() -> bool:
    """True where an ACL-protected named pipe can be created."""
    return _windows()


def new_pipe_nonce() -> str:
    """A per-start random suffix so a pipe name cannot be reserved ahead of time."""
    return secrets.token_hex(8)


def default_pipe_name(data_dir: Path | str, *, nonce: str | None = None) -> str:
    """One pipe name per Jarvis data directory (plus an optional per-start nonce)."""
    resolved = str(Path(data_dir).expanduser().resolve()).casefold()
    scope = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]
    name = f"{PIPE_PREFIX}jarvis-local-{scope}"
    if nonce:
        text = str(nonce)
        if not text.isalnum() or len(text) > 32:
            raise ValueError("pipe nonce must be short and alphanumeric")
        name += f"-{text}"
    return name


def validate_pipe_name(name: str) -> str:
    text = str(name)
    if not text.startswith(PIPE_PREFIX):
        raise ValueError("pipe name must start with the local named-pipe prefix")
    leaf = text[len(PIPE_PREFIX):]
    if not leaf or len(leaf) > 200 or any(ch in leaf for ch in "\\/\x00") or leaf != leaf.strip():
        raise ValueError("pipe name leaf is invalid")
    return text


def _validated_sid(value: str, label: str) -> str:
    sid = str(value).strip()
    if not sid.startswith("S-1-") or any(ch not in "S-0123456789" for ch in sid):
        raise ValueError(f"{label} is malformed")
    return sid


def pipe_security_descriptor_sddl(user_sid: str, integrity_sid: str | None = None) -> str:
    """A protected DACL admitting exactly one SID, plus a no-read-up/no-write-up label."""
    sid = _validated_sid(user_sid, "user SID")
    text = f"D:P(A;;GA;;;{sid})"
    if integrity_sid is not None:
        text += f"S:(ML;;NRNW;;;{_validated_sid(integrity_sid, 'integrity SID')})"
    return text


def integrity_rid(integrity_sid: str) -> int:
    """The mandatory-level value of an integrity SID such as ``S-1-16-8192``."""
    sid = _validated_sid(integrity_sid, "integrity SID")
    if not sid.startswith("S-1-16-"):
        raise ValueError("integrity SID must be a mandatory-label SID")
    return int(sid.rsplit("-", 1)[1])


def _clamp_frame_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_frame_bytes must be an integer")
    return max(1024, min(int(value), MAX_FRAME_BYTES_CEILING))


# --------------------------------------------------------------------------- win32


class _Win32:
    """Lazily bound kernel32/advapi32 entry points (Windows only)."""

    _instance: "_Win32 | None" = None

    def __init__(self) -> None:
        if _wintypes is None:
            raise LocalBrokerUnavailable("ctypes.wintypes is unavailable on this host")
        wintypes = _wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        k, a = self.kernel32, self.advapi32
        HANDLE, BOOL, DWORD, LPVOID = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID
        k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPVOID]
        k.CreateNamedPipeW.restype = HANDLE
        k.ConnectNamedPipe.argtypes = [HANDLE, LPVOID]
        k.ConnectNamedPipe.restype = BOOL
        k.DisconnectNamedPipe.argtypes = [HANDLE]
        k.DisconnectNamedPipe.restype = BOOL
        k.CloseHandle.argtypes = [HANDLE]
        k.CloseHandle.restype = BOOL
        k.ReadFile.argtypes = [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID]
        k.ReadFile.restype = BOOL
        k.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID]
        k.WriteFile.restype = BOOL
        k.FlushFileBuffers.argtypes = [HANDLE]
        k.FlushFileBuffers.restype = BOOL
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE]
        k.CreateFileW.restype = HANDLE
        k.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, DWORD]
        k.WaitNamedPipeW.restype = BOOL
        k.GetNamedPipeClientProcessId.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
        k.GetNamedPipeClientProcessId.restype = BOOL
        k.GetNamedPipeServerProcessId.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
        k.GetNamedPipeServerProcessId.restype = BOOL
        k.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
        k.OpenProcess.restype = HANDLE
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = HANDLE
        k.LocalFree.argtypes = [LPVOID]
        k.LocalFree.restype = LPVOID
        k.CancelSynchronousIo.argtypes = [HANDLE]
        k.CancelSynchronousIo.restype = BOOL
        k.OpenThread.argtypes = [DWORD, BOOL, DWORD]
        k.OpenThread.restype = HANDLE
        a.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
        a.OpenProcessToken.restype = BOOL
        a.GetTokenInformation.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD, ctypes.POINTER(DWORD)]
        a.GetTokenInformation.restype = BOOL
        a.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        a.ConvertSidToStringSidW.restype = BOOL
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, DWORD, ctypes.POINTER(LPVOID), ctypes.POINTER(wintypes.ULONG)
        ]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            LPVOID, DWORD, DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)
        ]
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = BOOL
        a.GetSecurityInfo.argtypes = [
            HANDLE, ctypes.c_int, DWORD, ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
            ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
        ]
        a.GetSecurityInfo.restype = DWORD

    @classmethod
    def get(cls) -> "_Win32":
        if not _windows():
            raise LocalBrokerUnavailable("the authenticated local broker requires Windows named pipes")
        if cls._instance is None:
            try:
                cls._instance = cls()
            except (OSError, AttributeError) as exc:
                raise LocalBrokerUnavailable(f"Win32 named-pipe entry points are unavailable: {exc}") from None
        return cls._instance


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


def _last_error() -> int:
    return int(ctypes.get_last_error())


def _token_sid(process_handle: int, information_class: int) -> str:
    """String SID at the head of a TOKEN_USER or TOKEN_MANDATORY_LABEL structure."""
    win = _Win32.get()
    token = _wintypes.HANDLE()
    if not win.advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        raise LocalBrokerError(f"OpenProcessToken failed ({_last_error()})")
    try:
        needed = _wintypes.DWORD(0)
        win.advapi32.GetTokenInformation(token, information_class, None, 0, ctypes.byref(needed))
        size = int(needed.value)
        if size <= 0 or size > 4096:
            raise LocalBrokerError("token information has an unexpected size")
        buffer = ctypes.create_string_buffer(size)
        if not win.advapi32.GetTokenInformation(
            token, information_class, buffer, size, ctypes.byref(needed)
        ):
            raise LocalBrokerError(f"GetTokenInformation failed ({_last_error()})")
        # Both structures begin with SID_AND_ATTRIBUTES {PSID Sid; DWORD Attributes}.
        sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
        text = _wintypes.LPWSTR()
        if not win.advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise LocalBrokerError(f"ConvertSidToStringSidW failed ({_last_error()})")
        try:
            return str(text.value)
        finally:
            win.kernel32.LocalFree(text)
    finally:
        win.kernel32.CloseHandle(token)


_IDENTITY_CACHE: dict[str, str] = {}
_IDENTITY_LOCK = threading.Lock()


def _own_identity(information_class: int, key: str) -> str:
    with _IDENTITY_LOCK:
        cached = _IDENTITY_CACHE.get(key)
    if cached is not None:
        return cached
    win = _Win32.get()
    value = _token_sid(win.kernel32.GetCurrentProcess(), information_class)
    with _IDENTITY_LOCK:
        _IDENTITY_CACHE[key] = value
    return value


def current_user_sid() -> str:
    """String SID of the user running this process (cached; it cannot change)."""
    return _own_identity(TOKEN_USER_CLASS, "user")


def current_integrity_sid() -> str:
    """Mandatory-label SID of this process, for example ``S-1-16-8192`` (medium)."""
    return _own_identity(TOKEN_INTEGRITY_LEVEL_CLASS, "integrity")


def _pid_identity(pid: int) -> tuple[str, str]:
    win = _Win32.get()
    process = win.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not process:
        raise LocalBrokerError(f"peer process {int(pid)} could not be inspected ({_last_error()})")
    try:
        return _token_sid(process, TOKEN_USER_CLASS), _token_sid(process, TOKEN_INTEGRITY_LEVEL_CLASS)
    finally:
        win.kernel32.CloseHandle(process)


def _peer_matches_current_user(handle: int, *, server_side: bool) -> bool:
    """True when the peer runs as our user SID at an integrity level not below ours."""
    win = _Win32.get()
    pid = _wintypes.DWORD(0)
    query = win.kernel32.GetNamedPipeClientProcessId if server_side else win.kernel32.GetNamedPipeServerProcessId
    if not query(handle, ctypes.byref(pid)) or int(pid.value) <= 0:
        return False
    try:
        user, integrity = _pid_identity(int(pid.value))
        return user == current_user_sid() and integrity_rid(integrity) >= integrity_rid(current_integrity_sid())
    except (LocalBrokerError, ValueError):
        return False


def _pipe_open_mode(first_instance: bool = True) -> int:
    mode = PIPE_ACCESS_DUPLEX
    if first_instance:
        mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
    return mode


def _pipe_mode_flags() -> int:
    return PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS


def _open_client_handle(name: str, access: int) -> int:
    win = _Win32.get()
    handle = win.kernel32.CreateFileW(
        name, access, 0, None, OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None
    )
    if handle is None or handle == INVALID_HANDLE_VALUE:
        raise OSError(_last_error(), "CreateFileW failed")
    return int(handle)


def pipe_effective_sddl(name: str) -> str:
    """The owner/group/DACL/label of a live pipe as SDDL, read through a client handle."""
    win = _Win32.get()
    name = validate_pipe_name(name)
    deadline = time.monotonic() + 2.0
    while True:
        try:
            handle = _open_client_handle(name, READ_CONTROL)
            break
        except OSError as exc:
            code = int(exc.errno or 0)
            if code != ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                raise LocalBrokerError(f"could not open the pipe for security inspection ({code})") from None
            # The single instance is still disconnecting its previous client.
            win.kernel32.WaitNamedPipeW(name, 200)
    information = (
        OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION
        | DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION
    )
    descriptor = ctypes.c_void_p()
    try:
        status = 1
        for object_type in (SE_KERNEL_OBJECT, SE_FILE_OBJECT):
            status = int(win.advapi32.GetSecurityInfo(
                handle, object_type, information, None, None, None, None, ctypes.byref(descriptor)
            ))
            if status == 0:
                break
        if status != 0:
            raise LocalBrokerError(f"GetSecurityInfo failed ({status})")
        text = _wintypes.LPWSTR()
        length = _wintypes.ULONG(0)
        if not win.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, SDDL_REVISION_1, information, ctypes.byref(text), ctypes.byref(length)
        ):
            raise LocalBrokerError(f"security descriptor could not be rendered ({_last_error()})")
        try:
            return str(text.value)
        finally:
            win.kernel32.LocalFree(text)
    finally:
        if descriptor.value:
            win.kernel32.LocalFree(descriptor)
        win.kernel32.CloseHandle(handle)


# ------------------------------------------------------------------------ framing


def _json_nesting_depth(body: bytes) -> int:
    """Maximum bracket nesting of a JSON document, ignoring string contents."""
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > deepest:
                deepest = depth
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return deepest


def _encode_frame(document: Mapping[str, Any], max_frame_bytes: int) -> bytes:
    try:
        body = json.dumps(
            dict(document), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise LocalBrokerError("response is not JSON-serialisable") from None
    if len(body) > max_frame_bytes:
        raise LocalBrokerError("frame exceeds the configured size limit")
    if _json_nesting_depth(body) > MAX_JSON_DEPTH:
        raise LocalBrokerError("frame nests deeper than the configured limit")
    return struct.pack(">I", len(body)) + body


def _read_exact(handle: int, size: int) -> bytes:
    win = _Win32.get()
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        buffer = ctypes.create_string_buffer(min(remaining, 65536))
        read = _wintypes.DWORD(0)
        if not win.kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
            code = _last_error()
            if code in _PEER_GONE:
                raise _PeerGone("peer closed the pipe before the frame completed")
            raise LocalBrokerError(f"ReadFile failed ({code})")
        if int(read.value) == 0:
            raise _PeerGone("peer closed the pipe before the frame completed")
        chunks.append(buffer.raw[: int(read.value)])
        remaining -= int(read.value)
    return b"".join(chunks)


def _read_frame(handle: int, max_frame_bytes: int) -> dict[str, Any]:
    header = _read_exact(handle, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > max_frame_bytes:
        raise LocalBrokerError("frame length is outside the configured size limit")
    body = _read_exact(handle, int(length))
    if _json_nesting_depth(body) > MAX_JSON_DEPTH:
        raise LocalBrokerError("frame nests deeper than the configured limit")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise LocalBrokerError("frame is not a UTF-8 JSON document") from None
    if not isinstance(document, dict):
        raise LocalBrokerError("frame must be a JSON object")
    return document


def _write_all(handle: int, data: bytes) -> None:
    win = _Win32.get()
    view = memoryview(data)
    while view:
        written = _wintypes.DWORD(0)
        chunk = bytes(view[:65536])
        if not win.kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None):
            code = _last_error()
            if code in _PEER_GONE:
                raise _PeerGone("peer closed the pipe before the frame was written")
            raise LocalBrokerError(f"WriteFile failed ({code})")
        if int(written.value) == 0:
            raise _PeerGone("peer stopped accepting data")
        view = view[int(written.value):]
    # No FlushFileBuffers here: on a pipe it blocks until the peer has read the
    # data, and a server that refuses a request never reads it. The server flushes
    # once, before disconnecting, so a reading client always sees the reply.


def validate_request_envelope(document: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (kind, payload) or raise for anything outside the closed envelope."""
    if set(document) - {"kind", "payload"}:
        raise LocalBrokerError("request carries fields outside the envelope")
    kind = document.get("kind")
    if not isinstance(kind, str) or not kind or len(kind) > MAX_KIND_CHARS or kind != kind.strip():
        raise LocalBrokerError("request kind is invalid")
    payload = document.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise LocalBrokerError("request payload must be an object")
    return kind, payload


# ------------------------------------------------------------------------- server


class LocalBrokerServer:
    """Serve one request per connection over an ACL- and label-protected named pipe."""

    def __init__(
        self,
        name: str,
        handler: RequestHandler,
        *,
        max_frame_bytes: int = MAX_FRAME_BYTES_DEFAULT,
        on_reject: Callable[[str], None] | None = None,
    ) -> None:
        if not callable(handler):
            raise ValueError("handler must be callable")
        self.name = validate_pipe_name(name)
        self._handler = handler
        self._max_frame_bytes = _clamp_frame_bytes(max_frame_bytes)
        self._on_reject = on_reject
        self._handle: int | None = None
        self._descriptor: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._served = 0
        self._rejected_peer = 0
        self._rejected_frame = 0
        self._handler_failures = 0
        self._serve_failures = 0
        self._peer_disconnected = 0
        self._deferred_close: tuple[int, int | None] | None = None
        self.user_sid: str | None = None
        self.integrity_sid: str | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Create the single pipe instance now (so squatting fails fast) and serve."""
        win = _Win32.get()
        with self._lock:
            if self._handle is not None:
                raise LocalBrokerError("broker is already started")
            self.user_sid = current_user_sid()
            self.integrity_sid = current_integrity_sid()
            sddl = pipe_security_descriptor_sddl(self.user_sid, self.integrity_sid)
            descriptor = ctypes.c_void_p()
            size = _wintypes.ULONG(0)
            if not win.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(size)
            ):
                raise LocalBrokerError(f"security descriptor could not be built ({_last_error()})")
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, 0)
            handle = win.kernel32.CreateNamedPipeW(
                self.name,
                _pipe_open_mode(first_instance=True),
                _pipe_mode_flags(),
                1,
                self._max_frame_bytes + 4,
                self._max_frame_bytes + 4,
                0,
                ctypes.byref(attributes),
            )
            if handle is None or handle == INVALID_HANDLE_VALUE:
                code = _last_error()
                win.kernel32.LocalFree(descriptor)
                if code in (ERROR_ACCESS_DENIED, ERROR_PIPE_BUSY):
                    raise LocalBrokerError(
                        "the broker pipe name already exists or cannot be created "
                        f"({code}); refusing to start behind another process"
                    )
                raise LocalBrokerError(f"CreateNamedPipeW failed ({code})")
            self._handle = int(handle)
            self._descriptor = descriptor.value
            self._stop.clear()
            self._thread = threading.Thread(target=self._serve, name="jarvis-local-broker", daemon=True)
            self._thread.start()

    def _nudge(self) -> None:
        """Connect and disconnect once so a ConnectNamedPipe parked in the server thread returns."""
        try:
            handle = _open_client_handle(self.name, GENERIC_READ | GENERIC_WRITE)
        except OSError:
            return
        _Win32.get().kernel32.CloseHandle(handle)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop serving; always returns, even with a client parked on the pipe."""
        with self._lock:
            handle = self._handle
            thread = self._thread
            descriptor = self._descriptor
            self._handle = None
            self._thread = None
            self._descriptor = None
        self._stop.set()
        win = _Win32.get() if handle is not None else None
        if thread is not None and win is not None:
            deadline = time.monotonic() + max(0.1, float(timeout))
            while thread.is_alive() and time.monotonic() < deadline:
                # Cancel whatever synchronous I/O the server thread is parked in and
                # release a pending ConnectNamedPipe; repeat until the thread exits,
                # because the thread may enter another blocking call in between.
                if thread.native_id is not None:
                    thread_handle = win.kernel32.OpenThread(THREAD_TERMINATE, False, int(thread.native_id))
                    if thread_handle:
                        win.kernel32.CancelSynchronousIo(thread_handle)
                        win.kernel32.CloseHandle(thread_handle)
                self._nudge()
                thread.join(0.05)
        if handle is None or win is None:
            return
        if thread is not None and thread.is_alive():
            # Still inside a handler after the deadline: the serving thread closes the
            # handle when it exits, so no stale handle value is ever operated on.
            with self._lock:
                self._deferred_close = (handle, descriptor)
            thread.join(0.2)
            with self._lock:
                pending = self._deferred_close
                self._deferred_close = None
            if pending is None or thread.is_alive():
                return
            handle, descriptor = pending
        win.kernel32.DisconnectNamedPipe(handle)
        win.kernel32.CloseHandle(handle)
        if descriptor is not None:
            win.kernel32.LocalFree(descriptor)

    @property
    def running(self) -> bool:
        return self._handle is not None and self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "served": self._served,
            "rejected_peer": self._rejected_peer,
            "rejected_frame": self._rejected_frame,
            "handler_failures": self._handler_failures,
            "serve_failures": self._serve_failures,
            "peer_disconnected": self._peer_disconnected,
        }

    # -- serving -------------------------------------------------------------

    def _reject(self, reason: str) -> None:
        if self._on_reject is not None:
            try:
                self._on_reject(reason)
            except Exception:
                # Rejection listeners are observability only and never affect serving.
                pass

    def _serve(self) -> None:
        win = _Win32.get()
        try:
            self._serve_loop(win)
        finally:
            with self._lock:
                pending = self._deferred_close
                self._deferred_close = None
            if pending is not None:
                handle, descriptor = pending
                win.kernel32.DisconnectNamedPipe(handle)
                win.kernel32.CloseHandle(handle)
                if descriptor is not None:
                    win.kernel32.LocalFree(descriptor)

    def _serve_loop(self, win: "_Win32") -> None:
        backoff = 0.01
        while not self._stop.is_set():
            handle = self._handle
            if handle is None:
                return
            connected = win.kernel32.ConnectNamedPipe(handle, None)
            if self._stop.is_set():
                return
            if not connected and _last_error() != ERROR_PIPE_CONNECTED:
                code = _last_error()
                # ERROR_NO_DATA means a client opened and closed the instance before we
                # listened; the instance must be reset or every later connect fails too.
                win.kernel32.DisconnectNamedPipe(handle)
                if code == ERROR_NO_DATA:
                    backoff = 0.01
                    continue
                if code == 6:  # ERROR_INVALID_HANDLE: the instance is gone for good
                    self._serve_failures += 1
                    self._reject("serving stopped: the pipe handle is no longer valid")
                    return
                self._serve_failures += 1
                if backoff == 0.01:
                    self._reject(f"connect failed ({code}); retrying with back-off")
                time.sleep(backoff)
                backoff = min(backoff * 2, 1.0)
                continue
            backoff = 0.01
            try:
                self._serve_one(handle)
            except BaseException as exc:  # noqa: BLE001 - the serving thread must survive
                # The serving thread must outlive every client and handler failure,
                # including a handler that raises SystemExit.
                self._serve_failures += 1
                self._reject(f"serving failed: {type(exc).__name__}")
            finally:
                if not self._stop.is_set():
                    win.kernel32.FlushFileBuffers(handle)
                win.kernel32.DisconnectNamedPipe(handle)

    def _serve_one(self, handle: int) -> None:
        if not _peer_matches_current_user(handle, server_side=True):
            self._rejected_peer += 1
            self._reject("peer process is not running as the current user at this integrity level")
            self._respond_safely(handle, {"ok": False, "error": "peer identity rejected"})
            return
        try:
            document = _read_frame(handle, self._max_frame_bytes)
            kind, payload = validate_request_envelope(document)
        except _PeerGone:
            # A benign disconnect (a probe, a cancelled client, an inspection open);
            # nothing was sent, so nothing is refused.
            self._peer_disconnected += 1
            return
        except LocalBrokerError as exc:
            self._rejected_frame += 1
            self._reject(f"malformed request: {exc}")
            self._respond_safely(handle, {"ok": False, "error": str(exc)})
            return
        if self._stop.is_set():
            return
        try:
            result = self._handler({"kind": kind, "payload": payload})
            if not isinstance(result, Mapping):
                raise LocalBrokerError("handler must return a mapping")
            response: dict[str, Any] = {"ok": True, "result": dict(result)}
        except (LocalBrokerError, LookupError, PermissionError, ValueError, TypeError, RuntimeError) as exc:
            self._handler_failures += 1
            response = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:500]}
        except Exception as exc:
            # Unexpected failures are refused by class name only; the message could
            # carry storage paths or record content, and the peer is only the indicator.
            self._handler_failures += 1
            self._reject(f"handler failed: {type(exc).__name__}")
            response = {"ok": False, "error": type(exc).__name__}
        self._served += 1
        self._respond_safely(handle, response)

    def _respond_safely(self, handle: int, response: Mapping[str, Any]) -> None:
        if self._stop.is_set():
            return
        try:
            frame = _encode_frame(response, self._max_frame_bytes)
        except LocalBrokerError as exc:
            frame = _encode_frame({"ok": False, "error": str(exc)}, self._max_frame_bytes)
        try:
            _write_all(handle, frame)
        except LocalBrokerError:
            # The peer went away; there is nobody left to tell.
            pass


# ------------------------------------------------------------------------- client


class LocalBrokerClient:
    """Connect, verify the server runs as our user, exchange one frame, disconnect."""

    def __init__(
        self,
        name: str,
        *,
        timeout: float = 2.0,
        max_frame_bytes: int = MAX_FRAME_BYTES_DEFAULT,
    ) -> None:
        self.name = validate_pipe_name(name)
        self.timeout = max(0.1, min(float(timeout), 30.0))
        self._max_frame_bytes = _clamp_frame_bytes(max_frame_bytes)

    def _connect(self) -> int:
        win = _Win32.get()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                return _open_client_handle(self.name, GENERIC_READ | GENERIC_WRITE)
            except OSError as exc:
                code = int(exc.errno or 0)
            if code == ERROR_FILE_NOT_FOUND:
                raise LocalBrokerUnavailable("the broker pipe does not exist")
            if code == ERROR_ACCESS_DENIED:
                raise LocalBrokerError("access to the broker pipe was denied")
            if code != ERROR_PIPE_BUSY:
                raise LocalBrokerError(f"CreateFileW failed ({code})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LocalBrokerError("the broker pipe stayed busy until the timeout")
            win.kernel32.WaitNamedPipeW(self.name, max(1, int(remaining * 1000)))

    def call(self, kind: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = {"kind": str(kind), "payload": dict(payload or {})}
        validate_request_envelope(request)
        frame = _encode_frame(request, self._max_frame_bytes)
        win = _Win32.get()
        handle = self._connect()
        try:
            if not _peer_matches_current_user(handle, server_side=False):
                raise LocalBrokerError(
                    "the broker pipe is not served by a process running as this user at this integrity level"
                )
            _write_all(handle, frame)
            response = _read_frame(handle, self._max_frame_bytes)
        finally:
            win.kernel32.CloseHandle(handle)
        if response.get("ok") is True and isinstance(response.get("result"), dict):
            return dict(response["result"])
        error = response.get("error")
        raise LocalBrokerError(str(error) if isinstance(error, str) and error else "broker refused the request")
