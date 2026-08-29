from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from jarvis.public_presence_service import (
    PublicControlState,
    PublicPresenceService,
    PublicPresenceUnavailable,
    control_state_from_mapping,
    durable_control_reader,
    environment_enabled,
    main,
    public_presence_database_path,
)
from jarvis.public_presence_store import PublicPresenceStore


class PublicPresenceServiceTests(unittest.TestCase):
    def test_installed_layout_uses_per_user_data_not_site_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed_root = root / "site-packages"
            local_app_data = root / "LocalAppData"
            installed_root.mkdir()
            with (
                patch(
                    "jarvis.public_presence_service._SOURCE_ROOT",
                    installed_root,
                ),
                patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": str(local_app_data)},
                    clear=False,
                ),
            ):
                os.environ.pop("JARVIS_DATA", None)
                database = public_presence_database_path()

        self.assertEqual(
            database,
            (local_app_data / "JarvisLocal" / "data" / "public_presence.db").resolve(),
        )
        self.assertFalse(database.is_relative_to(installed_root.resolve()))

    def test_disabled_and_missing_controls_fail_closed(self):
        service = PublicPresenceService()
        with self.assertRaisesRegex(PublicPresenceUnavailable, "disabled"):
            service.start()
        self.assertEqual(service.status()["state"], "disabled")
        self.assertFalse(service.status()["external_communication"])

    def test_start_requires_both_explicit_enable_and_permissive_controls(self):
        paused = PublicPresenceService(
            enabled=True,
            control_reader=lambda: PublicControlState(
                social_paused=True,
                emergency_stopped=False,
                mode="observe",
            ),
        )
        with self.assertRaisesRegex(PublicPresenceUnavailable, "paused"):
            paused.start()

        stopped = PublicPresenceService(
            enabled=True,
            control_reader=lambda: PublicControlState(
                social_paused=False,
                emergency_stopped=True,
                mode="observe",
            ),
        )
        with self.assertRaisesRegex(PublicPresenceUnavailable, "emergency"):
            stopped.start()

        offline = PublicPresenceService(
            enabled=True,
            control_reader=lambda: PublicControlState(
                social_paused=False,
                emergency_stopped=False,
                mode="offline",
            ),
        )
        with self.assertRaisesRegex(PublicPresenceUnavailable, "mode"):
            offline.start()

    def test_control_change_stops_running_service_without_leaking_machine_state(self):
        control = [PublicControlState(
            social_paused=False,
            emergency_stopped=False,
            mode="suggest",
            revision=1,
        )]
        service = PublicPresenceService(
            enabled=True,
            control_reader=lambda: control[0],
            advertised_tools=("moltbook_status",),
            clock=lambda: 123.0,
        )
        status = service.start()
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["started_at"], 123.0)
        control[0] = PublicControlState(
            social_paused=True,
            emergency_stopped=False,
            mode="suggest",
            revision=2,
        )
        status = service.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["state"], "paused")
        serialized = json.dumps(status).casefold()
        for private_field in ("hostname", "username", "pid", "path", "database"):
            self.assertNotIn(private_field, serialized)

    def test_environment_flag_is_strict_and_module_entrypoint_is_status_only(self):
        self.assertFalse(environment_enabled("false"))
        self.assertFalse(environment_enabled("enabled"))
        self.assertTrue(environment_enabled("true"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "JARVIS_DATA": directory,
                "JARVIS_PUBLIC_PRESENCE_ENABLED": "false",
            },
            clear=False,
        ):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["health"]), 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["state"], "disabled")

    def test_entrypoint_reads_the_same_durable_public_controls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "JARVIS_DATA": directory,
                "JARVIS_PUBLIC_PRESENCE_ENABLED": "true",
            },
            clear=False,
        ):
            database = Path(directory) / "public_presence.db"
            store = PublicPresenceStore(database)

            # The environment flag alone is insufficient: the durable row starts
            # disabled and paused, so the process cannot report ready.
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status"]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "paused")

            store.set_enabled(True, actor="test:operator")
            store.set_paused(False, actor="test:operator")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status"]), 0)
            ready = json.loads(output.getvalue())
            self.assertEqual(ready["state"], "ready")
            self.assertFalse(ready["running"])
            self.assertFalse(ready["external_communication"])
            self.assertEqual(ready["connected_platforms"], [])

            store.set_paused(True, actor="test:operator")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status"]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "paused")

            store.emergency_stop(actor="test:operator")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status"]), 0)
            self.assertEqual(
                json.loads(output.getvalue())["state"], "emergency_stopped"
            )

    def test_control_reader_is_public_database_only_and_does_not_touch_private_db(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            private = data / "jarvis.db"
            sentinel = b"private-memory-sentinel"
            private.write_bytes(sentinel)
            reader = durable_control_reader(data / "public_presence.db")
            self.assertFalse(reader().allows_start)
            self.assertEqual(private.read_bytes(), sentinel)
            with self.assertRaisesRegex(ValueError, "public_presence.db"):
                durable_control_reader(private)

    def test_public_database_path_is_bounded_and_never_a_filesystem_root(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory).resolve() / "public_presence.db"
            self.assertEqual(public_presence_database_path(directory), expected)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            public_presence_database_path("")
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            public_presence_database_path(Path.cwd().anchor)
        with self.assertRaisesRegex(ValueError, "bounded"):
            public_presence_database_path("a" * 4_097)

    def test_entrypoint_exposes_no_start_or_publish_command(self):
        for command in ("start", "publish", "listen"):
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    main([command])

    def test_entrypoint_reports_corrupt_control_store_as_unavailable_without_details(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "JARVIS_DATA": directory,
                "JARVIS_PUBLIC_PRESENCE_ENABLED": "true",
            },
            clear=False,
        ):
            database = Path(directory) / "public_presence.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE private_looking_table(secret TEXT)")
                connection.commit()
            finally:
                connection.close()
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status"]), 1)
            rendered = output.getvalue()
            payload = json.loads(rendered)
            self.assertEqual(payload["state"], "unavailable")
            self.assertTrue(payload["social_paused"])
            self.assertTrue(payload["emergency_stopped"])
            self.assertFalse(payload["external_communication"])
            self.assertNotIn(directory, rendered)
            self.assertNotIn("private_looking_table", rendered)

    def test_service_refuses_to_advertise_private_or_mutating_tools(self):
        with self.assertRaisesRegex(ValueError, "non-public"):
            PublicPresenceService(advertised_tools=("shell",))

    def test_independent_store_control_mapping_fails_closed(self):
        disabled = control_state_from_mapping({
            "enabled": False,
            "paused": False,
            "emergency_stopped": False,
            "updated_by": "untrusted and ignored",
        })
        self.assertFalse(disabled.allows_start)
        self.assertEqual(disabled.mode, "offline")
        enabled = control_state_from_mapping({
            "enabled": True,
            "paused": False,
            "emergency_stopped": False,
        }, active_mode="suggest")
        self.assertTrue(enabled.allows_start)
        with self.assertRaises(ValueError):
            control_state_from_mapping({
                "enabled": "yes",
                "paused": False,
                "emergency_stopped": False,
            })


if __name__ == "__main__":
    unittest.main()
