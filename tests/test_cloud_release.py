import concurrent.futures
import unittest
from unittest.mock import Mock, patch

from jarvis.cloud_release import CloudReleaseDenied, CloudReleaseGate, context_digest
from jarvis.model_client import _ACTIVE_MODEL_CONVERSATION, ModelClient


class CloudReleaseTests(unittest.TestCase):
    def setUp(self):
        self.gate = CloudReleaseGate()
        self.actor = "agt_" + "1" * 32
        self.model = "codex-cli:test-model"
        self.messages = [
            {"role": "user", "content": "Explain a public sorting algorithm."}
        ]

    def approve(self, **extra):
        values = {
            "actor": self.actor,
            "model": self.model,
            "messages": self.messages,
            "reviewed_sha256": context_digest(self.messages),
        }
        values.update(extra)
        return self.gate.approve(**values)

    def consume(self, ticket, **extra):
        values = {"ticket": ticket, "actor": self.actor, "model": self.model}
        values.update(extra)
        return self.gate.consume(**values)

    def test_exact_snapshot_and_one_use(self):
        ticket = self.approve()
        self.messages[0]["content"] = "Changed after approval"
        self.assertEqual(
            self.consume(ticket)[0]["content"], "Explain a public sorting algorithm."
        )
        with self.assertRaises(CloudReleaseDenied):
            self.consume(ticket)

    def test_review_mismatch(self):
        with self.assertRaisesRegex(CloudReleaseDenied, "review_mismatch"):
            self.approve(reviewed_sha256="0" * 64)

    def test_actor_or_model_substitution_burns_ticket(self):
        for change in (
            {"actor": "agt_" + "2" * 32},
            {"model": "claude-cli:sonnet"},
            {"model": "ollama:local"},
        ):
            ticket = self.approve()
            with self.assertRaises(CloudReleaseDenied):
                self.consume(ticket, **change)
            with self.assertRaises(CloudReleaseDenied):
                self.consume(ticket)

    def test_revoke_restart_unknown_and_expiry(self):
        ticket = self.approve()
        self.gate.revoke(ticket)
        with self.assertRaises(CloudReleaseDenied):
            self.consume(ticket)
        ticket = self.approve()
        with self.assertRaises(CloudReleaseDenied):
            CloudReleaseGate().consume(
                ticket=ticket, actor=self.actor, model=self.model
            )
        with (
            patch("jarvis.cloud_release.time.monotonic", return_value=float("inf")),
            self.assertRaises(CloudReleaseDenied),
        ):
            self.consume(ticket)

    def test_sensitive_examples_denied_without_echo(self):
        for content in (
            "-----BEGIN PRIVATE KEY-----",
            "seed phrase: example words",
            "network 192.168.1.12",
            "adapter aa:bb:cc:dd:ee:ff",
            "person@example.org",
            "C:" + "\\".join(("", "Users", "ExamplePerson", "notes.txt")),
        ):
            with self.subTest(content=content):
                with self.assertRaises(CloudReleaseDenied) as error:
                    context_digest([{"role": "user", "content": content}])
                self.assertNotIn(content, str(error.exception))

    def test_closed_schema(self):
        for messages in (
            [],
            [{"role": "tool", "content": "output"}],
            [{"role": "user", "content": "safe", "images": ["secret"]}],
            [{"role": "user", "content": {"hidden": "value"}}],
            [{"role": "user", "content": "\ud800"}],
            [{"role": "user", "content": "a" * 32769}],
        ):
            with self.assertRaises(CloudReleaseDenied):
                context_digest(messages)

    def test_invalid_expiry_and_model(self):
        for ttl in (True, 0, -1, 301, float("nan"), float("inf")):
            with self.assertRaises(CloudReleaseDenied):
                self.approve(ttl_seconds=ttl)
        for model in ("openai:test", "ollama:test", "codex-cli:test --unsafe"):
            with self.assertRaises(CloudReleaseDenied):
                self.approve(model=model)

    def test_concurrent_consumers_exactly_one(self):
        ticket = self.approve()

        def attempt(_):
            try:
                self.consume(ticket)
                return 1
            except CloudReleaseDenied:
                return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(16))), 1)

    def test_adapter_has_no_extra_context_or_tools_and_isolates_conversation(self):
        client = ModelClient(None)
        ticket = self.approve()

        def chat(messages, tools, model):
            self.assertEqual(messages, self.messages)
            self.assertEqual(tools, [])
            self.assertEqual(model, self.model)
            self.assertEqual(_ACTIVE_MODEL_CONVERSATION.get(), "release-" + ticket)
            return "reply"

        client.chat = Mock(side_effect=chat)
        self.assertEqual(
            client.chat_released(
                self.gate, ticket=ticket, actor=self.actor, model=self.model
            ),
            "reply",
        )
        self.assertIsNone(_ACTIVE_MODEL_CONVERSATION.get())
        with self.assertRaises(CloudReleaseDenied):
            client.chat_released(
                self.gate, ticket=ticket, actor=self.actor, model=self.model
            )
        self.assertEqual(client.chat.call_count, 1)

    def test_transport_failure_does_not_restore_approval(self):
        client = ModelClient(None)
        client.chat = Mock(side_effect=RuntimeError("transport failed"))
        ticket = self.approve()
        with self.assertRaises(RuntimeError):
            client.chat_released(
                self.gate, ticket=ticket, actor=self.actor, model=self.model
            )
        with self.assertRaises(CloudReleaseDenied):
            self.consume(ticket)


if __name__ == "__main__":
    unittest.main()
