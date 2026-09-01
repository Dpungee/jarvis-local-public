from __future__ import annotations

import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.presence import PresenceHTTPServer, run_presence


def _presence_config(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        presence_host="127.0.0.1",
        presence_port=port,
        presence_trusted_hosts=(),
        presence_remote_access="disabled",
        screen_companion_indicator=False,
    )


class _TrackedRuntime:
    instances: list[_TrackedRuntime] = []

    def __init__(self, _config: object) -> None:
        self.starts = 0
        self.shutdowns = 0
        self.instances.append(self)

    def start(self) -> None:
        self.starts += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


class _FailingRuntime(_TrackedRuntime):
    def start(self) -> None:
        self.starts += 1
        raise RuntimeError("partial startup failed")


class _TrackedServer:
    instances: list[_TrackedServer] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.closed = 0
        self.served = 0
        self.runtime: object | None = None
        self.instances.append(self)

    def attach_runtime(self, runtime: object) -> None:
        self.runtime = runtime

    def serve_forever(self, *, poll_interval: float) -> None:
        del poll_interval
        self.served += 1

    def server_close(self) -> None:
        self.closed += 1


class PresenceStartupLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _TrackedRuntime.instances.clear()
        _TrackedServer.instances.clear()

    def test_second_runtime_does_not_start_when_first_server_owns_port(self) -> None:
        first_runtime = SimpleNamespace()
        first_server = PresenceHTTPServer(("127.0.0.1", 0), first_runtime)
        try:
            port = int(first_server.server_port)
            with (
                patch("jarvis.presence.Config.load", return_value=_presence_config(port)),
                patch("jarvis.presence.PresenceRuntime", _TrackedRuntime),
            ):
                with self.assertRaises(OSError):
                    run_presence(open_browser=False)

            self.assertEqual(_TrackedRuntime.instances, [])
        finally:
            first_server.server_close()

    def test_partial_runtime_start_failure_closes_server_and_runtime(self) -> None:
        with (
            patch("jarvis.presence.Config.load", return_value=_presence_config(8787)),
            patch("jarvis.presence.PresenceRuntime", _FailingRuntime),
            patch("jarvis.presence.PresenceHTTPServer", _TrackedServer),
        ):
            with self.assertRaisesRegex(RuntimeError, "partial startup failed"):
                run_presence(open_browser=False)

        runtime = _FailingRuntime.instances[0]
        server = _TrackedServer.instances[0]
        self.assertEqual(runtime.starts, 1)
        self.assertEqual(runtime.shutdowns, 1)
        self.assertEqual(server.served, 0)
        self.assertEqual(server.closed, 1)

    def test_server_configuration_is_validated_before_bind(self) -> None:
        with patch.object(ThreadingHTTPServer, "__init__") as base_init:
            with self.assertRaisesRegex(ValueError, "remote access mode"):
                PresenceHTTPServer(
                    ("127.0.0.1", 8787),
                    SimpleNamespace(),
                    remote_access="invalid",
                )
        base_init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
