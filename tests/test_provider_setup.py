from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import jarvis.config as config_module
from jarvis import provider_setup
from jarvis.config import Config


ROOT = Path(__file__).resolve().parents[1]


class ProviderSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-provider-setup-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _ready(provider: str) -> provider_setup.CLIProbe:
        return provider_setup.CLIProbe(
            provider=provider,
            installed=True,
            runnable=True,
            authenticated=True,
            executable=Path(f"C:/{provider}.exe"),
        )

    def test_existing_env_or_used_database_preserves_historical_installation(self) -> None:
        (self.root / ".env").write_text("JARVIS_OLLAMA_ENABLED=true\n", encoding="utf-8")
        self.assertTrue(provider_setup.is_setup_complete(self.root, environ={}))

        (self.root / ".env").unlink()
        data = self.root / "data"
        data.mkdir()
        with closing(sqlite3.connect(data / "jarvis.db")) as connection:
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO messages DEFAULT VALUES")
            connection.commit()
        self.assertTrue(provider_setup.is_setup_complete(self.root, environ={}))

    def test_fresh_runtime_database_does_not_suppress_provider_setup(self) -> None:
        data = self.root / "data"
        data.mkdir()
        with closing(sqlite3.connect(data / "jarvis.db")) as connection:
            connection.execute("CREATE TABLE agent_projects (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO agent_projects DEFAULT VALUES")
            connection.execute("CREATE TABLE runtime_control (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO runtime_control DEFAULT VALUES")
            connection.execute("CREATE TABLE self_snapshots (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO self_snapshots DEFAULT VALUES")
            connection.execute("CREATE TABLE specialist_agents (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO specialist_agents DEFAULT VALUES")
            connection.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
            connection.commit()
        self.assertFalse(provider_setup.is_setup_complete(self.root, environ={}))

    def test_explicit_process_configuration_counts_as_migrated(self) -> None:
        self.assertTrue(
            provider_setup.is_setup_complete(
                self.root,
                environ={"JARVIS_FAST_MODEL": "claude-cli:haiku"},
            )
        )
        self.assertTrue(
            provider_setup.is_setup_complete(
                self.root,
                environ={"OPENAI_API_KEY": "presence-only-not-read"},
            )
        )

    def test_api_key_only_install_is_ready_without_cli_or_prompt(self) -> None:
        input_fn = Mock(side_effect=AssertionError("API-key setup must not prompt"))
        result = provider_setup.ensure_ready(
            False,
            self.root,
            environ={"OPENAI_API_KEY": "presence-only-not-read"},
            input_fn=input_fn,
            stdin_isatty=False,
        )
        self.assertEqual(result.state, "existing")
        input_fn.assert_not_called()

    def test_empty_api_key_environment_does_not_skip_first_run_setup(self) -> None:
        for value in ("", "   ", "\t"):
            with self.subTest(value=repr(value)):
                self.assertFalse(
                    provider_setup.is_setup_complete(
                        self.root,
                        environ={
                            "OPENAI_API_KEY": value,
                            "ANTHROPIC_API_KEY": value,
                        },
                    )
                )

    def test_headless_first_run_fails_before_reading_input(self) -> None:
        input_fn = Mock(side_effect=AssertionError("headless setup must not prompt"))
        with self.assertRaisesRegex(provider_setup.ProviderSetupRequired, "provider setup"):
            provider_setup.ensure_ready(
                False,
                self.root,
                environ={},
                input_fn=input_fn,
                stdin_isatty=False,
            )
        input_fn.assert_not_called()
        self.assertFalse((self.root / ".env").exists())

    def test_interactive_codex_setup_persists_verified_non_secret_choice(self) -> None:
        output = io.StringIO()
        with patch.object(
            provider_setup,
            "detect_provider",
            side_effect=lambda name, **_kwargs: self._ready(name),
        ):
            result = provider_setup.ensure_ready(
                True,
                self.root,
                environ={},
                input_fn=Mock(side_effect=["1"]),
                output=output,
                stdin_isatty=True,
            )

        self.assertEqual(result.state, "configured")
        self.assertEqual(result.choice, "codex")
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("JARVIS_CODEX_CLI_ENABLED=true", saved)
        self.assertIn("JARVIS_CLAUDE_CLI_ENABLED=false", saved)
        self.assertIn("JARVIS_OPENAI_API_ENABLED=false", saved)
        self.assertIn("JARVIS_ANTHROPIC_API_ENABLED=false", saved)
        self.assertIn("JARVIS_FAST_MODEL=codex-cli:gpt-5.6-luna", saved)
        self.assertIn("JARVIS_REASONING_MODEL=codex-cli:gpt-5.6-terra", saved)
        self.assertIn("JARVIS_CODING_MODEL=codex-cli:gpt-5.6-sol", saved)
        self.assertIn("JARVIS_DEEP_MODEL=codex-cli:gpt-5.6-sol", saved)
        self.assertIn("JARVIS_BACKGROUND_MODEL=codex-cli:gpt-5.6-luna", saved)
        self.assertIn("JARVIS_OLLAMA_ENABLED=false", saved)
        self.assertNotIn("API_KEY", saved)
        self.assertIn("will not ask again", output.getvalue())

    def test_both_routes_fast_work_to_claude_and_coding_to_codex(self) -> None:
        provider_setup.persist_provider_choice("both", self.root)
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("JARVIS_CODEX_CLI_ENABLED=true", saved)
        self.assertIn("JARVIS_CLAUDE_CLI_ENABLED=true", saved)
        self.assertIn("JARVIS_OPENAI_API_ENABLED=false", saved)
        self.assertIn("JARVIS_ANTHROPIC_API_ENABLED=false", saved)
        self.assertIn("JARVIS_FAST_MODEL=claude-cli:haiku", saved)
        self.assertIn("JARVIS_REASONING_MODEL=claude-cli:sonnet", saved)
        self.assertIn("JARVIS_CODING_MODEL=codex-cli:gpt-5.6-sol", saved)
        self.assertIn("JARVIS_DEEP_MODEL=codex-cli:gpt-5.6-sol", saved)
        self.assertIn("JARVIS_BACKGROUND_MODEL=claude-cli:haiku", saved)

        with (
            patch.object(config_module, "ROOT", self.root),
            patch.dict(
                os.environ,
                {
                    "JARVIS_SOUL": str(config_module.PACKAGED_SOUL),
                    "JARVIS_CONSTITUTION": str(config_module.PACKAGED_CONSTITUTION),
                },
                clear=True,
            ),
        ):
            configured = Config.load()
        self.assertTrue(configured.codex_cli_enabled)
        self.assertTrue(configured.claude_cli_enabled)
        self.assertFalse(configured.ollama_enabled)
        self.assertEqual(configured.coding_model, "codex-cli:gpt-5.6-sol")

    def test_atomic_update_preserves_unmanaged_lines_and_removes_managed_duplicates(self) -> None:
        original = (
            "# keep this comment\r\n"
            "JARVIS_COMMAND_TIMEOUT=77\r\n"
            "JARVIS_FAST_MODEL=old-one\r\n"
            "JARVIS_FAST_MODEL=old-two\r\n"
            "JARVIS_AUTONOMY=readonly\r\n"
        )
        env_path = self.root / ".env"
        env_path.write_text(original, encoding="utf-8", newline="")

        provider_setup.persist_provider_choice("claude", self.root)

        saved = env_path.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", saved)
        self.assertIn("JARVIS_COMMAND_TIMEOUT=77", saved)
        self.assertIn("JARVIS_AUTONOMY=readonly", saved)
        self.assertEqual(saved.count("JARVIS_FAST_MODEL="), 1)
        self.assertIn("JARVIS_FAST_MODEL=claude-cli:haiku", saved)
        self.assertFalse(list(self.root.glob(".jarvis-provider-*.tmp")))

    def test_existing_env_is_not_rewritten_by_automatic_first_run(self) -> None:
        env_path = self.root / ".env"
        before = b"# operator config\nJARVIS_FAST_MODEL=custom-local\n"
        env_path.write_bytes(before)
        result = provider_setup.ensure_ready(
            True,
            self.root,
            environ={},
            input_fn=Mock(side_effect=AssertionError("must not prompt")),
            stdin_isatty=True,
        )
        self.assertEqual(result.state, "existing")
        self.assertEqual(env_path.read_bytes(), before)

    def test_non_regular_env_is_rejected_without_replacement(self) -> None:
        (self.root / ".env").mkdir()
        with self.assertRaisesRegex(provider_setup.ProviderSetupError, "ordinary"):
            provider_setup.persist_provider_choice("codex", self.root)

    def test_declining_missing_provider_leaves_setup_incomplete(self) -> None:
        missing = provider_setup.CLIProbe("codex", False, False, False)
        answers = ["1"] + (["no"] if os.name == "nt" else [])
        with patch.object(provider_setup, "detect_provider", return_value=missing):
            with self.assertRaises(provider_setup.ProviderSetupRequired):
                provider_setup.ensure_ready(
                    True,
                    self.root,
                    environ={},
                    input_fn=Mock(side_effect=answers),
                    output=io.StringIO(),
                    stdin_isatty=True,
                )
        self.assertFalse((self.root / ".env").exists())

    def test_login_is_verified_before_choice_is_written(self) -> None:
        executable = Path("C:/claude.exe")
        probes = [
            provider_setup.CLIProbe("claude", True, True, False, executable),
            self._ready("claude"),
        ]
        with (
            patch.object(provider_setup, "detect_provider", side_effect=probes),
            patch.object(provider_setup, "_login_provider", return_value=True) as login,
        ):
            provider_setup.ensure_ready(
                True,
                self.root,
                environ={},
                input_fn=Mock(side_effect=["2", "yes"]),
                output=io.StringIO(),
                stdin_isatty=True,
            )
        login.assert_called_once()
        self.assertTrue((self.root / ".env").is_file())

    def test_auth_probe_discards_cli_output_and_never_reads_session_files(self) -> None:
        executable = self.root / ("codex.exe" if os.name == "nt" else "codex")
        executable.write_bytes(b"MZ" if os.name == "nt" else b"\x7fELF")
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, b"Logged in using ChatGPT\n", b""
            )
        )
        with patch.object(provider_setup, "_native_candidates", return_value=[executable]):
            probe = provider_setup.detect_provider("codex", environ={}, runner=runner)
        self.assertTrue(probe.authenticated)
        args, options = runner.call_args
        self.assertEqual(args[0][-2:], ["login", "status"])
        self.assertIn('cli_auth_credentials_store="keyring"', args[0])
        self.assertIn("CODEX_HOME", options["env"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertIs(options["stderr"], subprocess.PIPE)
        self.assertNotIn("auth.json", " ".join(args[0]))

    def test_codex_api_key_login_does_not_qualify_as_chatgpt_subscription(self) -> None:
        executable = self.root / ("codex.exe" if os.name == "nt" else "codex")
        executable.write_bytes(b"MZ" if os.name == "nt" else b"\x7fELF")
        for status in (
            b"Logged in using an API key\n",
            b"Logged in\n",
            b"Not logged in using ChatGPT\n",
            b"Logged in using ChatGPT\n" + b"x" * provider_setup._AUTH_STATUS_MAX_BYTES,
        ):
            with self.subTest(status=status[:40]):
                runner = Mock(
                    return_value=subprocess.CompletedProcess([], 0, status, b"")
                )
                with patch.object(
                    provider_setup, "_native_candidates", return_value=[executable]
                ):
                    probe = provider_setup.detect_provider(
                        "codex", environ={}, runner=runner
                    )
                self.assertTrue(probe.runnable)
                self.assertFalse(probe.authenticated)

    def test_claude_auth_probe_remains_exit_code_based_and_private(self) -> None:
        executable = self.root / ("claude.exe" if os.name == "nt" else "claude")
        executable.write_bytes(b"MZ" if os.name == "nt" else b"\x7fELF")
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        with patch.object(provider_setup, "_native_candidates", return_value=[executable]):
            probe = provider_setup.detect_provider("claude", environ={}, runner=runner)
        self.assertTrue(probe.authenticated)
        _args, options = runner.call_args
        self.assertIs(options["stdout"], subprocess.DEVNULL)
        self.assertIs(options["stderr"], subprocess.DEVNULL)

    @unittest.skipUnless(os.name == "nt", "Windows Package Manager path is Windows-only")
    def test_windows_install_uses_exact_official_package_and_no_provider_keys(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        environment = {
            "PATH": "C:\\Windows",
            "USERPROFILE": "C:\\Users\\operator",
            "OPENAI_API_KEY": "must-not-cross",
            "ANTHROPIC_API_KEY": "must-not-cross",
        }
        with patch.object(provider_setup.shutil, "which", return_value="C:/Windows/winget.exe"):
            installed = provider_setup._install_provider(
                "codex", environ=environment, runner=runner
            )
        self.assertTrue(installed)
        args, options = runner.call_args
        self.assertEqual(
            args[0],
            [
                "C:/Windows/winget.exe",
                "install",
                "--id",
                "OpenAI.Codex",
                "--exact",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
        )
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", options["env"])

    def test_login_inherits_terminal_but_not_ambient_provider_keys(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        probe = provider_setup.CLIProbe(
            "claude", True, True, False, Path("C:/trusted/claude.exe")
        )
        environment = {
            "PATH": "C:\\trusted",
            "USERPROFILE": "C:\\Users\\operator",
            "OPENAI_API_KEY": "must-not-cross",
            "ANTHROPIC_API_KEY": "must-not-cross",
        }
        self.assertTrue(
            provider_setup._login_provider(
                probe, environ=environment, runner=runner
            )
        )
        args, options = runner.call_args
        self.assertEqual(args[0][1:], ["auth", "login"])
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", options["env"])

    def test_explicit_login_bypasses_existing_install_short_circuit(self) -> None:
        result = provider_setup.ProviderSetupResult(
            "configured", "codex", self.root / ".env"
        )
        output = io.StringIO()
        with (
            patch.object(provider_setup.sys.stdin, "isatty", return_value=True),
            patch.object(provider_setup.sys, "stdout", output),
            patch.object(provider_setup, "_prepare_provider") as prepare,
            patch.object(
                provider_setup, "configure_provider", return_value=result
            ) as configure,
        ):
            status = provider_setup.main(["--login", "codex"])
        self.assertEqual(status, 0)
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[0], "codex")
        configure.assert_called_once_with("codex")
        self.assertIn("login and selection saved: codex", output.getvalue())

    def test_explicit_login_fails_before_prompting_when_headless(self) -> None:
        error = io.StringIO()
        with (
            patch.object(provider_setup.sys.stdin, "isatty", return_value=False),
            patch.object(provider_setup.sys, "stderr", error),
            patch.object(provider_setup, "_prepare_provider") as prepare,
        ):
            status = provider_setup.main(["--login", "codex"])
        self.assertEqual(status, 2)
        prepare.assert_not_called()
        self.assertIn("provider setup is required", error.getvalue())

    def test_windows_launchers_gate_before_starting_jarvis(self) -> None:
        for name in ("start_jarvis.bat", "start_jarvis_ui.bat"):
            source = (ROOT / name).read_text(encoding="utf-8")
            gate = source.index("jarvis.provider_setup --interactive")
            launch = source.rindex("-m jarvis")
            self.assertLess(gate, launch, name)
        presence = (ROOT / "start_jarvis_presence.ps1").read_text(encoding="utf-8")
        self.assertIn("jarvis.provider_setup --interactive", presence)
        self.assertLess(
            presence.index("jarvis.provider_setup --interactive"),
            presence.index("Start-Process"),
        )
        setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn('"jarvis.provider_setup", "--interactive"', setup)


if __name__ == "__main__":
    unittest.main()
