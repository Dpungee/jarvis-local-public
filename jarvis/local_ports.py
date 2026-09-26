"""Which local process is listening on a loopback TCP port (Windows), read-only.

Used to bind a preview address to the managed process that serves it: a URL that happens to
answer on some port is not evidence that the app the agent started is what answers there.
Returns ``None`` where the platform query is unavailable, and callers treat that as "not
proven" rather than as a match.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
from ctypes import wintypes

_AF_INET, _AF_INET6 = 2, 23
_TCP_TABLE_OWNER_PID_LISTENER = 3
_TH32CS_SNAPPROCESS = 0x00000002


def listening_pids(port: int) -> set[int] | None:
    """PIDs listening on ``port`` over IPv4 or IPv6, or ``None`` if unavailable here."""
    if os.name != "nt" or not 1 <= int(port) <= 65535:
        return None
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
    except OSError:
        return None
    found: set[int] = set()
    for family, row_size, port_offset, pid_offset in ((_AF_INET, 24, 8, 20), (_AF_INET6, 56, 36, 52)):
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family,
                                     _TCP_TABLE_OWNER_PID_LISTENER, 0)
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size.value or 4)
            result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family,
                                                  _TCP_TABLE_OWNER_PID_LISTENER, 0)
            if result == 0:
                break
            if result != 122:  # ERROR_INSUFFICIENT_BUFFER: retry with the new size
                return None
        else:
            return None
        raw = buffer.raw
        count = struct.unpack_from("<I", raw, 0)[0]
        for index in range(count):
            base = 4 + index * row_size
            if base + row_size > len(raw):
                break
            local_port = socket.ntohs(struct.unpack_from("<I", raw, base + port_offset)[0] & 0xFFFF)
            if local_port == port:
                found.add(struct.unpack_from("<I", raw, base + pid_offset)[0])
    return found


def parent_map() -> dict[int, int] | None:
    """``{pid: parent_pid}`` for running processes, or ``None`` if unavailable here."""
    if os.name != "nt":
        return None

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return None
    parents: dict[int, int] = {}
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def port_owner(port: int, root_pid: int) -> str | None:
    """``"self"``, ``"other"`` or ``"none"`` for who listens on ``port``; ``None`` if unknown."""
    owners = listening_pids(port)
    if owners is None:
        return None
    if not owners:
        return "none"
    served = served_by(port, root_pid)
    if served is None:
        return None
    return "self" if served else "other"


def served_by(port: int, root_pid: int) -> bool | None:
    """Whether ``port`` is listened on by ``root_pid`` or one of its descendants.

    ``None`` means the platform cannot tell; ``False`` means another process owns the port
    (or nothing does).
    """
    owners = listening_pids(port)
    parents = parent_map()
    if owners is None or parents is None:
        return None
    for pid in owners:
        seen: set[int] = set()
        current = pid
        while current and current not in seen and len(seen) < 64:
            if current == root_pid:
                return True
            seen.add(current)
            current = parents.get(current, 0)
    return False


def reachable_beyond_loopback(port: int) -> bool:
    """True when something accepts connections on ``port`` at a non-loopback local address.

    Previews are for this computer only. A server listening on every interface would expose
    the agent's app to the local network, so it is not opened.
    """
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return False
    for address in sorted(addresses):
        if address.startswith("127."):
            continue
        try:
            with socket.create_connection((address, port), timeout=0.3):
                return True
        except OSError:
            continue
    return False
