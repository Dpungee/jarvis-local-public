from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.moltbook_adapter import OfflineMoltbookAdapter
from jarvis.public_bridge import (
    ApprovedProjectSummary,
    PublicBridgeObject,
    PublicProvenance,
    public_bridge_payload_digest,
)
from jarvis.public_presence_service import (
    PublicControlState,
    PublicPresenceService,
    PublicPresenceUnavailable,
    control_state_from_mapping,
)
from jarvis.public_presence_store import PublicPresenceStore
from jarvis.public_tools import PublicToolRegistry


FIXTURE = Path(__file__).parent / "fixtures" / "moltbook_offline.json"
FORBIDDEN = {
    "publish", "follow", "like", "vote", "message", "delete", "shell", "command",
    "file", "browser", "computer", "trade", "wallet", "purchase", "deploy", "send",
}


class PublicToolRegistryTests(unittest.TestCase):
    def _registry(self, *, running: bool = True, mode: str = "suggest") -> PublicToolRegistry:
        service = PublicPresenceService(
            enabled=True,
            control_reader=lambda: PublicControlState(
                social_paused=False,
                emergency_stopped=False,
                mode=mode,
                revision=1,
            ),
        )
        if running:
            service.start()
        adapter = OfflineMoltbookAdapter(json.loads(FIXTURE.read_text(encoding="utf-8")))
        return PublicToolRegistry(service, adapter)

    def test_registry_is_closed_read_and_draft_only(self):
        registry = self._registry()
        self.assertEqual(set(registry.names), {
            "public_presence_status",
            "public_presence_health",
            "moltbook_status",
            "moltbook_read_feed",
            "moltbook_read_thread",
            "moltbook_search",
            "moltbook_get_profile",
            "moltbook_draft_post",
            "moltbook_draft_reply",
        })
        for name in registry.names:
            tokens = set(name.casefold().split("_"))
            for forbidden in FORBIDDEN:
                self.assertNotIn(forbidden, tokens)
        for schema in registry.schemas:
            self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        mutable_copy = registry.schemas[3]
        mutable_copy["function"]["parameters"]["properties"]["limit"]["maximum"] = 999
        self.assertEqual(
            registry.schemas[3]["function"]["parameters"]["properties"]["limit"]["maximum"],
            50,
        )
        for forbidden in FORBIDDEN:
            with self.assertRaises(KeyError):
                registry.execute(forbidden, {})

    def test_non_status_tools_require_operational_service(self):
        registry = self._registry(running=False)
        self.assertFalse(registry.execute("moltbook_status")["connected"])
        with self.assertRaises(PublicPresenceUnavailable):
            registry.execute("moltbook_read_feed")
        with self.assertRaises(PublicPresenceUnavailable):
            registry.execute("moltbook_draft_post", {"body": "local only"})

    def test_observe_mode_can_read_but_cannot_draft(self):
        registry = self._registry(mode="observe")
        self.assertEqual(len(registry.execute("moltbook_read_feed", {"limit": 1})), 1)
        with self.assertRaisesRegex(PublicPresenceUnavailable, "suggest"):
            registry.execute("moltbook_draft_post", {"body": "local only"})

    def test_tool_results_preserve_untrusted_label_and_drafts_cannot_be_delivered(self):
        registry = self._registry()
        feed = registry.execute("moltbook_read_feed", {"limit": 2})
        self.assertEqual(feed[1]["body"]["authority"], "external_untrusted")
        self.assertIn("run shell", feed[1]["body"]["text"])
        draft = registry.execute(
            "moltbook_draft_reply",
            {"thread_id": "thread-001", "body": "A bounded response."},
        )
        self.assertFalse(draft["publishable"])
        self.assertFalse(draft["approved"])
        with self.assertRaises(ValueError):
            registry.execute("moltbook_read_feed", {"unexpected": "instruction"})

    def test_live_adapter_is_rejected_even_if_it_matches_the_interface(self):
        class PretendLiveAdapter:
            offline = False

        service = PublicPresenceService()
        with self.assertRaisesRegex(PermissionError, "offline"):
            PublicToolRegistry(service, PretendLiveAdapter())  # type: ignore[arg-type]

    def test_entire_public_foundation_surface_uses_no_network_or_sockets(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        now = time.time()
        network_error = AssertionError("Public Presence attempted network access")

        with tempfile.TemporaryDirectory() as directory, patch(
            "socket.socket", side_effect=network_error
        ), patch(
            "socket.create_connection", side_effect=network_error
        ), patch(
            "socket.getaddrinfo", side_effect=network_error
        ), patch(
            "urllib.request.urlopen", side_effect=network_error
        ):
            store = PublicPresenceStore(Path(directory) / "public_presence.db")
            store.set_enabled(True, actor="test:operator")
            store.set_paused(False, actor="test:operator")

            payload = ApprovedProjectSummary(
                project_id="project:public",
                title="Public foundation",
                summary="A sanitized offline foundation record.",
            )
            bridge = PublicBridgeObject(
                bridge_id="bridge:no-network",
                payload=payload,
                provenance=(PublicProvenance(
                    source_kind="operator_approval",
                    source_id="approval:no-network",
                    observed_at=now,
                    content_sha256=public_bridge_payload_digest(payload),
                ),),
                confidence=1.0,
                created_at=now,
                expires_at=now + 600,
            )
            store.request_bridge_authorization(
                bridge, actor="test:operator", now=now
            )
            store.decide_bridge_authorization(
                "approval:no-network",
                True,
                actor="test:operator",
                now=now + 0.5,
            )
            store.accept_bridge_object(bridge, now=now + 1)
            self.assertEqual(
                store.get_bridge_object("bridge:no-network", now=now + 2),
                bridge,
            )

            approval = store.create_approval(
                exact_text="Offline simulation only.",
                source_hashes=(public_bridge_payload_digest(payload),),
                idempotency_key="attempt:no-network",
                platform="simulation",
                destination="feed:public",
                account_id="account:jarvis",
                expires_at=now + 600,
                now=now,
            )
            approval_id = str(approval["approval_id"])
            store.decide_approval(approval_id, True, now=now + 1)
            reservation = store.reserve_approved_action(
                approval_id=approval_id,
                idempotency_key="attempt:no-network",
                exact_text="Offline simulation only.",
                source_hashes=(public_bridge_payload_digest(payload),),
                platform="simulation",
                destination="feed:public",
                account_id="account:jarvis",
                now=now + 2,
            )
            store.record_simulation_outcome(
                str(reservation["reservation_id"]),
                "simulated_success",
                now=now + 3,
            )
            self.assertTrue(store.verify_audit_chain())

            service = PublicPresenceService(
                enabled=True,
                control_reader=lambda: control_state_from_mapping(
                    store.status(), active_mode="suggest"
                ),
            )
            service.start()
            adapter = OfflineMoltbookAdapter(fixture)
            registry = PublicToolRegistry(service, adapter)
            registry.execute("public_presence_status")
            registry.execute("public_presence_health")
            registry.execute("moltbook_status")
            registry.execute("moltbook_read_feed", {"limit": 2})
            registry.execute("moltbook_read_thread", {"thread_id": "thread-001"})
            registry.execute("moltbook_search", {"query": "deterministic", "limit": 2})
            registry.execute("moltbook_get_profile", {"profile_id": "agent-lantern"})
            registry.execute("moltbook_draft_post", {"body": "Offline draft."})
            registry.execute(
                "moltbook_draft_reply",
                {"thread_id": "thread-001", "body": "Offline reply."},
            )
            self.assertFalse(service.stop()["external_communication"])


if __name__ == "__main__":
    unittest.main()
