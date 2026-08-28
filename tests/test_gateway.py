from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.agent import AgentResult
from jarvis.config import Config
from jarvis.gateway import GatewayRuntime, InboundMessage
from jarvis.memory import Memory


class _FakeAdapter:
    channel = "telegram"

    def __init__(self) -> None:
        self.offset = 0
        self.messages: list[InboundMessage] = []
        self.sent: list[tuple[str, str]] = []

    def poll_or_listen(self):
        messages, self.messages = self.messages, []
        return messages

    def send(self, sender_id: str, text: str) -> None:
        self.sent.append((sender_id, text))


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="jarvis-gateway-test-"))
        self.workspace = self.root / "workspace"
        self.data_dir = self.root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            vault_dir=None,
            gateway_channel="telegram",
            gateway_token="123456:synthetic-test-token",
            gateway_allowed_ids=("42",),
            memory_embeddings="disabled",
        )
        self.adapter = _FakeAdapter()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_disabled_gateway_is_a_clean_noop(self) -> None:
        runtime = GatewayRuntime(
            replace(
                self.config, gateway_channel="", gateway_token=None,
                gateway_allowed_ids=(),
            )
        )
        self.assertFalse(runtime.enabled)
        self.assertEqual(runtime.run_once(), 0)

    def test_unallowlisted_sender_never_reaches_agent(self) -> None:
        calls: list[str] = []

        class FakeAgent:
            def run(self, prompt, **kwargs):
                calls.append(prompt)
                return AgentResult("should not run")

        runtime = GatewayRuntime(
            self.config, adapter=self.adapter,
            agent_factory=lambda _config, _memory: FakeAgent(),
        )
        handled = runtime.handle(InboundMessage("999", "hello", "1"))
        self.assertFalse(handled)
        self.assertEqual(calls, [])
        self.assertEqual(self.adapter.sent, [])

    def test_owner_message_is_untrusted_framed_and_reply_is_redacted(self) -> None:
        calls: list[tuple[str, int]] = []
        secret = "sk-proj-" + "A" * 32

        class FakeAgent:
            def run(self, prompt, *, conversation_id, **kwargs):
                calls.append((prompt, conversation_id))
                return AgentResult(f"finished {secret}")

        runtime = GatewayRuntime(
            self.config, adapter=self.adapter,
            agent_factory=lambda _config, _memory: FakeAgent(),
        )
        self.assertTrue(runtime.handle(InboundMessage("42", "summarize this", "1")))
        self.assertEqual(len(calls), 1)
        self.assertIn("<untrusted_gateway_message>", calls[0][0])
        self.assertIn(json.dumps("summarize this"), calls[0][0])
        sent = "\n".join(text for _, text in self.adapter.sent)
        self.assertNotIn(secret, sent)
        self.assertIn("[REDACTED]", sent)

        self.assertTrue(runtime.handle(InboundMessage("42", "follow up", "2")))
        self.assertEqual(calls[0][1], calls[1][1])

    def test_sensitive_action_needs_exact_scoped_approval_reply(self) -> None:
        calls: list[str] = []
        requested = False
        exact_resource = json.dumps(
            {"tool": "computer_write_file", "path": "report.txt"}, sort_keys=True
        )

        class FakeAgent:
            def __init__(self, memory: Memory) -> None:
                self.memory = memory

            def run(self, prompt, *, conversation_id, **kwargs):
                nonlocal requested
                calls.append(prompt)
                if "publish report" in prompt and not requested:
                    requested = True
                    authorized, approval_id = self.memory.authorize_or_request(
                        "publish_external", exact_resource, "publish the report",
                        approval_scope=f"conversation:{conversation_id}",
                    )
                    self_test.assertFalse(authorized)
                    return AgentResult(
                        "approval required", status="incomplete",
                        waiting_for_approval=True, approval_id=approval_id,
                        conversation_id=conversation_id,
                    )
                if "publish report" in prompt:
                    authorized, _approval_id = self.memory.authorize_or_request(
                        "publish_external", exact_resource, "publish the report",
                        approval_scope=f"conversation:{conversation_id}",
                    )
                    self_test.assertTrue(authorized)
                return AgentResult("completed after explicit approval")

        self_test = self
        runtime = GatewayRuntime(
            self.config, adapter=self.adapter,
            agent_factory=lambda _config, memory: FakeAgent(memory),
        )
        runtime.handle(InboundMessage("42", "publish report", "1"))
        pending = next(iter(runtime.state.pending.values()))
        approval_id = int(pending["approval_id"])
        prompt_text = "\n".join(text for _, text in self.adapter.sent)
        self.assertIn('{"path":"report.txt","tool":"computer_write_file"}', prompt_text)
        self.assertIn(f"approve {approval_id}", prompt_text)

        runtime.handle(InboundMessage("42", "yes", "2"))
        with Memory(self.data_dir / "jarvis.db") as memory:
            self.assertEqual(memory.get_approval(approval_id)["status"], "pending")

        runtime.handle(InboundMessage("42", f"approve {approval_id}", "3"))
        with Memory(self.data_dir / "jarvis.db") as memory:
            self.assertEqual(memory.get_approval(approval_id)["status"], "consumed")
        self.assertNotIn(next(iter(runtime.state.conversations)), runtime.state.pending)
        self.assertGreaterEqual(len(calls), 3)
        sent = "\n".join(text for _, text in self.adapter.sent)
        self.assertIn("Resuming the exact request", sent)
        self.assertIn("completed after explicit approval", sent)
