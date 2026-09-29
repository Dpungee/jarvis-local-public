from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis import provider_setup
from jarvis.config import _DOTENV_KEYS
from jarvis.installer import (
    INSTALLER_FEATURES,
    InstallerError,
    apply_feature_mode,
    persist_feature_updates,
    run_guided_install,
)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-installer-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_catalog_is_unique_and_only_writes_supported_nonsecret_settings(self):
        ids = [feature.feature_id for feature in INSTALLER_FEATURES]
        self.assertEqual(len(ids), len(set(ids)))
        keys = {
            key
            for feature in INSTALLER_FEATURES
            for mode in feature.modes
            for key, _value in mode.values
        }
        self.assertLessEqual(keys, _DOTENV_KEYS)
        self.assertFalse(any("TOKEN" in key or key.endswith("API_KEY") for key in keys))

    def test_recommended_rerun_preserves_unmanaged_settings_and_reviews_every_area(self):
        provider_setup.persist_provider_choice("codex", self.root)
        env_path = self.root / ".env"
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write("JARVIS_COMMAND_TIMEOUT=77\n")
        output = io.StringIO()

        result = run_guided_install(
            root=self.root,
            environ={},
            input_fn=Mock(side_effect=["1", ""]),
            output=output,
        )

        saved = env_path.read_text(encoding="utf-8")
        self.assertEqual(result["style"], "recommended")
        self.assertEqual(set(result["features"]), {item.feature_id for item in INSTALLER_FEATURES})
        self.assertIn("JARVIS_COMMAND_TIMEOUT=77", saved)
        self.assertIn("JARVIS_INITIATIVE=observe", saved)
        self.assertIn("JARVIS_SELF_INSPECT=read-only", saved)
        self.assertIn("JARVIS_EXTERNAL_ACCESS=disabled", saved)
        self.assertIn("Included in every installation", output.getvalue())
        self.assertFalse(list(self.root.glob(".jarvis-install-*.tmp")))

    def test_legacy_data_without_explicit_provider_still_runs_provider_review(self):
        data_dir = self.root / "data"
        data_dir.mkdir()
        connection = sqlite3.connect(data_dir / "jarvis.db")
        try:
            connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO memories DEFAULT VALUES")
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(provider_setup.is_setup_complete(self.root))
        self.assertFalse(provider_setup.has_provider_configuration(self.root))

        def configure(*_args, **_kwargs):
            provider_setup.persist_provider_choice("codex", self.root)
            return provider_setup.ProviderSetupResult("configured", "codex", self.root / ".env")

        with patch("jarvis.installer.select_provider_interactive", side_effect=configure) as chooser:
            run_guided_install(
                root=self.root,
                environ={},
                input_fn=Mock(side_effect=["1"]),
                output=io.StringIO(),
            )

        chooser.assert_called_once()
        self.assertTrue(provider_setup.has_provider_configuration(self.root))

    def test_trusted_desktop_expands_to_bounded_user_home(self):
        apply_feature_mode("computer-control", "desktop", self.root)
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("JARVIS_EXECUTION_MODE=trusted-host", saved)
        self.assertIn("JARVIS_COMPUTER_ACCESS=trusted-desktop", saved)
        self.assertIn(f"JARVIS_COMPUTER_ROOT={Path.home().resolve()}", saved)

    def test_unmanaged_or_multiline_values_fail_without_writing(self):
        with self.assertRaises(InstallerError):
            persist_feature_updates({"OPENAI_API_KEY": "secret"}, self.root)
        with self.assertRaises(InstallerError):
            persist_feature_updates({"JARVIS_PROACTIVE_ENABLED": "true\nBAD=1"}, self.root)
        self.assertFalse((self.root / ".env").exists())

    def test_grok_provider_needs_explicit_key_but_never_persists_it(self):
        with self.assertRaises(provider_setup.ProviderSetupRequired):
            provider_setup.configure_provider("grok", self.root, environ={})

        provider_setup.configure_provider(
            "grok",
            self.root,
            environ={"XAI_API_KEY": "xai-test-key-not-real"},
        )
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("JARVIS_XAI_API_ENABLED=true", saved)
        self.assertIn("JARVIS_FAST_MODEL=xai:grok-4.6", saved)
        self.assertNotIn("xai-test-key-not-real", saved)
        self.assertNotIn("XAI_API_KEY=", saved)


if __name__ == "__main__":
    unittest.main()
