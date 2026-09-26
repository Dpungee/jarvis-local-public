"""Deterministic page clock: input on animating apps is attributed against a no-input control window."""
import functools
import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path

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


class NetworkExposureTests(unittest.TestCase):
    def test_loopback_only_server_is_not_exposed_and_all_interfaces_is(self):
        loopback = socket.socket()
        loopback.bind(("127.0.0.1", 0))
        loopback.listen()
        everywhere = socket.socket()
        everywhere.bind(("0.0.0.0", 0))
        everywhere.listen()
        try:
            self.assertFalse(reachable_beyond_loopback(loopback.getsockname()[1]))
            lan = [a for a in {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
                   if not a.startswith("127.")]
            if lan:
                self.assertTrue(reachable_beyond_loopback(everywhere.getsockname()[1]))
        finally:
            loopback.close()
            everywhere.close()


if __name__ == "__main__":
    unittest.main()
