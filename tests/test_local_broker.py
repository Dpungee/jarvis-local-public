"""Authenticated local broker: ACL/label-protected named-pipe round trips and refusals."""
from __future__ import annotations

import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from jarvis import local_broker
from jarvis.local_broker import (
    FILE_FLAG_FIRST_PIPE_INSTANCE,
    PIPE_PREFIX,
    PIPE_REJECT_REMOTE_CLIENTS,
    LocalBrokerClient,
    LocalBrokerError,
    LocalBrokerServer,
    LocalBrokerUnavailable,
    default_pipe_name,
    integrity_rid,
    pipe_security_descriptor_sddl,
    validate_pipe_name,
    validate_request_envelope,
)

WINDOWS = os.name == "nt"
ROOT = Path(__file__).resolve().parents[1]


def _test_pipe_name() -> str:
    return PIPE_PREFIX + "jarvis-test-" + uuid.uuid4().hex[:16]


def _open_raw(name: str):
    """Open the pipe with plain file semantics, retrying while the single instance is busy."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return open(name, "r+b", buffering=0)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def _raw_exchange(name: str, data: bytes) -> dict:
    """Talk to the pipe without the client class so malformed frames can be sent."""
    with _open_raw(name) as pipe:
        pipe.write(data)
        header = pipe.read(4)
        (length,) = struct.unpack(">I", header)
        return json.loads(pipe.read(length).decode("utf-8"))


class LocalBrokerContractTests(unittest.TestCase):
    """Platform-independent contract checks."""

    def test_default_pipe_name_is_scoped_per_data_directory_and_optional_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = default_pipe_name(first)
            two = default_pipe_name(second)
            nonced = default_pipe_name(first, nonce="abcdef0123456789")
        self.assertTrue(one.startswith(PIPE_PREFIX + "jarvis-local-"))
        self.assertNotEqual(one, two)
        self.assertEqual(one, default_pipe_name(Path(first)))
        self.assertTrue(nonced.startswith(one + "-"))
        self.assertEqual(validate_pipe_name(nonced), nonced)
        self.assertEqual(len(local_broker.new_pipe_nonce()), 16)
        for bad in ("../x", "a b", "x" * 33, "\\"):
            with self.subTest(nonce=bad):
                with self.assertRaises(ValueError):
                    default_pipe_name(first, nonce=bad)

    def test_pipe_names_outside_the_local_prefix_are_rejected(self) -> None:
        for bad in ("jarvis", "\\\\host\\pipe\\jarvis", PIPE_PREFIX, PIPE_PREFIX + "a\\b",
                    PIPE_PREFIX + "a/b", PIPE_PREFIX + " a", PIPE_PREFIX + "x" * 201):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    validate_pipe_name(bad)

    def test_security_descriptor_admits_one_sid_and_labels_integrity(self) -> None:
        self.assertEqual(
            pipe_security_descriptor_sddl("S-1-5-21-1-2-3-1001"),
            "D:P(A;;GA;;;S-1-5-21-1-2-3-1001)",
        )
        self.assertEqual(
            pipe_security_descriptor_sddl("S-1-5-21-1-2-3-1001", "S-1-16-8192"),
            "D:P(A;;GA;;;S-1-5-21-1-2-3-1001)S:(ML;;NRNW;;;S-1-16-8192)",
        )
        for bad in ("", "S-1", "Everyone", "S-1-5-21-1;2", "S-1-5-21-1-2-3-1001)(A;;GA;;;WD"):
            with self.subTest(sid=bad):
                with self.assertRaises(ValueError):
                    pipe_security_descriptor_sddl(bad)
        with self.assertRaises(ValueError):
            pipe_security_descriptor_sddl("S-1-5-21-1-2-3-1001", "S-1-16-8192)(A;;GA;;;WD")
        self.assertEqual(integrity_rid("S-1-16-4096"), 4096)
        self.assertEqual(integrity_rid("S-1-16-12288"), 12288)
        with self.assertRaises(ValueError):
            integrity_rid("S-1-5-21-1-2-3-1001")

    def test_pipe_creation_flags_reject_remote_clients_and_squatting(self) -> None:
        self.assertTrue(local_broker._pipe_mode_flags() & PIPE_REJECT_REMOTE_CLIENTS)
        self.assertTrue(local_broker._pipe_open_mode() & FILE_FLAG_FIRST_PIPE_INSTANCE)

    def test_request_envelope_is_closed(self) -> None:
        self.assertEqual(validate_request_envelope({"kind": "status"}), ("status", {}))
        self.assertEqual(
            validate_request_envelope({"kind": "control", "payload": {"action": "pause"}}),
            ("control", {"action": "pause"}),
        )
        for bad in (
            {"kind": "status", "extra": 1},
            {"kind": ""},
            {"kind": " status"},
            {"kind": "k" * 65},
            {"kind": 3},
            {"kind": "status", "payload": []},
            {"payload": {}},
        ):
            with self.subTest(document=bad):
                with self.assertRaises(LocalBrokerError):
                    validate_request_envelope(bad)

    def test_frames_beyond_the_limit_or_not_serialisable_are_refused_before_sending(self) -> None:
        with self.assertRaises(LocalBrokerError):
            local_broker._encode_frame({"kind": "x" * 2048}, 1024)
        with self.assertRaisesRegex(LocalBrokerError, "not JSON-serialisable"):
            local_broker._encode_frame({"kind": object()}, 4096)

    def test_nesting_depth_scanner_ignores_string_contents(self) -> None:
        self.assertEqual(local_broker._json_nesting_depth(b'{"a": [1, {"b": []}]}'), 4)
        self.assertEqual(local_broker._json_nesting_depth(b'{"a": "[[[[[[\\"]]"}'), 1)
        self.assertEqual(local_broker._json_nesting_depth(b"[" * 500), 500)

    def test_non_windows_hosts_fail_closed(self) -> None:
        with patch.object(local_broker, "_windows", return_value=False):
            self.assertFalse(local_broker.broker_available())
            server = LocalBrokerServer(_test_pipe_name(), lambda request: {})
            with self.assertRaises(LocalBrokerUnavailable):
                server.start()
            with self.assertRaises(LocalBrokerUnavailable):
                LocalBrokerClient(_test_pipe_name()).call("status")

    def test_server_requires_a_callable_handler(self) -> None:
        with self.assertRaises(ValueError):
            LocalBrokerServer(_test_pipe_name(), None)  # type: ignore[arg-type]


@unittest.skipUnless(WINDOWS, "named pipes exist only on Windows")
class NamedPipeBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = _test_pipe_name()
        self.rejections: list[str] = []
        self.requests: list[dict] = []

        def handler(request):
            self.requests.append(dict(request))
            kind = request["kind"]
            if kind == "boom":
                raise ValueError("handler refused")
            if kind == "not-a-mapping":
                return ["nope"]
            if kind == "attribute-error":
                raise AttributeError("unexpected attribute")
            if kind == "os-error":
                raise OSError(5, "database is locked")
            if kind == "unserialisable":
                return {"value": object()}
            if kind == "nan":
                return {"value": float("nan")}
            if kind == "deep-result":
                nested: dict = {}
                cursor = nested
                for _ in range(40):
                    cursor["d"] = {}
                    cursor = cursor["d"]
                return nested
            if kind == "system-exit":
                raise SystemExit(3)
            if kind == "slow":
                time.sleep(0.05)
            return {"echo": kind, "payload": request["payload"]}

        self.server = LocalBrokerServer(self.name, handler, on_reject=self.rejections.append)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_round_trip_returns_the_handler_result(self) -> None:
        client = LocalBrokerClient(self.name)
        self.assertEqual(
            client.call("status", {"n": 1}),
            {"echo": "status", "payload": {"n": 1}},
        )
        self.assertEqual(client.call("second"), {"echo": "second", "payload": {}})
        self.assertEqual(self.server.stats["served"], 2)
        self.assertEqual(self.server.stats["rejected_peer"], 0)
        self.assertTrue(self.server.running)
        self.assertTrue(str(self.server.user_sid).startswith("S-1-"))
        self.assertTrue(str(self.server.integrity_sid).startswith("S-1-16-"))

    def test_effective_pipe_security_matches_the_declared_policy(self) -> None:
        sddl = local_broker.pipe_effective_sddl(self.name)
        sid = local_broker.current_user_sid()
        self.assertIn(f"(A;;FA;;;{sid})", sddl)
        self.assertIn("D:P", sddl)
        # Windows renders the label with its own flag order and an AI (auto-inherit) tag.
        self.assertRegex(sddl, r"S:(AI)?\(ML;;(NRNW|NWNR);;;(S-1-16-\d+|LW|ME|MP|HI|SI)\)")
        self.assertEqual(sddl.count("(A;;"), 1)
        self.assertEqual(LocalBrokerClient(self.name).call("after"), {"echo": "after", "payload": {}})

    def test_the_pipe_name_cannot_be_taken_twice(self) -> None:
        second = LocalBrokerServer(self.name, lambda request: {})
        with self.assertRaisesRegex(LocalBrokerError, "already exists"):
            second.start()
        self.assertFalse(second.running)
        self.assertEqual(LocalBrokerClient(self.name).call("still-first"), {"echo": "still-first", "payload": {}})

    def test_missing_pipe_is_reported_as_unavailable(self) -> None:
        with self.assertRaises(LocalBrokerUnavailable):
            LocalBrokerClient(_test_pipe_name(), timeout=0.2).call("status")

    def test_oversized_and_malformed_frames_are_refused_and_serving_continues(self) -> None:
        too_long = struct.pack(">I", local_broker.MAX_FRAME_BYTES_DEFAULT + 1)
        response = _raw_exchange(self.name, too_long)
        self.assertEqual(response["ok"], False)
        self.assertIn("size limit", response["error"])
        garbage = b"\xff\xfe not json"
        response = _raw_exchange(self.name, struct.pack(">I", len(garbage)) + garbage)
        self.assertEqual(response["ok"], False)
        self.assertIn("UTF-8 JSON", response["error"])
        not_object = b"[1,2,3]"
        response = _raw_exchange(self.name, struct.pack(">I", len(not_object)) + not_object)
        self.assertEqual(response["ok"], False)
        self.assertIn("JSON object", response["error"])
        outside_envelope = json.dumps({"kind": "status", "token": "x"}).encode()
        response = _raw_exchange(self.name, struct.pack(">I", len(outside_envelope)) + outside_envelope)
        self.assertEqual(response["ok"], False)
        self.assertIn("outside the envelope", response["error"])
        deep = b"[" * 60_000
        response = _raw_exchange(self.name, struct.pack(">I", len(deep)) + deep)
        self.assertEqual(response["ok"], False)
        self.assertIn("nests deeper", response["error"])
        self.assertEqual(self.server.stats["rejected_frame"], 5)
        self.assertEqual(len(self.rejections), 5)
        self.assertEqual(self.requests, [])
        self.assertTrue(self.server.running)
        self.assertEqual(LocalBrokerClient(self.name).call("after"), {"echo": "after", "payload": {}})

    def test_handler_failures_become_refusals_not_crashes(self) -> None:
        client = LocalBrokerClient(self.name)
        with self.assertRaisesRegex(LocalBrokerError, "ValueError: handler refused"):
            client.call("boom")
        with self.assertRaisesRegex(LocalBrokerError, "must return a mapping"):
            client.call("not-a-mapping")
        with self.assertRaisesRegex(LocalBrokerError, "^AttributeError$"):
            client.call("attribute-error")
        with self.assertRaisesRegex(LocalBrokerError, "^OSError$"):
            client.call("os-error")
        with self.assertRaisesRegex(LocalBrokerError, "not JSON-serialisable"):
            client.call("unserialisable")
        with self.assertRaisesRegex(LocalBrokerError, "not JSON-serialisable"):
            client.call("nan")
        with self.assertRaisesRegex(LocalBrokerError, "nests deeper"):
            client.call("deep-result")
        self.assertEqual(self.server.stats["handler_failures"], 4)
        self.assertEqual(self.server.stats["serve_failures"], 0)
        with self.assertRaises(LocalBrokerError):
            client.call("system-exit")
        self.assertEqual(self.server.stats["serve_failures"], 1)
        self.assertTrue(self.server.running)
        self.assertEqual(client.call("ok"), {"echo": "ok", "payload": {}})

    def test_a_peer_that_disconnects_without_sending_is_not_a_rejection(self) -> None:
        for _ in range(3):
            _open_raw(self.name).close()
        self.assertEqual(LocalBrokerClient(self.name).call("after"), {"echo": "after", "payload": {}})
        self.assertEqual(self.server.stats["rejected_frame"], 0)
        self.assertEqual(self.rejections, [])
        self.assertGreaterEqual(self.server.stats["peer_disconnected"], 3)

    def test_a_client_that_connects_and_closes_before_the_server_listens_does_not_wedge_it(self) -> None:
        class SlowListener(LocalBrokerServer):
            def _serve(self) -> None:
                time.sleep(0.3)
                super()._serve()

        name = _test_pipe_name()
        server = SlowListener(name, lambda request: {"ok": request["kind"]})
        server.start()
        self.addCleanup(server.stop)
        for _ in range(3):
            handle = _open_raw(name)
            handle.close()
        self.assertEqual(LocalBrokerClient(name, timeout=5).call("later"), {"ok": "later"})
        self.assertTrue(server.running)

    def test_stop_returns_promptly_with_a_silent_connected_client(self) -> None:
        silent = _open_raw(self.name)
        try:
            time.sleep(0.1)
            started = time.monotonic()
            self.server.stop()
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertFalse(self.server.running)
        finally:
            silent.close()
        with self.assertRaises(LocalBrokerUnavailable):
            LocalBrokerClient(self.name, timeout=0.2).call("status")

    def test_concurrent_clients_are_all_served(self) -> None:
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                client = LocalBrokerClient(self.name, timeout=10)
                for _ in range(4):
                    results.append(client.call("slow", {"index": index}))
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 24)
        self.assertEqual(self.server.stats["served"], 24)

    def test_server_refuses_a_peer_that_is_not_the_current_user(self) -> None:
        real = local_broker._peer_matches_current_user

        def only_client_side_passes(handle, *, server_side):
            return False if server_side else real(handle, server_side=server_side)

        with patch.object(local_broker, "_peer_matches_current_user", only_client_side_passes):
            with self.assertRaisesRegex(LocalBrokerError, "peer identity rejected"):
                LocalBrokerClient(self.name).call("status")
        self.assertEqual(self.server.stats["rejected_peer"], 1)
        self.assertEqual(len(self.rejections), 1)
        self.assertIn("not running as the current user", self.rejections[0])
        self.assertEqual(self.requests, [])
        self.assertEqual(LocalBrokerClient(self.name).call("again"), {"echo": "again", "payload": {}})

    def test_client_refuses_a_server_that_is_not_the_current_user(self) -> None:
        real = local_broker._peer_matches_current_user

        def only_server_side_passes(handle, *, server_side):
            return real(handle, server_side=server_side) if server_side else False

        with patch.object(local_broker, "_peer_matches_current_user", only_server_side_passes):
            with self.assertRaisesRegex(LocalBrokerError, "not served by a process running as this user"):
                LocalBrokerClient(self.name).call("status")
        self.assertEqual(self.requests, [])

    def test_peer_check_uses_the_process_token_user_and_integrity(self) -> None:
        sid = local_broker.current_user_sid()
        self.assertTrue(sid.startswith("S-1-"))
        integrity = local_broker.current_integrity_sid()
        self.assertTrue(integrity.startswith("S-1-16-"))
        with patch.object(local_broker, "_pid_identity", return_value=("S-1-5-21-0-0-0-999", integrity)):
            with self.assertRaises(LocalBrokerError):
                LocalBrokerClient(self.name).call("status")
        with patch.object(local_broker, "_pid_identity", return_value=(sid, "S-1-16-4096")):
            with self.assertRaises(LocalBrokerError):
                LocalBrokerClient(self.name).call("status")
        self.assertEqual(LocalBrokerClient(self.name).call("real"), {"echo": "real", "payload": {}})

    def test_stop_removes_the_pipe_and_is_idempotent(self) -> None:
        self.server.stop()
        self.assertFalse(self.server.running)
        with self.assertRaises(LocalBrokerUnavailable):
            LocalBrokerClient(self.name, timeout=0.2).call("status")
        self.server.stop()


# ---------------------------------------------------------------- real low-IL peer

_LOW_IL_CHILD = '''
import sys, time
sys.dont_write_bytecode = True
name, out_path, root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
results = []
try:
    from jarvis import local_broker as lb
    results.append("integrity:" + lb.current_integrity_sid())
    try:
        lb.LocalBrokerClient(name, timeout=2).call("status")
        results.append("call:served")
    except lb.LocalBrokerError as exc:
        results.append("call:refused")
    try:
        handle = lb._open_client_handle(name, lb.GENERIC_READ)
        lb._Win32.get().kernel32.CloseHandle(handle)
        results.append("read:opened")
    except OSError as exc:
        results.append("read:refused:%d" % int(exc.errno or 0))
except Exception as exc:
    results.append("error:" + type(exc).__name__)
with open(out_path, "a", encoding="utf-8") as fh:
    fh.write("\\n".join(results))
'''


def _spawn_low_integrity_child(script: Path, arguments: list[str], timeout_ms: int = 60_000) -> int:
    """Run the current interpreter at Low integrity (S-1-16-4096) with the given script."""
    import ctypes.wintypes as wt

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    HANDLE, BOOL, DWORD, PVOID = wt.HANDLE, wt.BOOL, wt.DWORD, ctypes.c_void_p
    k.GetCurrentProcess.restype = HANDLE
    k.GetCurrentProcess.argtypes = []
    k.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    k.WaitForSingleObject.restype = DWORD
    k.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    k.GetExitCodeProcess.restype = BOOL
    k.CloseHandle.argtypes = [HANDLE]
    k.CloseHandle.restype = BOOL
    a.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    a.OpenProcessToken.restype = BOOL
    a.DuplicateTokenEx.argtypes = [HANDLE, DWORD, PVOID, ctypes.c_int, ctypes.c_int, ctypes.POINTER(HANDLE)]
    a.DuplicateTokenEx.restype = BOOL
    a.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(PVOID)]
    a.ConvertStringSidToSidW.restype = BOOL
    a.GetLengthSid.argtypes = [PVOID]
    a.GetLengthSid.restype = DWORD
    a.SetTokenInformation.argtypes = [HANDLE, ctypes.c_int, PVOID, DWORD]
    a.SetTokenInformation.restype = BOOL

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
            ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    a.CreateProcessAsUserW.argtypes = [
        HANDLE, wt.LPCWSTR, wt.LPWSTR, PVOID, PVOID, BOOL, DWORD, PVOID, wt.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    a.CreateProcessAsUserW.restype = BOOL

    token = HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 0x2 | 0x8 | 0x80 | 0x1, ctypes.byref(token)):
        raise AssertionError(f"OpenProcessToken failed ({ctypes.get_last_error()})")
    low = HANDLE()
    if not a.DuplicateTokenEx(token, 0xF01FF, None, 2, 1, ctypes.byref(low)):
        raise AssertionError(f"DuplicateTokenEx failed ({ctypes.get_last_error()})")
    sid = PVOID()
    if not a.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):
        raise AssertionError(f"ConvertStringSidToSidW failed ({ctypes.get_last_error()})")
    label = SID_AND_ATTRIBUTES(sid.value, 0x20)
    if not a.SetTokenInformation(low, 25, ctypes.byref(label), ctypes.sizeof(label) + a.GetLengthSid(sid)):
        raise AssertionError(f"SetTokenInformation failed ({ctypes.get_last_error()})")
    command = subprocess.list2cmdline([sys.executable, "-X", "utf8", "-B", str(script), *arguments])
    command_line = ctypes.create_unicode_buffer(command)
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    process = PROCESS_INFORMATION()
    if not a.CreateProcessAsUserW(
        low, None, command_line, None, None, False, 0x08000000, None, None,
        ctypes.byref(startup), ctypes.byref(process),
    ):
        raise AssertionError(f"CreateProcessAsUserW failed ({ctypes.get_last_error()})")
    try:
        k.WaitForSingleObject(process.hProcess, timeout_ms)
        code = DWORD(0)
        k.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        k.CloseHandle(process.hThread)
        k.CloseHandle(process.hProcess)
        k.CloseHandle(low)
        k.CloseHandle(token)


@unittest.skipUnless(WINDOWS, "integrity labels exist only on Windows")
class LowIntegrityPeerTests(unittest.TestCase):
    def test_a_low_integrity_same_user_process_cannot_use_or_even_read_the_pipe(self) -> None:
        name = _test_pipe_name()
        served: list[str] = []
        server = LocalBrokerServer(name, lambda request: served.append(request["kind"]) or {"ok": True})
        server.start()
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "low_child.py"
            script.write_text(_LOW_IL_CHILD, encoding="utf-8")
            output = Path(directory) / "results.txt"
            output.write_text("", encoding="utf-8")
            subprocess.run(
                ["icacls", str(output), "/setintegritylevel", "low"],
                capture_output=True, text=True, check=False,
            )
            code = _spawn_low_integrity_child(script, [name, str(output), str(ROOT)])
            results = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(code, 0, results)
        self.assertIn("integrity:S-1-16-4096", results)
        self.assertIn("call:refused", results)
        self.assertTrue(any(r.startswith("read:refused:") for r in results), results)
        self.assertEqual(served, [])
        self.assertEqual(server.stats["served"], 0)
        # The operator's own (medium-integrity) process is unaffected afterwards.
        self.assertEqual(LocalBrokerClient(name).call("ping"), {"ok": True})
        self.assertRegex(local_broker.pipe_effective_sddl(name), re.compile(r"\(ML;;(NRNW|NWNR);;;"))


if __name__ == "__main__":
    unittest.main()
