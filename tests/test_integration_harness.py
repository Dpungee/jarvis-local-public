from __future__ import annotations

import unittest

from jarvis.integration_harness import CredentialBrokerPolicy, ScopedGrant, WalletIntent, WalletSignerPolicy, default_registry


class IntegrationHarnessTests(unittest.TestCase):
    def test_registry_is_inert_and_complete(self) -> None:
        rows = default_registry().list_public()
        self.assertEqual({row["integration_id"] for row in rows}, {"mcp", "plugins", "desktop", "browser", "api", "wallet"})
        self.assertTrue(all(row["state"] == "UNCONFIGURED" for row in rows))

    def test_credential_broker_accepts_exact_destination_without_secret(self) -> None:
        grant = ScopedGrant("agt_test", "api.read", "acct_reports", ("api.example.com",))
        result = CredentialBrokerPolicy().authorize(agent_id="agt_test", capability="api.read", credential_ref="cred_reports", destination="https://api.example.com/v1/report", resource="acct_reports", grant=grant)
        self.assertEqual(result, "cred_reports")

    def test_credential_broker_rejects_raw_secret_and_destination_confusion(self) -> None:
        grant = ScopedGrant("agt_test", "api.read", "acct_reports", ("api.example.com",))
        policy = CredentialBrokerPolicy()
        base = dict(agent_id="agt_test", capability="api.read", credential_ref="cred_reports", resource="acct_reports", grant=grant)
        with self.assertRaises(PermissionError):
            policy.authorize(**base, destination="https://api.example.com.evil.test/", supplied_secret="token")
        with self.assertRaises(PermissionError):
            policy.authorize(**base, destination="https://api.example.com.evil.test/")
        with self.assertRaises(PermissionError):
            policy.authorize(**base, destination="https://127.0.0.1/")
        with self.assertRaises(PermissionError):
            policy.authorize(**base, destination="http://api.example.com/")

    def test_wallet_policy_rejects_budget_destination_and_approval_expansion(self) -> None:
        grant = ScopedGrant("agt_test", "wallet.sign", "wallet_ops", ("dest_ok",), 500, 20)
        policy = WalletSignerPolicy()
        policy.authorize(agent_id="agt_test", intent=WalletIntent("wallet_ops", "test-chain", "test", "dest_ok", 500, 20), grant=grant)
        with self.assertRaises(PermissionError):
            policy.authorize(agent_id="agt_test", intent=WalletIntent("wallet_ops", "test-chain", "test", "dest_bad", 1, 1), grant=grant)
        with self.assertRaises(PermissionError):
            policy.authorize(agent_id="agt_test", intent=WalletIntent("wallet_ops", "test-chain", "test", "dest_ok", 501, 1), grant=grant)
        with self.assertRaises(PermissionError):
            policy.authorize(agent_id="agt_test", intent=WalletIntent("wallet_ops", "test-chain", "test", "dest_ok", 1, 1, ("unlimited-token-approval",)), grant=grant)


if __name__ == "__main__":
    unittest.main()
