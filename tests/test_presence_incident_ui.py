import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PresenceIncidentUIStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "jarvis" / "presence.html").read_text(encoding="utf-8")
        cls.style = (ROOT / "jarvis" / "presence.css").read_text(encoding="utf-8")
        cls.script = (ROOT / "jarvis" / "presence.js").read_text(encoding="utf-8")

    def function_block(self, start, end):
        begin = self.script.index(f"function {start}")
        finish = self.script.index(f"function {end}", begin)
        return self.script[begin:finish]

    def test_incident_dialog_and_responsive_layout_exist(self):
        self.assertIn('id="network-defense-incident-dialog"', self.page)
        self.assertIn('id="network-defense-incident-list"', self.page)
        self.assertIn('id="close-network-defense-incident"', self.page)
        self.assertIn(".network-defense-incident-facts", self.style)
        self.assertIn(
            ".new-network-device-facts, .network-defense-incident-facts",
            self.style,
        )

    def test_renderer_uses_text_content_and_covers_explanation_contract(self):
        renderer = self.function_block(
            "renderNetworkDefenseIncidents",
            "maybeShowNetworkDefenseIncidents",
        )
        self.assertIn(".textContent", renderer)
        self.assertNotIn(".innerHTML", renderer)
        self.assertNotIn("insertAdjacentHTML", renderer)
        for label in (
            "Detected signal",
            "Evidence",
            "Confidence",
            "What Jarvis automatically did",
            "What Jarvis did not do",
            "Recommended action",
            "Time",
            "Receipt",
        ):
            self.assertIn(label, renderer)

    def test_only_explicit_reviewable_actions_render_approval_buttons(self):
        normalizer = self.function_block(
            "normalizeNetworkDefenseIncident",
            "legacyNetworkDefenseIncidents",
        )
        renderer = self.function_block(
            "renderNetworkDefenseIncidents",
            "maybeShowNetworkDefenseIncidents",
        )
        self.assertIn('raw.approval?.required === true', normalizer)
        self.assertIn("Number.isSafeInteger(actionId)", normalizer)
        self.assertIn('!/^[0-9a-f]{32}$/.test(incidentId)', normalizer)
        self.assertIn("deviceRaw.device_type || deviceRaw.type", normalizer)
        self.assertIn('incident.approval?.required === true', renderer)
        self.assertIn("decideApproval(incident.approval.actionId", renderer)

    def test_signed_ack_and_sse_contract_are_wired(self):
        self.assertIn(
            'post("/api/network-defense/incidents/acknowledge"',
            self.script,
        )
        self.assertIn("incident_id: incident.incidentId", self.script)
        self.assertIn("receipt_id: incident.receiptId", self.script)
        self.assertIn('event.kind === "network_defense_incident"', self.script)
        self.assertIn("payload.pending_incidents || payload.incident", self.script)
        self.assertIn("legacyNetworkDefenseIncidents", self.script)

    def test_tool_readiness_ui_states_the_automatic_safety_ceiling(self):
        renderer = self.function_block("renderNetworkDefense", "bluetoothDeviceName")
        self.assertIn("Defensive tool readiness", renderer)
        self.assertIn("allowlisted passive local diagnostics", renderer)
        self.assertIn("must be wired to an exact approval before use", renderer)
        self.assertIn("cannot apply blocking, quarantine, or firewall changes", renderer)
        self.assertIn("row.textContent", renderer)
        self.assertNotIn("innerHTML", renderer)


if __name__ == "__main__":
    unittest.main()
