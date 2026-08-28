from __future__ import annotations

import unittest

from jarvis.subprocess_env import trusted_cli_environment


class TrustedCLIEnvironmentTests(unittest.TestCase):
    def test_preserves_bounded_proxy_and_custom_ca_settings_case_insensitively(self) -> None:
        source = {
            "Path": "C:\\trusted-bin",
            "https_proxy": "http://127.0.0.1:8080",
            "NO_PROXY": "localhost,127.0.0.1",
            "REQUESTS_CA_BUNDLE": "C:\\certs\\company.pem",
            "NODE_EXTRA_CA_CERTS": "C:\\certs\\company.pem",
            "SSL_CERT_FILE": "C:\\certs\\company.pem",
        }

        result = trusted_cli_environment(source)

        self.assertEqual(result, source)

    def test_drops_provider_secrets_and_unrelated_application_environment(self) -> None:
        result = trusted_cli_environment(
            {
                "PATH": "C:\\trusted-bin",
                "HTTPS_PROXY": "http://127.0.0.1:8080",
                "OPENAI_API_KEY": "must-not-cross",
                "ANTHROPIC_API_KEY": "must-not-cross",
                "JARVIS_WORKSPACE": "C:\\private-workspace",
                "PYTHONPATH": "C:\\untrusted-imports",
                "NODE_OPTIONS": "--require C:\\untrusted.js",
            }
        )

        self.assertEqual(
            result,
            {
                "PATH": "C:\\trusted-bin",
                "HTTPS_PROXY": "http://127.0.0.1:8080",
            },
        )

    def test_drops_nul_values_and_can_exclude_ssh_agent(self) -> None:
        result = trusted_cli_environment(
            {
                "PATH": "safe\x00unsafe",
                "HTTPS_PROXY": "http://127.0.0.1:8080\x00bad",
                "SSH_AUTH_SOCK": "C:\\agent.sock",
            },
            include_ssh_agent=False,
        )

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
