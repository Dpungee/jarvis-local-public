"""Authenticated local credentials; Windows per-user DPAPI wraps each AES-GCM key.

No plaintext or unwrapped key is persisted. Files are bound to their absolute path
and purpose: moving a profile requires reconnecting, not copying credential files.
Unsupported platforms/protection failures fail closed. This does not defend against
code running as the same OS account, rollback of whole files, or privileged access.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"JARVIS-CREDENTIAL\x00\x01"
MAX_BYTES = 1024 * 1024
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class CredentialProtectionError(PermissionError):
    """Deliberately contains neither credential material nor private filesystem paths."""


def _failure() -> CredentialProtectionError:
    return CredentialProtectionError("Credential protection unavailable or invalid; reconnect securely.")


def _dpapi(data: bytes, entropy: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise _failure()

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    data_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy)
    source = Blob(len(data), ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    extra = Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    operation.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        # UI_FORBIDDEN only: LOCAL_MACHINE must never be enabled.
        if not operation(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1,
                         ctypes.byref(result)):
            raise _failure()
        if not result.data or not 0 < result.size <= MAX_BYTES:
            raise _failure()
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(data_buffer, 0, len(data_buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def _ordinary(info: os.stat_result, *, directory: bool = False) -> bool:
    return (not (getattr(info, "st_file_attributes", 0) & 0x400)
            and (stat.S_ISDIR(info.st_mode) if directory else
                 stat.S_ISREG(info.st_mode) and info.st_nlink == 1))


def safe_directory(path: Path, *, create: bool = False) -> None:
    """Reject aliases/reparse points before creating or touching credential paths."""
    path = path.absolute()
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                continue
            part.mkdir(mode=0o700)
            info = part.lstat()
        if not _ordinary(info, directory=True):
            raise _failure()
    if os.path.normcase(str(path)) != os.path.normcase(str(path.resolve())):
        raise _failure()


class ProtectedFile:
    """Atomic ciphertext persistence and validated, in-place legacy migration.

    Shared in-process locks serialize migration/replacement across store instances.
    Callers serialize larger read/modify/write transactions using their policy lock.
    """

    def __init__(self, path: Path, purpose: str) -> None:
        self.path = path.absolute()
        identity = os.path.normcase(str(self.path))
        self._aad = MAGIC + purpose.encode("utf-8") + b"\x00" + identity.encode("utf-8")
        with _locks_guard:
            self._lock = _locks.setdefault(identity, threading.RLock())

    def _read(self) -> bytes | None:
        safe_directory(self.path.parent)
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return None
        if not _ordinary(before) or before.st_size > MAX_BYTES:
            raise _failure()
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _ordinary(opened) or not os.path.samestat(before, opened):
                raise _failure()
            value = stream.read(MAX_BYTES + 1)
        if len(value) > MAX_BYTES:
            raise _failure()
        return value

    def _seal(self, value: bytes) -> bytes:
        key = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(12)
        wrapped = _dpapi(key, hashlib.sha256(self._aad).digest())
        encrypted = AESGCM(key).encrypt(nonce, value, self._aad)
        return MAGIC + len(wrapped).to_bytes(4, "big") + wrapped + nonce + encrypted

    def _open(self, value: bytes) -> bytes:
        offset = len(MAGIC)
        size = int.from_bytes(value[offset:offset + 4], "big")
        offset += 4
        if not value.startswith(MAGIC) or not 1 <= size <= 65536 or len(value) < offset + size + 28:
            raise _failure()
        key = _dpapi(value[offset:offset + size], hashlib.sha256(self._aad).digest(), decrypt=True)
        offset += size
        return AESGCM(key).decrypt(value[offset:offset + 12], value[offset + 12:], self._aad)

    def read(self, validate_legacy: Callable[[bytes], bool]) -> bytes | None:
        with self._lock:
            try:
                value = self._read()
                if value is None:
                    return None
                if value.startswith(MAGIC):
                    return self._open(value)
                if not validate_legacy(value):
                    raise _failure()
                # Never expose legacy credentials until protected replacement succeeds.
                self.write(value)
                return value
            except (OSError, ValueError, TypeError, InvalidTag, UnsupportedAlgorithm):
                raise _failure() from None

    def write(self, value: bytes) -> None:
        with self._lock:
            temporary = None
            try:
                if not value or len(value) > MAX_BYTES - 65536:
                    raise _failure()
                previous = self._read()
                encrypted = self._seal(value)
                if self._open(encrypted) != value:
                    raise _failure()
                safe_directory(self.path.parent, create=True)
                candidate = self.path.with_name(self.path.name + "." + secrets.token_hex(16) + ".tmp")
                fd = os.open(candidate, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                temporary = candidate
                with os.fdopen(fd, "w+b") as stream:
                    if stream.write(encrypted) != len(encrypted):
                        raise _failure()
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.seek(0)
                    persisted = stream.read(MAX_BYTES + 1)
                    if persisted != encrypted or self._open(persisted) != value:
                        raise _failure()
                if self._read() != previous:
                    raise _failure()
                os.replace(temporary, self.path)
                temporary = None
            except (OSError, ValueError, TypeError, InvalidTag, UnsupportedAlgorithm):
                raise _failure() from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass  # Any surviving scratch file contains ciphertext only.

    def clear(self) -> None:
        with self._lock:
            try:
                if self._read() is not None:
                    self.path.unlink()
            except (OSError, ValueError, TypeError):
                raise _failure() from None
