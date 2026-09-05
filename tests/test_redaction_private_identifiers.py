"""The widened private-identifier screen (VTMF M3 §6.2).

Both corpora are the design's, verbatim: 28 positives with their kind and 44
negatives, 72 cases.  The wall-clock bound is measured at the product's real
maximum (a claim value is bounded to 4,000 characters) and enforced under
JARVIS_ENFORCE_TIMING_GATES=1; the scan cap that makes it hold is asserted
unconditionally, needing no clock, and purity is asserted because the sealed
holdout, the release-policy checker and the read path all have to agree on
one answer.
"""
from __future__ import annotations

import importlib
import os
import time
import unittest
from typing import Any

from jarvis import redaction

# --- the pinned corpora ------------------------------------------------------

POSITIVES: tuple[tuple[str, str], ...] = (
    ("alice@example.com", "email"),
    ("Contact ops at alice@example.com for the relay", "email"),
    ("C:\\Users\\example-user\\Documents\\notes.txt", "user_home"),
    ("/home/example-user/notes.txt", "user_home"),
    ("\\\\example-server\\Users\\example-user\\share", "user_home"),
    ("admin@10.0.0.7", "ip_host_email"),
    ("ops@[192.168.4.11]", "ip_host_email"),
    ("relay@[IPv6:2607:f8b0:4005:80c::200e]", "ip_host_email"),
    ("+1 (415) 555-0199", "phone"),
    ("415-555-0199", "phone"),
    ("+44 20 7946 0958", "phone"),
    ("(212) 555 0147", "phone"),
    ("call the operator on 415 555 0199 today", "phone"),
    ("10.0.0.7", "ipv4"),
    ("192.168.4.11", "ipv4"),
    ("172.16.31.9", "ipv4"),
    ("8.8.8.8", "ipv4"),
    ("the box answers on 10.0.0.7 today", "ipv4"),
    ("fe80::1ff:fe23:4567:890a", "ipv6"),
    ("2607:f8b0:4005:80c::200e", "ipv6"),
    ("fd00:1234:5678:9abc:def0:1234:5678:9abc", "ipv6"),
    ("123-45-6789", "ssn"),
    ("4111 1111 1111 1111", "card"),
    ("5500-0000-0000-0004", "card"),
    ("221B Baker Street", "street_address"),
    ("1600 Pennsylvania Avenue", "street_address"),
    ("12 Main St", "street_address"),
    ("ship it to 350 Fifth Ave", "street_address"),
)

NEGATIVES: tuple[str, ...] = (
    "Fenwick", "Harrier box", "Kestrel relay", "Kestrel relay 2",
    "Northgate region", "deployed on host Harrier box", "v2.3.1", "9090",
    "listen port 9090", "2026-09-03", "2026-09-03T14:22:05Z",
    "192.168.0.0/16", "release 1.2.3.4", "build 2024.10.1", "port 8080-8090",
    "ISBN 978-3-16-148410-0", "00:1A:2B:3C:4D:5E", "12:30:15", "127.0.0.1",
    "0.0.0.0", "255.255.255.255", "192.0.2.15", "198.51.100.7",
    "203.0.113.42", "239.255.255.250", "::1", "2001:db8::1",
    "4111 1111 1111 1112", "order 1234567890", "1.2.3", "42 Elm",
    "Kestrel Way", "rack 12-04-08", "978-3-16", "v1.2.3.4", "1.2.3.4-rc1",
    "550e8400-e29b-41d4-a716-446655440000", "schema 46", "SKU 1234-5678",
    "uptime 99.999", "https://example.com/docs/2026/09/03",
    "the relay moved to the Fenwick datacenter in Northgate",
    "9 boxes in rack 12", "region Northgate, capacity 2000",
)

# The red team of 2026-09-03 found that a generic negative-context net
# exempted every widened kind at once: a word in front of a credential made it
# stop screening.  Each of these was (False, None) and became a graph entity.
RED_TEAM_POSITIVES: tuple[tuple[str, str], ...] = (
    ("10.0.0.7/32", "ipv4"),                       # /32 is one host
    ("rack 10.0.0.7", "ipv4"),
    ("v10.0.0.7", "ipv4"),                         # no version is 10.0.0.7
    ("case 078-05-1120", "ssn"),
    ("invoice 4111 1111 1111 1111", "card"),
    ("port 415-555-0199", "phone"),
    ("ops@example.com-rc1", "email"),
    ("ship to 221B Baker Street now", "street_address"),
    ("fe80::1ff:fe23:4567:890a/128", "ipv6"),      # /128 is one host
)

ADVERSARIAL: tuple[str, ...] = (
    "1" * 4_000,
    ("1234-" * 800)[:4_000],
    ("abcd:" * 800)[:4_000],
    ("(123) " * 700)[:4_000],
    ("192.168.1.1 " * 400)[:4_000],
    ("+1 (415) 555-0199 v1.2.3.4 fe80::1 " * 200)[:4_000],
    ("a" * 3_999) + "@",
)


class WidenedPrivateIdentifierBatteryTests(unittest.TestCase):
    def test_positive_corpus_returns_the_named_kind(self) -> None:
        self.assertEqual(len(POSITIVES), 28)
        for text, expected in POSITIVES:
            with self.subTest(text=text):
                self.assertEqual(redaction.private_identifier_kind(text), expected)
                self.assertTrue(redaction.contains_private_identifier_extended(text))
                self.assertEqual(redaction.screen_endpoint(text), (True, expected))

    def test_negative_corpus_returns_none(self) -> None:
        self.assertEqual(len(NEGATIVES), 44)
        for text in NEGATIVES:
            with self.subTest(text=text):
                self.assertIsNone(redaction.private_identifier_kind(text))
                self.assertFalse(redaction.contains_private_identifier_extended(text))
                self.assertEqual(redaction.screen_endpoint(text), (False, None))

    def test_every_kind_is_in_the_closed_set(self) -> None:
        self.assertEqual(
            {kind for _text, kind in POSITIVES} | {"long_value"},
            set(redaction.PRIVATE_IDENTIFIER_KINDS),
        )

    def test_red_team_corpus_screens_with_its_kind(self) -> None:
        for text, expected in RED_TEAM_POSITIVES:
            with self.subTest(text=text):
                self.assertEqual(redaction.private_identifier_kind(text), expected)
                self.assertEqual(redaction.screen_endpoint(text), (True, expected))

    def test_only_ipv4_and_phone_have_any_context_exemption(self) -> None:
        # A word in front of a credential does not make it safe.
        for word in ("case", "invoice", "order", "rack", "sku", "ticket", "batch",
                     "serial", "asset", "version", "release", "build"):
            for text, kind in (
                ("078-05-1120", "ssn"),
                ("4111 1111 1111 1111", "card"),
                ("221B Baker Street", "street_address"),
                ("fe80::1ff:fe23:4567:890a", "ipv6"),
            ):
                with self.subTest(word=word, kind=kind):
                    self.assertEqual(
                        redaction.private_identifier_kind(f"{word} {text}"), kind
                    )

    def test_a_private_quad_is_never_a_version_number(self) -> None:
        for prefix in ("version", "v", "build", "release", "api", "sdk", "schema"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    redaction.private_identifier_kind(f"{prefix} 10.0.0.7"), "ipv4"
                )
                # A public quad stays exempt under the same words.
                self.assertIsNone(
                    redaction.private_identifier_kind(f"{prefix} 1.2.3.4")
                )

    def test_a_cidr_network_is_exempt_but_a_single_host_is_not(self) -> None:
        for bits in (0, 8, 16, 24, 31):
            with self.subTest(bits=bits):
                self.assertIsNone(
                    redaction.private_identifier_kind(f"192.168.0.0/{bits}")
                )
        self.assertEqual(redaction.private_identifier_kind("10.0.0.7/32"), "ipv4")

    def test_a_phone_is_exempt_only_below_ten_digits_or_as_an_isbn(self) -> None:
        self.assertIsNone(redaction.private_identifier_kind("port 8080-8090"))
        self.assertIsNone(redaction.private_identifier_kind("ISBN 978-3-16-148410-0"))
        self.assertIsNone(redaction.private_identifier_kind("978-3-16-148410-0"))
        self.assertEqual(redaction.private_identifier_kind("port 415-555-0199"), "phone")

    def test_cidr_block_is_a_network_not_a_host(self) -> None:
        # A network is not a private identifier; the /nn suffix window kills it.
        self.assertIsNone(redaction.private_identifier_kind("192.168.0.0/16"))
        self.assertEqual(redaction.private_identifier_kind("192.168.0.9"), "ipv4")

    def test_declared_misses_are_still_misses(self) -> None:
        # Recorded in the design so a later reader does not mistake them for
        # regressions: an all-letter IPv6 and a dotted phone number.
        self.assertIsNone(redaction.private_identifier_kind("dead::beef"))
        self.assertIsNone(redaction.private_identifier_kind("415.555.0199"))


class ScanLimitTests(unittest.TestCase):
    def test_no_regex_ever_sees_more_than_the_scan_limit(self) -> None:
        seen: list[int] = []
        original = redaction._scan

        def recording(text: str) -> str | None:
            seen.append(len(text))
            return original(text)

        redaction._scan = recording  # type: ignore[assignment]
        try:
            redaction.private_identifier_kind("x" * 40_000)
            redaction.private_identifier_kind("Harrier box")
        finally:
            redaction._scan = original  # type: ignore[assignment]
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), redaction.SCAN_LIMIT)

    def test_the_last_scan_limit_characters_get_the_full_kind_set(self) -> None:
        # The red team hid a bare IPv4, a street address and a user-home path
        # past character 512, where only the digit-run rule used to look.
        filler = "the relay moved to the Fenwick datacenter. " * 20
        self.assertGreater(len(filler), redaction.SCAN_LIMIT)
        for tail, expected in (
            ("call 415 555 0199", "phone"),
            ("write to ops@example.com", "email"),
            ("the box answers on 10.0.0.7 today", "ipv4"),
            ("ship it to 221B Baker Street", "street_address"),
            ("C:\\Users\\example-user\\notes.txt", "user_home"),
        ):
            with self.subTest(tail=tail):
                self.assertEqual(
                    redaction.private_identifier_kind(filler + tail), expected
                )

    def test_the_middle_of_a_very_long_value_only_changes_which_kind_is_named(self) -> None:
        # Past 1,024 characters the middle is inspected by the digit-run, the
        # compressed-hex run and the "@" rule and nothing else -- but the
        # value is over-long either way, so what the middle rules decide is
        # the *name* of the kind, never whether it screens.
        head = "the relay moved to the Fenwick datacenter. " * 20
        tail = "x" * 1_400
        for buried in ("call 415 555 0199", "mail ops@example.com",
                       "answers on 10.0.0.7", "nothing of interest here"):
            with self.subTest(buried=buried):
                self.assertEqual(
                    redaction.private_identifier_kind(head + buried + tail),
                    "long_value",
                )
                self.assertTrue(redaction.screen_endpoint(head + buried + tail)[0])

    def test_an_over_long_value_is_screened_whatever_it_holds(self) -> None:
        # Design 2.4 lists "over-long value" beside e-mail and phone as a kind
        # in its own right: past the scan cap the screen cannot see the whole
        # value, so it must not vouch for it.  The sealed holdout leaked a
        # 600-character value that carried no digit run at all.
        clean = "the relay moved to the Fenwick datacenter in Northgate. " * 40
        self.assertGreater(len(clean), redaction.SCAN_LIMIT)
        self.assertEqual(redaction.private_identifier_kind(clean), "long_value")
        self.assertEqual(redaction.private_identifier_kind("a" * 600), "long_value")
        # Exactly at the cap is still inspectable, so it is not over-long.
        self.assertIsNone(redaction.private_identifier_kind("a" * 512))

    def test_compressed_hex_run_is_screened_at_the_tail_and_in_the_middle(self) -> None:
        filler = "notes about the relay and the box. " * 20
        self.assertEqual(
            redaction.private_identifier_kind(filler + "fe80::1ff:fe23"), "ipv6"
        )
        self.assertEqual(
            redaction.private_identifier_kind(
                filler + "fe80::1ff:fe23" + "x" * 1_400
            ),
            "long_value",
        )


class ScreenEndpointTests(unittest.TestCase):
    def test_a_secret_is_reported_as_secret_not_as_a_private_kind(self) -> None:
        screened, reason = redaction.screen_endpoint("api_key = " + "a" * 32)
        self.assertTrue(screened)
        self.assertEqual(reason, "secret")
        self.assertIsNone(redaction.private_identifier_kind("Harrier box"))

    def test_secret_detection_is_checked_before_the_private_scan(self) -> None:
        # A value that is both: the secret reason wins, so a receipt never
        # calls a credential an "email".
        self.assertEqual(
            redaction.screen_endpoint("api_key = alice@example.com"), (True, "secret")
        )

    def test_ordinary_endpoints_pass(self) -> None:
        for text in ("Harrier box", "Fenwick", "listen port 9090", ""):
            with self.subTest(text=text):
                self.assertEqual(redaction.screen_endpoint(text), (False, None))

    def test_non_string_input_does_not_raise(self) -> None:
        for value in (None, 9090, 1.5, True):
            with self.subTest(value=value):
                self.assertEqual(redaction.screen_endpoint(value), (False, None))


class PinnedScreenIsUnchangedTests(unittest.TestCase):
    def test_the_narrow_screen_still_flags_only_email_and_user_home(self) -> None:
        for text, kind in POSITIVES:
            with self.subTest(text=text):
                self.assertEqual(
                    redaction.contains_private_identifier(text),
                    kind in {"email", "user_home"},
                )

    def test_the_narrow_screen_still_passes_every_negative(self) -> None:
        for text in NEGATIVES:
            with self.subTest(text=text):
                self.assertFalse(redaction.contains_private_identifier(text))

    def test_redaction_helpers_are_unaffected(self) -> None:
        self.assertEqual(
            redaction.redact_private_identifiers("mail alice@example.com now"),
            "mail [EMAIL] now",
        )
        self.assertTrue(redaction.contains_secret("api_key = " + "a" * 32))


class PurityTests(unittest.TestCase):
    def test_the_same_input_gives_the_same_answer_after_a_thousand_others(self) -> None:
        probe = "the box answers on 10.0.0.7 today"
        first = redaction.private_identifier_kind(probe)
        for index in range(1_000):
            redaction.private_identifier_kind(f"filler {index} Harrier box")
        self.assertEqual(redaction.private_identifier_kind(probe), first)

    def test_a_freshly_imported_module_agrees(self) -> None:
        fresh = importlib.reload(importlib.import_module("jarvis.redaction"))
        try:
            for text, expected in POSITIVES:
                with self.subTest(text=text):
                    self.assertEqual(fresh.private_identifier_kind(text), expected)
        finally:
            importlib.reload(fresh)

    def test_the_module_holds_no_mutable_screen_state(self) -> None:
        # No cache to poison: every screen constant is a pattern, a frozenset
        # or a tuple.
        for name in (
            "PRIVATE_IDENTIFIER_KINDS", "_WIDENED_RULES", "_RUN_CHARACTERS",
            "_PHONE_SEPARATORS", "_LONG_VALUE_SEPARATORS", "_HEX_RUN_CHARACTERS",
        ):
            with self.subTest(name=name):
                self.assertIsInstance(
                    getattr(redaction, name), (tuple, frozenset)
                )


ENFORCE_TIMING = os.environ.get("JARVIS_ENFORCE_TIMING_GATES") == "1"

SCREEN_ENDPOINT_CORPUS: tuple[str, ...] = (
    *ADVERSARIAL,
    ("1234-5678-" * 400)[:4_000],
    ("abcd:1234:" * 400)[:4_000],
    ("a=1 " * 1_000)[:4_000],
)


class WallClockBoundTests(unittest.TestCase):
    """The bound is 5 ms for any input up to 4,000 characters, and the reason
    it holds is asserted separately from the milliseconds.

    Two different claims, because only one of them is honest on every host.
    The absolute millisecond bound is enforced only under
    ``JARVIS_ENFORCE_TIMING_GATES=1``, the same rule the store-level 7.9 gate
    follows: CI runs this suite under ``coverage --branch``, which slows
    Python several-fold, and a wall-clock assertion measured through
    instrumentation tests the profiler rather than the screen -- it failed at
    17.7 ms against a 5 ms bound while the product was unchanged.

    What is asserted unconditionally is the property the bound exists to
    protect, and it needs no clock: **no pattern is ever handed more than the
    head and tail windows**, whatever the input length.  That is the ReDoS
    invariant the 108 ms review finding was about, it is exact rather than
    statistical, and it holds identically under any instrumentation.
    """

    BOUND_MS = 5.0

    # --- the clock-free invariant ------------------------------------------

    def _windows_seen(self, function: Any, text: str) -> list[tuple[int, ...]]:
        """The lengths of every window the scan phase was handed for one call.

        Both regex phases -- the secret detection and the private scan --
        iterate the windows ``_scan_windows`` returns, so accounting there
        covers each of them.
        """
        seen: list[tuple[int, ...]] = []
        original = redaction._scan_windows

        def recording(value: str) -> tuple[tuple[str, ...], str]:
            windows, middle = original(value)
            seen.append(tuple(len(window) for window in windows))
            return windows, middle

        redaction._scan_windows = recording  # type: ignore[assignment]
        try:
            function(text)
        finally:
            redaction._scan_windows = original  # type: ignore[assignment]
        return seen

    def _assert_scan_phase_bounded(self, function: Any, text: str) -> None:
        calls = self._windows_seen(function, text)
        self.assertTrue(calls, "the scan phase was never reached")
        for windows in calls:
            self.assertLessEqual(len(windows), 2)
            for length in windows:
                self.assertLessEqual(length, redaction.SCAN_LIMIT)

    def _measure(self, function: Any, text: str) -> float:
        best = float("inf")
        for _attempt in range(5):
            started = time.perf_counter()
            function(text)
            best = min(best, (time.perf_counter() - started) * 1000.0)
        return best

    def _check(self, function: Any, text: str, bound: float) -> float:
        self._assert_scan_phase_bounded(function, text)
        best = self._measure(function, text)
        if ENFORCE_TIMING:
            self.assertLessEqual(best, bound)
        return best

    # --- the corpora --------------------------------------------------------

    def test_adversarial_corpus_stays_inside_the_bound(self) -> None:
        for text in ADVERSARIAL:
            with self.subTest(length=len(text)):
                self.assertLessEqual(len(text), 4_000)
                self._check(redaction.private_identifier_kind, text, self.BOUND_MS)

    def test_screen_endpoint_is_bounded_too(self) -> None:
        # The correctness review: only the private half honoured the scan cap,
        # so SECRET_VALUE ran over the whole value -- 73-75 ms on this corpus,
        # which alone blew the 25 ms graph read budget.
        for text in SCREEN_ENDPOINT_CORPUS:
            with self.subTest(length=len(text)):
                self._check(redaction.screen_endpoint, text, self.BOUND_MS)

    def test_an_entity_label_is_far_inside_the_bound(self) -> None:
        self._check(redaction.private_identifier_kind, "H" * 80, self.BOUND_MS)

    def test_beyond_the_product_maximum_still_does_not_blow_up(self) -> None:
        # The review 108 ms ReDoS input; the scan cap is why it is now flat.
        self._check(redaction.private_identifier_kind, "1" * 5_000, self.BOUND_MS * 2)

    def test_the_scan_phase_does_not_grow_with_the_input(self) -> None:
        # The property a ratio of wall-clock times would only approximate: a
        # hundredfold longer input hands the patterns exactly the same number
        # of characters.  The middle is still walked, but by the two
        # regex-free run rules, not by a pattern.
        small = self._windows_seen(redaction.screen_endpoint, "1" * 5_000)
        large = self._windows_seen(redaction.screen_endpoint, "1" * 500_000)
        self.assertEqual(small, large)
        self.assertEqual(small, [(redaction.SCAN_LIMIT, redaction.SCAN_LIMIT)])

    def test_no_input_raises(self) -> None:
        for text in (*SCREEN_ENDPOINT_CORPUS, "1" * 500_000, "", "H" * 80):
            with self.subTest(length=len(text)):
                screened, reason = redaction.screen_endpoint(text)
                self.assertIsInstance(screened, bool)
                self.assertTrue(reason is None or isinstance(reason, str))

    def test_a_secret_at_either_end_of_a_long_value_is_still_caught(self) -> None:
        secret = "api_key = " + "a" * 32
        filler = "x" * 3_900
        self.assertEqual(redaction.screen_endpoint(secret + " " + filler)[1], "secret")
        self.assertEqual(redaction.screen_endpoint(filler + " " + secret)[1], "secret")

    def test_recorded_timing(self) -> None:
        """Recorded, not asserted: the one line a reader can compare against
        the 5 ms bound, whatever the host and whatever the instrumentation."""
        worst = 0.0
        where = ""
        for text in SCREEN_ENDPOINT_CORPUS:
            best = self._measure(redaction.screen_endpoint, text)
            if best > worst:
                worst, where = best, f"{len(text)} chars"
        print(
            f"\n[timing] screen_endpoint worst best-of-5: {worst:.3f} ms "
            f"({where}); bound {self.BOUND_MS} ms, enforced="
            f"{'yes' if ENFORCE_TIMING else 'no (set JARVIS_ENFORCE_TIMING_GATES=1)'}"
        )


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
