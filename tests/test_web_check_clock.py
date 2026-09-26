"""Deterministic page clock: input on animating apps is attributed against a no-input control window."""
import functools
import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import call, patch

from jarvis import web_check
from jarvis.local_ports import reachable_beyond_loopback

# Gravity runs inside requestAnimationFrame, so the page changes on its own between inputs.
GAME = """<!doctype html><html><head><title>Probe game</title></head><body>
<canvas id="c" width="200" height="200"></canvas><p id="s">Score: 0</p>
<script>
const c = document.getElementById('c').getContext('2d'); let x = 90, y = 0, score = 0, last = 0;
function frame(t) { if (t - last > 400) { y = (y + 20) % 200; last = t; }
  c.fillStyle = '#111'; c.fillRect(0, 0, 200, 200); c.fillStyle = '#0f0'; c.fillRect(x, y, 20, 20);
  requestAnimationFrame(frame); }
requestAnimationFrame(frame);
document.addEventListener('keydown', e => { if (e.key === 'ArrowLeft') x -= 20; });
</script></body></html>"""

# Removes the check's clock: the check must report that it fell back to real time.
CLOCK_REPLACER = GAME.replace("<script>", "<script>window.__jarvisClock = undefined;")

# Force gravity due between the last scheduled RAF (1536 ms) and the first
# control endpoint (1548 ms). The phase must not depend on browser startup time.
ENDPOINT_GRAVITY = GAME.replace("last = 0", "last = performance.now() - 100")
FRAME_COUNT_MOTION = GAME.replace(
    "if (t - last > 400) { y = (y + 20) % 200; last = t; }",
    "y = (y + 20) % 200;",
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipIf(web_check.browser_executable() is None, "No Edge or Chrome installation")
class DeterministicClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        Path(cls.tmp.name, "game.html").write_text(GAME, encoding="utf-8")
        Path(cls.tmp.name, "replacer.html").write_text(CLOCK_REPLACER, encoding="utf-8")
        Path(cls.tmp.name, "endpoint.html").write_text(ENDPOINT_GRAVITY, encoding="utf-8")
        Path(cls.tmp.name, "frame-count.html").write_text(FRAME_COUNT_MOTION, encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=cls.tmp.name)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def test_animating_game_input_is_attributed_by_the_control_window(self):
        result = web_check.run_web_check(self.base + "game.html", actions=[
            {"type": "key", "key": "ArrowLeft"}, {"type": "key", "key": "q"}])
        self.assertEqual(result["page_clock"], "deterministic")
        self.assertTrue(result["animating_without_input"])
        left, unbound = result["steps"]
        self.assertTrue(left["control_window_quiet"])
        self.assertTrue(left["responded"])        # pixels only; no text changed
        self.assertFalse(left["text_changed"])
        self.assertIsNone(unbound.get("responded"))
        self.assertTrue(result["verified"])

    def test_page_that_removes_the_clock_is_reported_as_real_time(self):
        result = web_check.run_web_check(self.base + "replacer.html", actions=[
            {"type": "key", "key": "ArrowLeft"}])
        # The check says it could not control time, and applies the original real-time rule:
        # a screen change counts only when the page was still without input.
        self.assertNotEqual(result["page_clock"], "deterministic")
        self.assertNotIn("control_window_quiet", result["steps"][0])

    def test_control_endpoint_gravity_does_not_hide_a_real_input_response(self):
        result = web_check.run_web_check(self.base + "endpoint.html", actions=[
            {"type": "key", "key": "ArrowLeft"}, {"type": "key", "key": "q"}])
        left, unbound = result["steps"]
        self.assertEqual(result["page_clock"], "deterministic")
        self.assertTrue(left["control_window_quiet"])
        self.assertTrue(left["canvas_changed"])
        self.assertTrue(left["responded"])
        self.assertFalse(unbound.get("responded", False))
        self.assertEqual(result["responded_to_input"], ["step 1"])
        self.assertTrue(result["verified"])

    def test_endpoint_gravity_without_a_bound_key_is_not_an_input_response(self):
        result = web_check.run_web_check(self.base + "endpoint.html", actions=[
            {"type": "key", "key": "q"}])
        self.assertTrue(result["steps"][0]["control_window_quiet"])
        self.assertFalse(result["steps"][0]["canvas_changed"])
        self.assertEqual(result["responded_to_input"], [])
        self.assertFalse(result["verified"])

    def test_frame_count_motion_stays_ambiguous_even_at_frozen_time(self):
        result = web_check.run_web_check(self.base + "frame-count.html", actions=[
            {"type": "key", "key": "q"}])
        step = result["steps"][0]
        self.assertEqual(result["page_clock"], "deterministic")
        self.assertFalse(step["control_window_quiet"])
        self.assertTrue(step["canvas_changed"])
        self.assertFalse(step.get("responded", False))
        self.assertEqual(result["responded_to_input"], [])
        self.assertFalse(result["verified"])

    def test_unbound_input_never_gets_credit_for_the_next_gravity_tick(self):
        for settle in (780, 840, 920):
            with self.subTest(settle_ms=settle):
                result = web_check.run_web_check(self.base + "game.html", settle_ms=settle, actions=[
                    {"type": "key", "key": "q"},
                    {"type": "key", "key": "q"},
                    {"type": "key", "key": "q"},
                ])
                self.assertEqual(result["page_clock"], "deterministic")
                self.assertFalse(result["verified"])
                self.assertEqual(result["responded_to_input"], [])
                self.assertTrue(all(not step.get("responded") for step in result["steps"]))


class NetworkExposureTests(unittest.TestCase):
    def test_loopback_only_server_is_not_exposed(self):
        with socket.socket() as loopback:
            loopback.bind(("127.0.0.1", 0))
            loopback.listen()
            self.assertFalse(reachable_beyond_loopback(loopback.getsockname()[1]))

    def test_nonloopback_connection_is_detected_without_exposing_a_listener(self):
        # Mock the socket boundary rather than opening a listener to the LAN.
        # Documentation-only addresses never leave this fixture.
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 0))
                     for host in ("127.0.0.1", "198.51.100.10", "198.51.100.10")]
        with patch("jarvis.local_ports.socket.gethostname", return_value="test-host"), \
             patch("jarvis.local_ports.socket.getaddrinfo", return_value=addresses) as resolve, \
             patch("jarvis.local_ports.socket.create_connection") as connect:
            self.assertTrue(reachable_beyond_loopback(12345))
        resolve.assert_called_once_with("test-host", None, socket.AF_INET)
        connect.assert_called_once_with(("198.51.100.10", 12345), timeout=0.3)
        connect.return_value.__enter__.assert_called_once_with()
        connect.return_value.__exit__.assert_called_once()

    def test_refused_nonloopback_connections_are_not_exposure(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 0))
                     for host in ("198.51.100.11", "127.0.0.1", "198.51.100.10")]
        with patch("jarvis.local_ports.socket.getaddrinfo", return_value=addresses), \
             patch("jarvis.local_ports.socket.create_connection", side_effect=OSError) as connect:
            self.assertFalse(reachable_beyond_loopback(12345))
        self.assertEqual(connect.call_args_list, [
            call(("198.51.100.10", 12345), timeout=0.3),
            call(("198.51.100.11", 12345), timeout=0.3),
        ])

    def test_address_resolution_failure_does_not_attempt_a_connection(self):
        with patch("jarvis.local_ports.socket.getaddrinfo", side_effect=OSError), \
             patch("jarvis.local_ports.socket.create_connection") as connect:
            self.assertFalse(reachable_beyond_loopback(12345))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
