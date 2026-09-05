import os
import re
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jarvis.config as config_module
from jarvis.config import Config, create_project_workspace, resolve_project_workspace

TEST_ROOT = Path(__file__).resolve().parents[1] / ".config-test-root"


def load_config(environment):
    isolated_environment = {
        "JARVIS_SOUL": str(config_module.PACKAGED_SOUL),
        "JARVIS_CONSTITUTION": str(config_module.PACKAGED_CONSTITUTION),
        **environment,
    }
    with (
        patch.object(config_module, "ROOT", TEST_ROOT),
        patch.dict(os.environ, isolated_environment, clear=True),
        patch.object(Path, "mkdir"),
        patch.object(config_module, "_seed_default_soul"),
    ):
        return Config.load()


class ConfigSecurityTests(unittest.TestCase):
    def test_example_documents_every_supported_operator_setting(self):
        example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        documented = set(
            re.findall(r"(?m)^(?:#\s*)?([A-Z][A-Z0-9_]+)=", example)
        )
        self.assertEqual(config_module._DOTENV_KEYS - documented, set())

    def test_every_active_example_setting_is_loadable(self):
        """Copying .env.example to .env must never fail startup.

        The previous guard only proved that every supported key is documented;
        an example line for a key the loader rejects (JARVIS_STRATEGY_TRANSFER
        was one) broke every command on first run.
        """
        example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        active = set(re.findall(r"(?m)^([A-Z][A-Z0-9_]+)=", example))
        self.assertEqual(active - config_module._DOTENV_KEYS, set())

    def test_default_endpoint_is_loopback(self):
        config = load_config({})
        self.assertEqual(config.ollama_url, "http://127.0.0.1:11434")
        self.assertTrue(config.ollama_enabled)
        self.assertFalse(config.ollama_allow_remote)
        self.assertEqual(config.execution_mode, "disabled")
        self.assertEqual(config.execution_backend, "host")
        self.assertEqual(config.network_defense_mode, "disabled")
        self.assertFalse(config.network_incident_popups_enabled)
        self.assertEqual(config.ollama_health_timeout, 5.0)
        self.assertEqual(config.ollama_generation_timeout, 600.0)
        self.assertEqual(config.ollama_max_output_tokens, 2048)
        self.assertEqual(config.ollama_keep_alive, "30m")
        self.assertEqual(config.ollama_deep_keep_alive, "0")
        self.assertIsNone(config.ollama_num_thread)
        self.assertFalse(config.ollama_preload)
        self.assertTrue(config.reasoning_thinking)
        self.assertTrue(config.cloud_enabled)
        self.assertEqual(config.self_inspect, "disabled")
        self.assertEqual(config.self_repair, "disabled")
        self.assertEqual(config.initiative, "disabled")
        self.assertEqual(config.initiative_quiet_hours, "")
        self.assertEqual(config.cloud_generation_timeout, 600.0)
        self.assertEqual(config.cloud_max_output_tokens, 8192)
        self.assertEqual(config.cloud_max_response_bytes, 8 * 1024 * 1024)
        self.assertEqual(config.cloud_max_retries, 2)
        self.assertEqual(config.cloud_retry_backoff, 0.5)
        self.assertFalse(config.openai_api_enabled)
        self.assertFalse(config.openai_images_enabled)
        self.assertFalse(config.anthropic_api_enabled)
        self.assertFalse(config.codex_cli_enabled)
        self.assertFalse(config.claude_cli_enabled)
        self.assertEqual(config.background_model, "fast")
        self.assertIsNone(config.learning_model)
        self.assertEqual(config.presence_host, "127.0.0.1")
        self.assertEqual(config.presence_remote_access, "disabled")
        self.assertEqual(config.presence_trusted_hosts, ())
        self.assertEqual(config.presence_port, 8787)
        self.assertEqual(config.presence_max_agents, 3)
        self.assertEqual(config.worker_concurrency, 3)
        self.assertEqual(config.gateway_channel, "")
        self.assertIsNone(config.gateway_token)
        self.assertEqual(config.gateway_allowed_ids, ())
        self.assertEqual(config.google_drive_access, "app_files")
        self.assertTrue(config.memory_auto_improve)
        self.assertEqual(config.strategy_transfer, "observe")
        self.assertEqual(config.memory_embeddings, "disabled")
        self.assertEqual(config.memory_embedding_model, "text-embedding-3-small")
        self.assertEqual(config.memory_embedding_dimensions, 512)
        self.assertEqual(config.memory_claim_clock, "shadow")
        self.assertEqual(config.memory_claim_stale_threshold, 0.70)
        self.assertEqual(config.fast_model, "qwen3.5:9b")
        self.assertEqual(config.deep_model, "qwen3-coder:30b")
        self.assertEqual(config.deep_context_length, 4096)

    def test_google_drive_full_access_requires_explicit_valid_opt_in(self):
        full = load_config({"JARVIS_GOOGLE_DRIVE_ACCESS": " FULL "})
        self.assertEqual(full.google_drive_access, "full")
        for value in ("", "all", "readwrite", "drive"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "GOOGLE_DRIVE_ACCESS"
            ):
                load_config({"JARVIS_GOOGLE_DRIVE_ACCESS": value})

    def test_claim_clock_requires_an_explicit_valid_mode(self):
        enforced = load_config(
            {
                "JARVIS_MEMORY_CLAIM_CLOCK": " ENFORCE ",
                "JARVIS_MEMORY_CLAIM_STALE_THRESHOLD": "0.83",
            }
        )
        self.assertEqual(enforced.memory_claim_clock, "enforce")
        self.assertEqual(enforced.memory_claim_stale_threshold, 0.83)
        for value in ("", "enabled", "auto", "kairos"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "MEMORY_CLAIM_CLOCK"
            ):
                load_config({"JARVIS_MEMORY_CLAIM_CLOCK": value})

    def test_strategy_transfer_defaults_to_observe_and_requires_closed_mode(self):
        self.assertEqual(load_config({}).strategy_transfer, "observe")
        self.assertEqual(
            load_config({"JARVIS_STRATEGY_TRANSFER": " ADVISE "}).strategy_transfer,
            "advise",
        )
        self.assertEqual(
            load_config({"JARVIS_STRATEGY_TRANSFER": " TRIAL "}).strategy_transfer,
            "trial",
        )
        for value in ("", "enabled", "auto", "execute", "unbounded"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "STRATEGY_TRANSFER"
            ):
                load_config({"JARVIS_STRATEGY_TRANSFER": value})

    def test_execution_mode_requires_explicit_valid_opt_in(self):
        trusted = load_config({"JARVIS_EXECUTION_MODE": " TRUSTED-HOST "})
        self.assertEqual(trusted.execution_mode, "trusted-host")
        for value in ("", "enabled", "readonly", "sandbox", "host"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "EXECUTION_MODE"):
                load_config({"JARVIS_EXECUTION_MODE": value})

    def test_network_defense_mode_has_a_bounded_readonly_ceiling(self):
        enabled = load_config({"JARVIS_NETWORK_DEFENSE_MODE": " SAFE-READONLY "})
        self.assertEqual(enabled.network_defense_mode, "safe-readonly")
        for value in ("", "active", "contain", "auto-block", "offensive"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "NETWORK_DEFENSE_MODE"
            ):
                load_config({"JARVIS_NETWORK_DEFENSE_MODE": value})

    def test_docker_backend_selection_fails_closed_without_daemon(self):
        with patch("jarvis.execution.docker_available", return_value=False):
            with self.assertRaisesRegex(ValueError, "Docker CLI"):
                load_config({"JARVIS_EXECUTION_BACKEND": "docker"})
        with patch("jarvis.execution.docker_available", return_value=True):
            config = load_config({"JARVIS_EXECUTION_BACKEND": "docker"})
        self.assertEqual(config.execution_backend, "docker")
        with self.assertRaisesRegex(ValueError, "EXECUTION_BACKEND"):
            load_config({"JARVIS_EXECUTION_BACKEND": "fallback"})

    def test_gateway_requires_token_and_owner_allowlist(self):
        config = load_config({
            "JARVIS_GATEWAY_CHANNEL": "telegram",
            "JARVIS_GATEWAY_TOKEN": "123456:synthetic-token",
            "JARVIS_GATEWAY_ALLOWED_IDS": "42,84",
        })
        self.assertEqual(config.gateway_channel, "telegram")
        self.assertEqual(config.gateway_allowed_ids, ("42", "84"))
        self.assertNotIn("synthetic-token", repr(config))
        for values in (
            {"JARVIS_GATEWAY_CHANNEL": "telegram"},
            {
                "JARVIS_GATEWAY_CHANNEL": "telegram",
                "JARVIS_GATEWAY_TOKEN": "synthetic",
            },
            {"JARVIS_GATEWAY_CHANNEL": "public-web"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                load_config(values)

    def test_default_soul_is_seeded_before_config_returns(self):
        test_root = Path(__file__).resolve().parent / ".tmp" / f"config-seed-{os.getpid()}"
        if test_root.exists():
            shutil.rmtree(test_root)
        test_root.mkdir(parents=True)
        packaged_soul = test_root / "packaged-soul.md"
        packaged_soul.write_text("seeded personality\n", encoding="utf-8")
        try:
            with (
                patch.object(config_module, "ROOT", test_root),
                patch.object(config_module, "PACKAGED_SOUL", packaged_soul),
                patch.dict(os.environ, {}, clear=True),
            ):
                config = Config.load()
            self.assertEqual(config.soul_path, test_root / "SOUL.md")
            self.assertEqual(config.soul_path.read_text(encoding="utf-8"), "seeded personality\n")
        finally:
            shutil.rmtree(test_root)

    def test_remote_endpoint_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            load_config({"JARVIS_OLLAMA_URL": "https://ollama.example"})

    def test_trusted_remote_requires_https_even_with_opt_in(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_config(
                {
                    "JARVIS_OLLAMA_URL": "http://ollama.example:11434",
                    "JARVIS_OLLAMA_ALLOW_REMOTE": "true",
                }
            )

    def test_trusted_https_remote_and_client_settings_load(self):
        env = {
            "JARVIS_OLLAMA_URL": "https://ollama.example:443",
            "JARVIS_OLLAMA_ALLOW_REMOTE": "yes",
            "JARVIS_OLLAMA_HEALTH_TIMEOUT": "3.5",
            "JARVIS_OLLAMA_GENERATION_TIMEOUT": "900",
            "JARVIS_OLLAMA_MAX_OUTPUT_TOKENS": "3072",
            "JARVIS_OLLAMA_MAX_RESPONSE_BYTES": "1048576",
            "JARVIS_OLLAMA_MAX_RETRIES": "1",
            "JARVIS_OLLAMA_RETRY_BACKOFF": "0.5",
            "JARVIS_OLLAMA_KEEP_ALIVE": "45M",
            "JARVIS_OLLAMA_DEEP_KEEP_ALIVE": "-1",
            "JARVIS_OLLAMA_NUM_THREAD": "8",
            "JARVIS_OLLAMA_PRELOAD": "true",
            "JARVIS_REASONING_THINKING": "false",
            "JARVIS_CLOUD_ENABLED": "false",
            "OLLAMA_API_KEY": "do-not-leak",
        }
        config = load_config(env)

        self.assertEqual(config.ollama_url, "https://ollama.example:443")
        self.assertTrue(config.ollama_allow_remote)
        self.assertEqual(config.ollama_health_timeout, 3.5)
        self.assertEqual(config.ollama_generation_timeout, 900.0)
        self.assertEqual(config.ollama_max_output_tokens, 3072)
        self.assertEqual(config.ollama_max_response_bytes, 1048576)
        self.assertEqual(config.ollama_max_retries, 1)
        self.assertEqual(config.ollama_retry_backoff, 0.5)
        self.assertEqual(config.ollama_keep_alive, "45m")
        self.assertEqual(config.ollama_deep_keep_alive, "-1")
        self.assertEqual(config.ollama_num_thread, 8)
        self.assertTrue(config.ollama_preload)
        self.assertFalse(config.reasoning_thinking)
        self.assertFalse(config.cloud_enabled)
        self.assertNotIn("do-not-leak", repr(config))

    def test_invalid_boolean_and_bounds_fail_closed(self):
        cases = (
            {"JARVIS_OLLAMA_ALLOW_REMOTE": "sometimes"},
            {"JARVIS_OLLAMA_ENABLED": "sometimes"},
            {"JARVIS_OLLAMA_HEALTH_TIMEOUT": "0"},
            {"JARVIS_OLLAMA_GENERATION_TIMEOUT": "not-a-number"},
            {"JARVIS_OLLAMA_MAX_OUTPUT_TOKENS": "64"},
            {"JARVIS_OLLAMA_MAX_RESPONSE_BYTES": "100"},
            {"JARVIS_OLLAMA_MAX_RETRIES": "99"},
            {"JARVIS_OLLAMA_RETRY_BACKOFF": "-1"},
            {"JARVIS_OLLAMA_KEEP_ALIVE": "forever"},
            {"JARVIS_OLLAMA_DEEP_KEEP_ALIVE": "forever"},
            {"JARVIS_OLLAMA_NUM_THREAD": "0"},
            {"JARVIS_OLLAMA_PRELOAD": "sometimes"},
            {"JARVIS_REASONING_THINKING": "sometimes"},
            {"JARVIS_CLOUD_ENABLED": "sometimes"},
            {"JARVIS_CLOUD_GENERATION_TIMEOUT": "0"},
            {"JARVIS_CLOUD_MAX_OUTPUT_TOKENS": "128"},
            {"JARVIS_CLOUD_MAX_RESPONSE_BYTES": "100"},
            {"JARVIS_CLOUD_MAX_RETRIES": "99"},
            {"JARVIS_CLOUD_RETRY_BACKOFF": "-1"},
            {"JARVIS_SELF_INSPECT": "write"},
            {"JARVIS_SELF_REPAIR": "apply"},
            {"JARVIS_INITIATIVE": "unbounded"},
            {"JARVIS_INITIATIVE_QUIET_HOURS": "25:00-07:00"},
            {"JARVIS_PRESENCE_MAX_AGENTS": "0"},
            {"JARVIS_PRESENCE_TRUSTED_HOSTS": "*.example.com"},
            {"JARVIS_PRESENCE_MAX_AGENTS": "9"},
            {"JARVIS_WORKER_CONCURRENCY": "0"},
            {"JARVIS_WORKER_CONCURRENCY": "9"},
            {"JARVIS_MEMORY_AUTO_IMPROVE": "sometimes"},
            {"JARVIS_STRATEGY_TRANSFER": "unbounded"},
            {"JARVIS_MEMORY_EMBEDDINGS": "local-magic"},
            {"JARVIS_MEMORY_EMBEDDING_MODEL": ""},
            {"JARVIS_MEMORY_EMBEDDING_DIMENSIONS": "63"},
            {"JARVIS_MEMORY_EMBEDDING_DIMENSIONS": "4097"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                load_config(values)

    def test_self_inspection_accepts_only_disabled_or_read_only(self):
        self.assertEqual(
            load_config({"JARVIS_SELF_INSPECT": " READ-ONLY "}).self_inspect,
            "read-only",
        )
        for value in ("", "enabled", "write", "repair", "apply"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "SELF_INSPECT"
            ):
                load_config({"JARVIS_SELF_INSPECT": value})

    def test_self_repair_is_proposal_only_and_depends_on_read_only_inspection(self):
        enabled = load_config({
            "JARVIS_SELF_INSPECT": "read-only",
            "JARVIS_SELF_REPAIR": "propose",
            "JARVIS_INITIATIVE": "act",
            "JARVIS_INITIATIVE_QUIET_HOURS": "23:00-07:00",
        })
        self.assertEqual(enabled.self_repair, "propose")
        self.assertEqual(enabled.initiative, "act")
        with self.assertRaisesRegex(ValueError, "requires"):
            load_config({"JARVIS_SELF_REPAIR": "propose"})

    def test_cloud_limits_load_without_persisting_api_keys_in_config(self):
        config = load_config({
            "OPENAI_API_KEY": "sk-test-openai-not-real",
            "ANTHROPIC_API_KEY": "sk-ant-test-not-real",
            "JARVIS_CLOUD_GENERATION_TIMEOUT": "900",
            "JARVIS_CLOUD_MAX_OUTPUT_TOKENS": "16384",
            "JARVIS_CLOUD_MAX_RESPONSE_BYTES": "1048576",
            "JARVIS_CLOUD_MAX_RETRIES": "1",
            "JARVIS_CLOUD_RETRY_BACKOFF": "0.75",
            "JARVIS_OPENAI_API_ENABLED": "false",
            "JARVIS_OPENAI_IMAGES_ENABLED": "true",
            "JARVIS_ANTHROPIC_API_ENABLED": "false",
            "JARVIS_CODEX_CLI_ENABLED": "true",
            "JARVIS_CLAUDE_CLI_ENABLED": "true",
        })
        self.assertEqual(config.cloud_generation_timeout, 900.0)
        self.assertEqual(config.cloud_max_output_tokens, 16384)
        self.assertEqual(config.cloud_max_response_bytes, 1048576)
        self.assertEqual(config.cloud_max_retries, 1)
        self.assertEqual(config.cloud_retry_backoff, 0.75)
        self.assertFalse(config.openai_api_enabled)
        self.assertTrue(config.openai_images_enabled)
        self.assertFalse(config.anthropic_api_enabled)
        self.assertTrue(config.codex_cli_enabled)
        self.assertTrue(config.claude_cli_enabled)
        rendered = repr(config)
        self.assertNotIn("sk-test-openai", rendered)
        self.assertNotIn("sk-ant-test", rendered)

    def test_global_context_override_applies_to_all_profiles(self):
        config = load_config({"JARVIS_CONTEXT_LENGTH": "49152"})
        self.assertEqual(config.context_length, 49152)
        self.assertEqual(config.fast_context_length, 49152)
        self.assertEqual(config.reasoning_context_length, 49152)
        self.assertEqual(config.coding_context_length, 49152)
        self.assertEqual(config.deep_context_length, 49152)

    def test_profile_context_defaults_prioritize_fast_latency(self):
        config = load_config({})
        self.assertEqual(config.fast_context_length, 16384)
        self.assertEqual(config.reasoning_context_length, 16384)
        self.assertEqual(config.coding_context_length, 16384)
        self.assertEqual(config.deep_context_length, 4096)

    def test_profile_context_override_wins_over_global_override(self):
        config = load_config({
            "JARVIS_CONTEXT_LENGTH": "49152",
            "JARVIS_CODING_CONTEXT_LENGTH": "98304",
        })
        self.assertEqual(config.fast_context_length, 49152)
        self.assertEqual(config.coding_context_length, 98304)

    def test_background_model_can_be_overridden_but_not_empty(self):
        config = load_config({"JARVIS_BACKGROUND_MODEL": "qwen3:8b"})
        self.assertEqual(config.background_model, "qwen3:8b")
        with self.assertRaisesRegex(ValueError, "BACKGROUND_MODEL"):
            load_config({"JARVIS_BACKGROUND_MODEL": "   "})

    def test_learning_model_and_presence_configuration(self):
        config = load_config({
            "JARVIS_LEARNING_MODEL": "openai:gpt-5.6-luna",
            "JARVIS_PRESENCE_HOST": "localhost",
            "JARVIS_PRESENCE_PORT": "9876",
            "JARVIS_PRESENCE_REMOTE_ACCESS": "paired",
        })
        self.assertEqual(config.learning_model, "openai:gpt-5.6-luna")
        self.assertEqual(config.presence_host, "localhost")
        self.assertEqual(config.presence_port, 9876)
        self.assertEqual(config.presence_remote_access, "paired")
        with self.assertRaisesRegex(ValueError, "PRESENCE_HOST"):
            load_config({"JARVIS_PRESENCE_HOST": "0.0.0.0"})
        with self.assertRaises(ValueError):
            load_config({"JARVIS_PRESENCE_PORT": "80"})
        with self.assertRaisesRegex(ValueError, "PRESENCE_REMOTE_ACCESS"):
            load_config({"JARVIS_PRESENCE_REMOTE_ACCESS": "public"})

    def test_project_workspaces_are_disjoint_and_reject_unsafe_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            default_workspace = parent / "workspace"
            default_workspace.mkdir()
            config = replace(load_config({}), workspace=default_workspace)

            project_root, relative_path = create_project_workspace(config, "security-lab")

            self.assertEqual(relative_path, "@projects/security-lab")
            self.assertEqual(
                project_root,
                parent / "workspace-projects" / "security-lab",
            )
            self.assertNotEqual(project_root.parent, default_workspace)
            self.assertEqual(
                resolve_project_workspace(config, relative_path),
                project_root,
            )

            shutil.rmtree(parent / "workspace-projects")
            (parent / "workspace-projects").write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "ordinary directory"):
                resolve_project_workspace(config, relative_path)


if __name__ == "__main__":
    unittest.main()
