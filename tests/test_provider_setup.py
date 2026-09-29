from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
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

    def test_ambient_process_configuration_does_not_suppress_local_setup(self) -> None:
        self.assertFalse(
            provider_setup.is_setup_complete(
                self.root,
                environ={
                    "JARVIS_FAST_MODEL": provider_setup._PINNED_CLAUDE_SONNET_4_5_MODEL
                },
            )
        )
        self.assertFalse(
            provider_setup.is_setup_complete(
                self.root,
                environ={"OPENAI_API_KEY": "presence-only-not-read"},
            )
        )

    def test_api_key_only_headless_install_requires_local_setup_without_prompting(self) -> None:
        input_fn = Mock(side_effect=AssertionError("headless setup must not prompt"))
        with self.assertRaisesRegex(provider_setup.ProviderSetupRequired, "provider setup"):
            provider_setup.ensure_ready(
                False,
                self.root,
                environ={"OPENAI_API_KEY": "presence-only-not-read"},
                input_fn=input_fn,
                stdin_isatty=False,
            )
        input_fn.assert_not_called()

    def test_copied_template_provider_values_do_not_suppress_provider_review(self) -> None:
        copied = """\
JARVIS_WORKSPACE=C:\\custom-workspace
JARVIS_MODEL=auto
JARVIS_FAST_MODEL=qwen3.5:9b
JARVIS_REASONING_MODEL=gpt-oss:20b
JARVIS_CODING_MODEL=qwen3-coder:30b
JARVIS_DEEP_MODEL=qwen3-coder:30b
JARVIS_BACKGROUND_MODEL=fast
JARVIS_OLLAMA_ENABLED=true
JARVIS_OPENAI_API_ENABLED=false
JARVIS_ANTHROPIC_API_ENABLED=false
JARVIS_CODEX_CLI_ENABLED=false
JARVIS_CLAUDE_CLI_ENABLED=false
"""
        (self.root / ".env").write_text(copied, encoding="utf-8")
        self.assertFalse(provider_setup.is_setup_complete(self.root, environ={}))

    def test_wizard_marker_distinguishes_saved_choice_from_template_defaults(self) -> None:
        provider_setup.persist_provider_choice("codex", self.root)
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn(provider_setup._SETUP_MARKER, saved)
        self.assertTrue(provider_setup.is_setup_complete(self.root, environ={}))

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

    def test_ollama_choice_restores_local_profiles_without_credentials(self) -> None:
        with patch.object(
            provider_setup,
            "detect_provider",
            side_effect=AssertionError("local selection must not probe a subscription CLI"),
        ):
            result = provider_setup.configure_provider("ollama", self.root)

        self.assertEqual(result.choice, "ollama")
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("JARVIS_OLLAMA_ENABLED=true", saved)
        self.assertIn("JARVIS_CLOUD_ENABLED=false", saved)
        self.assertIn("JARVIS_CODEX_CLI_ENABLED=false", saved)
        self.assertIn("JARVIS_CLAUDE_CLI_ENABLED=false", saved)
        self.assertIn("JARVIS_FAST_MODEL=qwen3.5:9b", saved)
        self.assertIn("JARVIS_CODING_MODEL=qwen3-coder:30b", saved)
        self.assertNotIn("API_KEY", saved)

    def test_first_run_can_choose_ollama_without_subscription_setup(self) -> None:
        with (
            patch.object(
                provider_setup,
                "detect_provider",
                side_effect=AssertionError("Ollama setup must not probe a subscription CLI"),
            ),
            patch.object(
                provider_setup,
                "_prepare_provider",
                side_effect=AssertionError("Ollama setup must not launch a CLI login"),
            ),
        ):
            result = provider_setup.ensure_ready(
                True,
                self.root,
                environ={},
                input_fn=Mock(side_effect=["4"]),
                output=io.StringIO(),
                stdin_isatty=True,
            )

        self.assertEqual(result.choice, "ollama")
        self.assertIn(
            "JARVIS_OLLAMA_ENABLED=true",
            (self.root / ".env").read_text(encoding="utf-8"),
        )

    def test_first_run_can_choose_grok_api_without_subscription_setup(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                provider_setup,
                "detect_provider",
                side_effect=AssertionError("API setup must not probe a subscription CLI"),
            ),
            patch.object(
                provider_setup,
                "_prepare_provider",
                side_effect=AssertionError("API setup must not launch a CLI login"),
            ),
        ):
            result = provider_setup.ensure_ready(
                True,
                self.root,
                environ={"XAI_API_KEY": "xai-test-key-not-real"},
                input_fn=Mock(side_effect=["7"]),
                output=output,
                stdin_isatty=True,
            )

        self.assertEqual(result.choice, "grok")
        self.assertIn("7. Grok through the xAI API", output.getvalue())
        saved = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("JARVIS_XAI_API_ENABLED=true", saved)
        self.assertIn("JARVIS_OPENAI_API_ENABLED=false", saved)
        self.assertIn("JARVIS_ANTHROPIC_API_ENABLED=false", saved)
        self.assertIn("JARVIS_OLLAMA_ENABLED=false", saved)
        self.assertIn("JARVIS_CODING_MODEL=xai:grok-4.6", saved)
        self.assertNotIn("xai-test-key-not-real", saved)
        self.assertTrue(provider_setup.has_provider_configuration(self.root))

    def test_api_choice_without_its_key_is_refused_before_anything_is_saved(self) -> None:
        for choice, answer, variable in (
            ("openai-api", "5", "OPENAI_API_KEY"),
            ("anthropic-api", "6", "ANTHROPIC_API_KEY"),
            ("grok", "7", "XAI_API_KEY"),
        ):
            with self.subTest(choice=choice):
                with self.assertRaisesRegex(provider_setup.ProviderSetupRequired, variable):
                    provider_setup.configure_provider(choice, self.root, environ={})
                with self.assertRaisesRegex(provider_setup.ProviderSetupRequired, variable):
                    provider_setup.ensure_ready(
                        True,
                        self.root,
                        environ={},
                        input_fn=Mock(side_effect=[answer]),
                        output=io.StringIO(),
                        stdin_isatty=True,
                    )
                self.assertFalse((self.root / ".env").exists())

    def test_api_choices_enable_only_their_own_billed_adapter(self) -> None:
        expected = {
            "openai-api": ("JARVIS_OPENAI_API_ENABLED", "openai:gpt-5.6-sol"),
            "anthropic-api": ("JARVIS_ANTHROPIC_API_ENABLED", "anthropic:claude-sonnet-5"),
            "grok": ("JARVIS_XAI_API_ENABLED", "xai:grok-4.6"),
        }
        switches = {switch for switch, _model in expected.values()}
        for choice, (switch, coding_model) in expected.items():
            with self.subTest(choice=choice):
                values = provider_setup._provider_values(choice)
                self.assertEqual(values[switch], "true")
                for other in switches - {switch}:
                    self.assertEqual(values[other], "false")
                self.assertEqual(values["JARVIS_CODEX_CLI_ENABLED"], "false")
                self.assertEqual(values["JARVIS_CLAUDE_CLI_ENABLED"], "false")
                self.assertEqual(values["JARVIS_CLOUD_ENABLED"], "true")
                self.assertEqual(values["JARVIS_OLLAMA_ENABLED"], "false")
                self.assertEqual(values["JARVIS_CODING_MODEL"], coding_model)
        for choice in ("codex", "claude", "both", "ollama"):
            with self.subTest(choice=choice):
                values = provider_setup._provider_values(choice)
                for switch in switches:
                    self.assertEqual(values[switch], "false")

    def test_grok_choice_round_trips_through_config(self) -> None:
        configured = SimpleNamespace(
            codex_cli_enabled=False,
            claude_cli_enabled=False,
            ollama_enabled=False,
            xai_api_enabled=True,
            openai_api_enabled=False,
            anthropic_api_enabled=False,
        )
        self.assertEqual(provider_setup.provider_choice_from_config(configured), "grok")

    def test_template_without_xai_switch_is_still_the_unchanged_template(self) -> None:
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        legacy = "\n".join(
            line for line in template.splitlines()
            if not line.strip().startswith("JARVIS_XAI_API_ENABLED")
        )
        self.assertFalse(provider_setup._has_completed_local_configuration(template))
        self.assertFalse(provider_setup._has_completed_local_configuration(legacy))

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

        local = provider_setup.config_with_provider_choice(configured, "ollama")
        self.assertEqual(provider_setup.provider_choice_from_config(local), "ollama")
        self.assertTrue(local.ollama_enabled)
        self.assertFalse(local.cloud_enabled)
        self.assertFalse(local.codex_cli_enabled)
        self.assertFalse(local.claude_cli_enabled)
        self.assertEqual(local.fast_model, "qwen3.5:9b")

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
        self.assertIn(
            f"JARVIS_FAST_MODEL={provider_setup._PINNED_CLAUDE_SONNET_4_5_MODEL}",
            saved,
        )
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

    @staticmethod
    def _canary_config() -> SimpleNamespace:
        return SimpleNamespace(
            model="auto",
            fast_model="codex-cli:fast-one",
            reasoning_model="codex-cli:reason-one",
            coding_model="codex-cli:code-one",
            deep_model="codex-cli:code-one",
            background_model="fast",
            learning_model="codex-cli:fast-one",
        )

    def test_first_turn_canary_checks_each_unique_route_without_tools_or_secrets(self) -> None:
        client = Mock()
        client.chat.return_value = {"content": provider_setup._CANARY_SENTINEL}
        result = provider_setup.run_provider_canary(
            config=self._canary_config(),
            client_factory=Mock(return_value=client),
        )

        self.assertEqual(len(result.checks), 3)
        self.assertEqual(client.chat.call_count, 3)
        tested_models = {call.args[2] for call in client.chat.call_args_list}
        self.assertEqual(
            tested_models,
            {"codex-cli:fast-one", "codex-cli:reason-one", "codex-cli:code-one"},
        )
        for call in client.chat.call_args_list:
            messages, tools, _model = call.args
            self.assertEqual(tools, [])
            self.assertTrue(call.kwargs["think"] is False)
            self.assertEqual(call.kwargs["keep_alive"], "0")
            rendered = repr(messages)
            self.assertIn(provider_setup._CANARY_SENTINEL, rendered)
            self.assertNotIn("API_KEY", rendered)
        client.close.assert_called_once_with()

    def test_first_turn_canary_fails_closed_with_retry_guidance_and_no_output_echo(self) -> None:
        client = Mock()
        client.chat.return_value = {"content": "private provider output"}
        with self.assertRaises(provider_setup.ProviderSetupRequired) as raised:
            provider_setup.run_provider_canary(
                config=self._canary_config(),
                client_factory=Mock(return_value=client),
            )
        message = str(raised.exception)
        self.assertIn("--canary", message)
        self.assertNotIn("private provider output", message)
        client.close.assert_called_once_with()

    def test_first_turn_canary_sanitizes_configured_model_in_failure_message(self) -> None:
        config = self._canary_config()
        config.fast_model = "codex-cli:model\nINJECTED"
        client = Mock()
        client.chat.side_effect = RuntimeError("secret upstream diagnostic")
        with self.assertRaises(provider_setup.ProviderSetupRequired) as raised:
            provider_setup.run_provider_canary(
                config=config,
                client_factory=Mock(return_value=client),
            )
        message = str(raised.exception)
        self.assertNotIn("\nINJECTED", message)
        self.assertNotIn("secret upstream diagnostic", message)
        self.assertIn("--canary", message)

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

    def test_native_candidates_ignore_path_and_use_runtime_resolver(self) -> None:
        poison = self.root / ("codex.exe" if os.name == "nt" else "codex")
        trusted = self.root / "runtime-validated-codex"
        with patch.object(
            provider_setup,
            "resolve_codex_cli_executable",
            return_value=trusted,
        ) as resolver:
            candidates = provider_setup._native_candidates(
                "codex",
                {"PATH": str(poison.parent), "APPDATA": str(poison.parent)},
            )

        self.assertEqual(candidates, [trusted])
        resolver.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "Windows Package Manager path is Windows-only")
    def test_windows_install_ignores_untrusted_path_shim(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        with patch.object(
            provider_setup, "_windows_package_manager_executable", return_value=None
        ) as resolver:
            installed = provider_setup._install_provider(
                "codex",
                environ={"PATH": str(self.root)},
                runner=runner,
            )

        self.assertFalse(installed)
        resolver.assert_called_once_with()
        runner.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Package Manager path is Windows-only")
    def test_winget_registry_hint_cannot_escape_desktop_app_installer(self) -> None:
        poison = self.root / "winget.exe"
        poison.write_bytes(b"MZ")
        with patch.object(
            provider_setup, "_winget_registry_hint", return_value=poison
        ):
            self.assertIsNone(provider_setup._windows_package_manager_executable())

    @unittest.skipUnless(os.name == "nt", "Windows Package Manager path is Windows-only")
    def test_winget_accepts_exact_trusted_desktop_app_installer_binary(self) -> None:
        candidate = (
            self.root
            / "WindowsApps"
            / "Microsoft.DesktopAppInstaller_1.29.290.0_x64__8wekyb3d8bbwe"
            / "winget.exe"
        )
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"MZnative")
        with (
            patch.object(
                provider_setup, "_winget_registry_hint", return_value=candidate
            ),
            patch.object(
                provider_setup, "trusted_install_file", return_value=candidate
            ),
        ):
            self.assertEqual(
                provider_setup._windows_package_manager_executable(), candidate
            )

    @unittest.skipUnless(os.name == "nt", "Windows Package Manager path is Windows-only")
    def test_windows_install_uses_exact_official_package_and_no_provider_keys(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        environment = {
            "PATH": "C:\\Windows",
            "USERPROFILE": "C:\\Users\\operator",
            "OPENAI_API_KEY": "must-not-cross",
            "ANTHROPIC_API_KEY": "must-not-cross",
        }
        with (
            patch.object(
                provider_setup,
                "_windows_package_manager_executable",
                return_value=Path("C:/Windows/winget.exe"),
            ),
        ):
            installed = provider_setup._install_provider(
                "codex", environ=environment, runner=runner
            )
        self.assertTrue(installed)
        args, options = runner.call_args
        self.assertEqual(
            args[0],
            [
                str(Path("C:/Windows/winget.exe")),
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
        self.assertIn('"jarvis.installer", "--interactive"', setup)


if __name__ == "__main__":
    unittest.main()
