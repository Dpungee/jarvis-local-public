"""Synthetic startup readiness and containment checks; no browser is launched."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis import web_check


MARKER = "12345\n/devtools/browser/00000000-0000-0000-0000-000000000000\n"


class StartupMarkerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.sleeps = []
        clock = patch("jarvis.web_check.time.monotonic", side_effect=lambda: self.now)
        sleep = patch("jarvis.web_check.time.sleep", side_effect=self.advance)
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)

    def advance(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def test_complete_marker_is_returned_without_a_fixed_delay(self):
        marker = Mock(read_text=Mock(return_value=MARKER))
        self.assertEqual(web_check._wait_for_devtools_port(marker, 120),
                         (12345, MARKER.split()[1]))
        marker.read_text.assert_called_once_with(encoding="utf-8")
        self.assertEqual(self.sleeps, [])

    def test_transient_denial_disappearance_and_partial_marker_recover(self):
        marker = Mock(read_text=Mock(side_effect=[
            PermissionError("writer sharing"), FileNotFoundError(), "12345\n", "", MARKER]))
        self.assertEqual(web_check._wait_for_devtools_port(marker, 120),
                         (12345, MARKER.split()[1]))
        self.assertEqual(marker.read_text.call_count, 5)
        self.assertEqual(self.sleeps, [0.05] * 4)

    def test_invalid_marker_is_never_returned(self):
        invalid = ["0\n/devtools/browser/x", "65536\n/devtools/browser/x",
                   "-1\n/devtools/browser/x", "12345\n/", "12345\nhttp://example.invalid/",
                   "12345\n/devtools/browser/x\nextra", "12345\n/devtools/browser/x?query",
                   "１２３\n/devtools/browser/x"]
        for value in invalid:
            with self.subTest(marker=value):
                marker = Mock(read_text=Mock(side_effect=[value, MARKER]))
                self.assertEqual(web_check._wait_for_devtools_port(marker, self.now + 1),
                                 (12345, MARKER.split()[1]))
                self.assertEqual(marker.read_text.call_count, 2)

    def test_partial_utf8_recovers(self):
        marker = Mock(read_text=Mock(side_effect=[
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "partial marker"), MARKER]))
        self.assertEqual(web_check._wait_for_devtools_port(marker, 120)[0], 12345)

    def test_permanent_denial_missing_or_malformed_marker_reaches_deadline(self):
        for outcome in (PermissionError(), FileNotFoundError(), "12345\n", "not a marker"):
            with self.subTest(outcome=type(outcome).__name__):
                self.now = 100.0
                self.sleeps.clear()
                reader = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                marker = Mock(read_text=reader)
                with self.assertRaisesRegex(web_check.WebCheckError, "did not start"):
                    web_check._wait_for_devtools_port(marker, 100.12)
                self.assertEqual(self.now, 100.12)
                self.assertEqual(reader.call_count, 3)
                self.assertLessEqual(max(self.sleeps), 0.05)
                self.assertLess(self.sleeps[-1], 0.05)

    def test_expired_budget_does_not_read_or_sleep(self):
        marker = Mock()
        with self.assertRaises(web_check.WebCheckError):
            web_check._wait_for_devtools_port(marker, 100)
        marker.read_text.assert_not_called()
        self.assertEqual(self.sleeps, [])

    def test_read_completing_after_deadline_cannot_succeed(self):
        def delayed_read(**kwargs):
            self.now = 121.0
            return MARKER
        marker = Mock(read_text=Mock(side_effect=delayed_read))
        with self.assertRaises(web_check.WebCheckError):
            web_check._wait_for_devtools_port(marker, 120)
        self.assertEqual(self.sleeps, [])

    def test_unrelated_io_error_is_not_retried(self):
        marker = Mock(read_text=Mock(side_effect=OSError("device error")))
        with self.assertRaisesRegex(OSError, "device error"):
            web_check._wait_for_devtools_port(marker, 120)
        self.assertEqual(marker.read_text.call_count, 1)
        self.assertEqual(self.sleeps, [])


@unittest.skipUnless(web_check.os.name == "nt", "Windows process containment contract")
class StartupContainmentTests(unittest.TestCase):
    def test_startup_cap_and_shorter_total_deadline_preserve_cleanup(self):
        for timeout, expected_deadline in ((60, 120), (5, 105)):
            with self.subTest(timeout=timeout):
                process = Mock()
                job = Mock(handle=1)
                with patch("jarvis.web_check.time.monotonic", return_value=100), \
                     patch("jarvis.web_check.subprocess.Popen", return_value=process) as launch, \
                     patch("jarvis.execution.WindowsJob", return_value=job) as contain, \
                     patch("jarvis.execution._resume_windows_process") as resume, \
                     patch("jarvis.execution._terminate_process_tree") as terminate, \
                     patch("jarvis.web_check._wait_for_devtools_port", side_effect=web_check.WebCheckError(
                         "The headless browser did not start.")) as wait, \
                     patch("jarvis.web_check._WebSocket") as connect:
                    with self.assertRaisesRegex(web_check.WebCheckError, "did not start"):
                        web_check.run_web_check("http://127.0.0.1:8765/", timeout_seconds=timeout,
                                                browser=Path("synthetic-browser.exe"))
                marker, deadline = wait.call_args.args
                self.assertEqual(marker.name, "DevToolsActivePort")
                self.assertEqual(deadline, expected_deadline)
                self.assertFalse(marker.parent.exists())
                contain.assert_called_once_with(process)
                resume.assert_called_once_with(process)
                terminate.assert_called_once_with(process, job)
                process.wait.assert_called_once_with(timeout=10)
                connect.assert_not_called()
                self.assertTrue(launch.call_args.kwargs["creationflags"] & 0x00000004)


if __name__ == "__main__":
    unittest.main()
