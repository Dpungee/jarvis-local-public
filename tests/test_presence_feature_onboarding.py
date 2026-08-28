from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from jarvis.feature_onboarding import FeatureOnboardingStore
from jarvis.presence import PresenceHTTPServer, PresenceRuntime


ROOT = Path(__file__).resolve().parents[1]


def _runtime(
    root: Path,
    data_dir: Path,
    *,
    network_access: str = "disabled",
    network_monitor_enabled: bool = False,
    network_defense_mode: str = "disabled",
    bluetooth_access: str = "disabled",
    bluetooth_monitor_enabled: bool = False,
    network_incident_popups_enabled: bool = False,
) -> PresenceRuntime:
    runtime = PresenceRuntime.__new__(PresenceRuntime)
    runtime.config = SimpleNamespace(
        root=root,
        data_dir=data_dir,
        network_access=network_access,
        network_monitor_enabled=network_monitor_enabled,
        network_defense_mode=network_defense_mode,
        bluetooth_access=bluetooth_access,
        bluetooth_monitor_enabled=bluetooth_monitor_enabled,
        network_incident_popups_enabled=network_incident_popups_enabled,
    )
    runtime._feature_onboarding = FeatureOnboardingStore(root, data_dir)
    runtime._feature_onboarding_error = None
    runtime.emitted_feature_events = []
    runtime.emit = lambda kind, **payload: runtime.emitted_feature_events.append(
        (kind, payload)
    )
    return runtime


class PresenceFeatureOnboardingHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "jarvis"
        self.data_dir = self.root / "data"
        self.root.mkdir(parents=True)
        self.runtime = _runtime(self.root, self.data_dir)
        self.server = PresenceHTTPServer(("127.0.0.1", 0), self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path: str, *, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        return urllib.request.urlopen(request, timeout=2)

    def feature(self, status: dict, capability_id: str) -> dict:
        return next(
            row
            for row in status["features"]
            if row["capability_id"] == capability_id
        )

    def test_get_and_post_return_exact_reversible_feature_state(self):
        with self.request("/api/feature-onboarding") as response:
            before = json.load(response)
        self.assertTrue(before["available"])
        self.assertEqual(before["pending_count"], 7)
        self.assertFalse(before["downloads_performed"])
        self.assertFalse(before["active_probes_performed"])
        self.assertFalse(before["containment_authorized"])
        popup = self.feature(before, "network-security-alerts-ui")
        self.assertFalse(popup["effective_now"])
        self.assertFalse(popup["restart_pending"])

        with self.request(
            "/api/feature-onboarding/decision",
            payload={
                "capability_id": "private-lan-inventory",
                "decision": "setup",
                "expected_configuration_sha256": before["configuration_sha256"],
            },
        ) as response:
            changed = json.load(response)
        self.assertEqual(changed["result"]["decision"], "setup")
        self.assertTrue(changed["result"]["restart_required"])
        self.assertFalse(changed["result"]["downloads_performed"])
        feature = self.feature(changed["status"], "private-lan-inventory")
        self.assertTrue(feature["configured"])
        self.assertFalse(feature["effective_now"])
        self.assertTrue(feature["restart_pending"])
        self.assertIn("JARVIS_NETWORK_ACCESS=private-lan", (self.root / ".env").read_text())
        self.assertEqual(
            self.runtime.emitted_feature_events[-1][0],
            "feature_setup_changed",
        )

        # The same durable configuration is effective, with no pending restart,
        # after the running configuration reflects a restart.
        self.runtime.config.network_access = "private-lan"
        with self.request("/api/feature-onboarding") as response:
            restarted = json.load(response)
        feature = self.feature(restarted, "private-lan-inventory")
        self.assertTrue(feature["configured"])
        self.assertTrue(feature["effective_now"])
        self.assertFalse(feature["restart_pending"])

    def test_stale_configuration_hash_is_a_conflict_and_does_not_apply(self):
        with self.request("/api/feature-onboarding") as response:
            before = json.load(response)
        (self.root / ".env").write_text("UNRELATED_SETTING=preserved\n", encoding="utf-8")

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "/api/feature-onboarding/decision",
                payload={
                    "capability_id": "bluetooth-inventory",
                    "decision": "setup",
                    "expected_configuration_sha256": before["configuration_sha256"],
                },
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("refresh", error["error"].casefold())
        contents = (self.root / ".env").read_text(encoding="utf-8")
        self.assertEqual(contents, "UNRELATED_SETTING=preserved\n")


class PresenceFeatureOnboardingUIStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = (ROOT / "jarvis" / "presence.html").read_text(encoding="utf-8")
        cls.style = (ROOT / "jarvis" / "presence.css").read_text(encoding="utf-8")
        cls.script = (ROOT / "jarvis" / "presence.js").read_text(encoding="utf-8")
        cls.backend = (ROOT / "jarvis" / "presence.py").read_text(encoding="utf-8")

    def function_block(self, start: str, end: str) -> str:
        begin = self.script.index(f"function {start}")
        finish = self.script.index(f"function {end}", begin)
        return self.script[begin:finish]

    def async_function_block(self, start: str, end: str) -> str:
        begin = self.script.index(f"async function {start}")
        finish = self.script.index(f"function {end}", begin)
        return self.script[begin:finish]

    def test_first_run_dialog_and_settings_catalog_remain_accessible(self):
        self.assertIn('data-view="customize"><span class="nav-icon">☷</span><span>Settings</span>', self.page)
        self.assertIn('id="feature-onboarding-dialog"', self.page)
        self.assertIn('id="feature-onboarding-list"', self.page)
        self.assertIn('id="close-feature-onboarding"', self.page)
        self.assertIn("Set features up now, leave them for later, or keep them disabled", self.page)
        self.assertIn(".feature-onboarding-dialog", self.style)
        self.assertIn(".feature-onboarding-actions", self.style)
        self.assertIn("@media (max-width: 760px)", self.style)

        first_run = self.function_block(
            "maybeShowFeatureOnboarding",
            "renderCustomize",
        )
        self.assertIn("state.onboardingDismissedForSession", first_run)
        self.assertIn("state.featureOnboarding.pending_count", first_run)
        self.assertIn('$("approval-dialog").open', first_run)
        self.assertIn("renderFeatureOnboardingDialog()", first_run)
        self.assertIn("dialog.showModal()", first_run)
        self.assertIn("schedulePriorityDialogs();", self.script)
        self.assertIn(
            '$("approval-dialog").addEventListener("close", schedulePriorityDialogs)',
            self.script,
        )

        settings = self.async_function_block("renderCustomize", "applyStatus")
        self.assertIn("await refreshFeatureOnboarding()", settings)
        self.assertIn("Optional capabilities", settings)
        self.assertIn("featureOnboardingCard(feature)", settings)
        self.assertIn("Set up or turn off any implemented capability at any time", settings)

    def test_feature_content_is_rendered_as_text_and_decisions_are_bounded(self):
        renderer = self.function_block("featureOnboardingCard", "refreshFeatureOnboarding")
        self.assertIn("title.textContent", renderer)
        self.assertIn("description.textContent", renderer)
        self.assertIn("safety.textContent", renderer)
        self.assertIn("pairing.textContent", renderer)
        self.assertNotIn("innerHTML", renderer)
        self.assertNotIn("insertAdjacentHTML", renderer)
        self.assertNotIn("eval(", renderer)
        self.assertNotIn("new Function", renderer)
        self.assertNotIn("innerHTML", self.script)

        decision = self.async_function_block(
            "decideOptionalFeature",
            "featureOnboardingCard",
        )
        self.assertIn('post("/api/feature-onboarding/decision"', decision)
        self.assertIn("expected_configuration_sha256", decision)
        self.assertIn("error?.status === 409", decision)
        self.assertIn("savedFeature?.restart_pending", decision)
        self.assertIn("setup.disabled = busy || feature.configured", renderer)
        for exact_decision in ('"setup"', '"skip"', '"disable"'):
            self.assertIn(exact_decision, renderer)

    def test_modal_scheduler_preserves_operator_priority_and_resumes(self):
        scheduler = self.function_block(
            "schedulePriorityDialogs",
            "showNetworkDefenseIncidents",
        )
        approval = scheduler.index('$("approval-dialog").open')
        onboarding = scheduler.index("maybeShowFeatureOnboarding()")
        network = scheduler.index('$("new-network-device-dialog")')
        bluetooth = scheduler.index('$("new-bluetooth-device-dialog")')
        defense = scheduler.index("maybeShowNetworkDefenseIncidents()")
        self.assertLess(approval, onboarding)
        self.assertLess(onboarding, network)
        self.assertLess(network, bluetooth)
        self.assertLess(bluetooth, defense)
        self.assertIn(
            '$("approval-dialog").addEventListener("close", schedulePriorityDialogs)',
            self.script,
        )
        self.assertIn(
            '$("feature-onboarding-dialog").addEventListener("close", schedulePriorityDialogs)',
            self.script,
        )

    def test_popup_preference_suppresses_backend_events_and_frontend_replay(self):
        frontend = self.function_block(
            "showNetworkDefenseIncidents",
            "showNewNetworkDeviceAlerts",
        )
        self.assertIn("state.networkInventory?.incident_popups_enabled === false", frontend)
        self.assertIn("state.networkDefenseIncidents.clear()", frontend)
        self.assertIn("if (dialog.open) dialog.close()", frontend)
        self.assertIn("return;", frontend)

        scan_begin = self.backend.index("    def _scan_network_inventory_locked(")
        scan_end = self.backend.index("    def set_network_device_profile(", scan_begin)
        scan = self.backend[scan_begin:scan_end]
        guard = 'if bool(getattr(self.config, "network_incident_popups_enabled", False)):'
        self.assertIn(guard, scan)
        self.assertLess(scan.index(guard), scan.index('"network_defense_incident"'))
        self.assertIn('"incident_popups_enabled": bool(', self.backend)


if __name__ == "__main__":
    unittest.main()
