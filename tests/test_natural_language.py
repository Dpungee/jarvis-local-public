from __future__ import annotations

import unittest

from jarvis.natural_language import (
    has_current_public_information_shape,
    intent_classification_text,
    intent_routing_text,
    operator_action_text,
    public_web_evidence_boundary_allows,
)
from jarvis.agent import _requires_web


class NaturalLanguageIntentTests(unittest.TestCase):
    def test_normalizes_general_shorthand_and_single_edit_typos(self) -> None:
        cases = {
            "wht u think abt dogs": "what you think about dogs",
            "can u chek if Python 3.15 is out rn": (
                "can you check if Python 3.15 is out right now"
            ),
            "is Eminem touring next yr": "is Eminem touring next year",
            "latst weather tody pls": "latest weather today please",
            "idk tho": "i do not know though",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(intent_classification_text(source), expected)

    def test_does_not_change_names_unknown_words_or_authority_words(self) -> None:
        source = "Run Zauth for ExampleUser, deploy Quuxly, then buy DOGE."
        self.assertEqual(intent_classification_text(source), source)

    def test_does_not_turn_short_ordinary_words_into_web_intent(self) -> None:
        source = "Make a new directory called archives."
        self.assertEqual(intent_classification_text(source), source)
        self.assertFalse(has_current_public_information_shape(source))

    def test_preserves_urls_and_local_paths_exactly(self) -> None:
        source = (
            "chek https://example.com/wht/u?yr=rn and "
            r"C:\Users\example\wht-yr.txt rn"
        )
        normalized = intent_classification_text(source)
        self.assertIn("https://example.com/wht/u?yr=rn", normalized)
        self.assertIn(r"C:\Users\example\wht-yr.txt", normalized)
        self.assertTrue(normalized.startswith("check "))
        self.assertTrue(normalized.endswith(" right now"))

    def test_protected_spans_are_identified_before_unicode_normalization(self) -> None:
        source = (
            "chek "
            "https://example.com/\u212a?q=\u2460 "
            "C:\\Temp\\\ufb01le.txt "
            "\\\\server\\share\\\u212a.txt "
            '"C:\\Program Files\\\ufb01le.txt" '
            "./research/\ufb01le.txt rn"
        )
        normalized = intent_classification_text(source)
        for exact in (
            "https://example.com/\u212a?q=\u2460",
            "C:\\Temp\\\ufb01le.txt",
            "\\\\server\\share\\\u212a.txt",
            '"C:\\Program Files\\\ufb01le.txt"',
            "./research/\ufb01le.txt",
        ):
            with self.subTest(exact=exact):
                self.assertIn(exact, normalized)
        self.assertTrue(normalized.startswith("check "))
        self.assertTrue(normalized.endswith(" right now"))

    def test_routing_view_masks_private_paths_but_preserves_public_urls(self) -> None:
        source = (
            r"Read C:\latest\research.txt and ./news/report.txt, then "
            "check https://example.com/latest."
        )
        routed = intent_routing_text(source)
        self.assertNotIn(r"C:\latest\research.txt", routed)
        self.assertNotIn("./news/report.txt", routed)
        self.assertEqual(routed.count("[local-path]"), 2)
        self.assertIn("https://example.com/latest.", routed)

    def test_unquoted_spaced_local_paths_never_leak_routing_suffixes(self) -> None:
        paths = (
            r"C:\Program Files\latest\release today.log",
            r"\\server\share\My Files\current weather today.txt",
            r".\My Files\latest weather today.txt",
            r"..\My Files\latest weather today.txt",
            "./My Files/latest weather today.txt",
            "../My Files/latest weather today.txt",
            "~/My Files/latest weather today.txt",
            "/home/example/My Files/latest weather today.txt",
            "/Users/example/My Files/latest weather today.txt",
        )
        for path in paths:
            source = f"check then read {path}"
            with self.subTest(path=path):
                self.assertEqual(
                    intent_routing_text(source),
                    "check then read [local-path]",
                )
                self.assertIn(path, intent_classification_text(source))
                self.assertFalse(has_current_public_information_shape(source))
                self.assertFalse(_requires_web(source))

        ambiguous = r"check C:\Program Files\latest release today"
        self.assertEqual(intent_routing_text(ambiguous), "check [local-path]")
        self.assertFalse(_requires_web(ambiguous))

    def test_absolute_posix_paths_are_local_targets_not_public_queries(self) -> None:
        for path in (
            "/secret.txt",
            "/tmp",
            "/latest-weather-today.txt",
            "/\ufb01le.txt",
            "/tmp/latest-weather-today.txt",
            "/var/tmp/current/news.txt",
            "/opt/example/release-notes.txt",
        ):
            source = f"check then read {path}"
            with self.subTest(path=path):
                self.assertEqual(
                    intent_routing_text(source),
                    "check then read [local-path]",
                )
                self.assertIn(path, intent_classification_text(source))
                self.assertFalse(has_current_public_information_shape(source))
                self.assertFalse(_requires_web(source))

    def test_unc_share_roots_are_local_targets_not_public_queries(self) -> None:
        for path in (
            r"\\server\latest",
            r"\\server\current-news",
            r"\\server\share\latest-weather-today.txt",
        ):
            source = f"check then read {path}"
            with self.subTest(path=path):
                self.assertEqual(
                    intent_routing_text(source),
                    "check then read [local-path]",
                )
                self.assertIn(path, intent_classification_text(source))
                self.assertFalse(has_current_public_information_shape(source))
                self.assertFalse(_requires_web(source))

    def test_ambiguous_suffix_after_local_path_is_masked_fail_closed(self) -> None:
        for source in (
            "read ./notes.txt latest weather today",
            "read ../notes.txt news today",
            "check ~/notes.txt current release",
            "read /secret.txt latest weather today",
            r"read \\server\share current news today",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    intent_routing_text(source).split("[local-path]", 1)[1],
                    "",
                )
                self.assertFalse(has_current_public_information_shape(source))
                self.assertFalse(_requires_web(source))

    def test_bare_filenames_in_file_operations_are_masked_fail_closed(self) -> None:
        for source in (
            "read latest-weather-today.txt",
            "open current-news.json latest weather today",
            "inspect release-notes.md current news",
        ):
            with self.subTest(source=source):
                routed = intent_routing_text(source)
                self.assertIn("[local-path]", routed)
                self.assertNotIn("latest weather today", routed)
                self.assertNotIn("current news", routed)
                self.assertFalse(has_current_public_information_shape(source))
                self.assertFalse(_requires_web(source))

        source = "read notes.txt then research latest weather today"
        self.assertEqual(
            intent_routing_text(source),
            "read [local-path] then research latest weather today",
        )
        self.assertTrue(_requires_web(source))

        # A host-like value under an explicit web lookup verb remains public;
        # bare local masking must not become a generic domain blocker.
        public = "check example.com for the latest release"
        self.assertNotIn("[local-path]", intent_routing_text(public))
        self.assertTrue(_requires_web(public))

    def test_local_path_mask_preserves_an_explicit_following_public_action(self) -> None:
        for path in (
            r".\My Files\notes.txt",
            "~/My Files/notes.txt",
            "/home/example/My Files/notes.txt",
        ):
            source = f"read {path} then research latest weather today"
            with self.subTest(path=path):
                self.assertEqual(
                    intent_routing_text(source),
                    "read [local-path] then research latest weather today",
                )
                self.assertTrue(_requires_web(source))

    def test_spaced_path_mask_stops_at_extension_before_explicit_next_action(self) -> None:
        source = (
            r"read C:\Program Files\notes.txt then research latest weather today"
        )
        routed = intent_routing_text(source)
        self.assertEqual(
            routed,
            "read [local-path] then research latest weather today",
        )
        self.assertTrue(_requires_web(source))

    def test_private_path_words_do_not_authorize_public_web_routing(self) -> None:
        for source in (
            r"Read C:\research\notes.txt",
            r"Open C:\latest\report.txt",
            "Read ./research/notes.txt",
            r"Inspect \\server\news\today.txt",
            r"What is the latest version recorded in C:\Private Roadmap.txt?",
            r'What is the latest version recorded in "C:\Private Roadmap.txt"?',
        ):
            with self.subTest(source=source):
                self.assertFalse(_requires_web(source))

    def test_quoted_and_code_data_cannot_authorize_web_routing(self) -> None:
        private_samples = (
            'Summarize this private excerpt: "What is the latest news today?"',
            'Summarize this private excerpt: "What is the latest news today?',
            "Summarize this private excerpt: 'What is the latest news today?",
            "Summarize this text: `latest weather today`",
            "Summarize this text:\n```\nlatest news today\n```",
            "Summarize this text:\n> latest news today",
            "Summarize this text:\n> private excerpt\nlatest news today",
            "Summarize this text:\n    latest news today",
            "Summarize this text:\n\tlatest news today",
            "Summarize this private excerpt: 'What is the latest news today?'",
            'Summarize this private excerpt: "What is the latest\nnews today?"',
            "Summarize this private excerpt: <code>latest news today</code>",
            "Summarize this private excerpt: <blockquote>latest news today</blockquote>",
            "Summarize this private excerpt: <pre>latest news today</pre>",
            "Summarize this private excerpt: <samp>latest news today</samp>",
            "Summarize this private excerpt: <script>latest news today</script>",
            "Summarize this private excerpt: <textarea>latest news today</textarea>",
            "Summarize [latest news today](private-note).",
            "Summarize this private excerpt: &quot;latest news today&quot;",
        )
        for source in private_samples:
            with self.subTest(source=source):
                self.assertIn("[inert-text]", intent_routing_text(source))
                self.assertFalse(_requires_web(source))
                self.assertFalse(public_web_evidence_boundary_allows(source))

        # Quoted public subjects remain usable when the operator's own words
        # independently request current public information.
        self.assertTrue(_requires_web('What is the latest news about "Eminem" today?'))
        self.assertTrue(
            _requires_web(
                "Summarize [the current release notes](https://example.com/releases)."
            )
        )
        self.assertTrue(
            public_web_evidence_boundary_allows(
                'What is the latest news about "Eminem" today?'
            )
        )

    def test_operator_action_view_masks_inert_and_negated_actions(self) -> None:
        for source in (
            'Explain why "delete all files" is dangerous.',
            "Explain why `delete all files` is dangerous.",
            "Explain this example:\n> move the files into archives",
            'Translate "move the files into archives".',
            "Do not delete any files.",
            "Never create a folder here.",
        ):
            with self.subTest(source=source):
                action_view = operator_action_text(source)
                self.assertFalse(
                    any(
                        verb in action_view.casefold().split()
                        for verb in ("delete", "move", "create")
                    ),
                    action_view,
                )

        mixed = operator_action_text(
            "Do not delete the originals, but create a backup folder."
        )
        self.assertNotIn("delete", mixed.casefold())
        self.assertIn("create a backup folder", mixed.casefold())

    def test_operator_action_view_requires_affirmative_directive_grammar(self) -> None:
        non_directives = (
            "You should not delete any files.",
            "I cannot generate a logo image.",
            "Do **not** create a PDF report.",
            "Do not:\n edit this image.",
            "Should I improve this image?",
            "Explain how to run the application.",
            "Jarvis, explain how to delete these files.",
            "Would it be safe to run the application?",
            "The prompt says delete all files.",
            "What do you remember about my preference?",
            "Where do you store that preference?",
            "Delete files? No, only explain.",
        )
        for source in non_directives:
            with self.subTest(source=source):
                view = operator_action_text(source)
                self.assertFalse(
                    any(
                        verb in view.casefold().split()
                        for verb in (
                            "create",
                            "delete",
                            "edit",
                            "generate",
                            "improve",
                            "remember",
                            "run",
                            "store",
                        )
                    ),
                    view,
                )

        directives = (
            "Without delay, delete the files now.",
            "Never mind, delete the files now.",
            "Don't just explain, delete the files now.",
            "Do not wait, create a PDF report now.",
            "Can you please generate a logo?",
            "I want you to edit module.py and run tests.",
            "Let's remember this preference for later.",
        )
        for source in directives:
            with self.subTest(source=source):
                self.assertRegex(
                    operator_action_text(source),
                    r"\b(?:create|delete|edit|generate|remember|run)\b",
                )

    def test_implicit_local_files_cannot_authorize_public_routing(self) -> None:
        for source in (
            "What is the latest version recorded in roadmap.md?",
            "What is the latest version recorded in private/roadmap.md?",
            "What is the latest version recorded in Private Roadmap.txt?",
        ):
            with self.subTest(source=source):
                self.assertIn("[local-path]", intent_routing_text(source))
                self.assertFalse(_requires_web(source))
                self.assertFalse(public_web_evidence_boundary_allows(source))

        creation = (
            "Research current OSHA heat guidance and save a cited one-page brief "
            "as heat-guide.pdf."
        )
        self.assertTrue(public_web_evidence_boundary_allows(creation))
        self.assertTrue(_requires_web(creation))

    def test_local_target_allows_only_a_separate_public_research_clause(self) -> None:
        for source in (
            r"Read C:\notes.txt then research latest weather today",
            r"Read C:\notes.txt. Research latest weather today",
            r"Research latest weather today. Then read C:\notes.txt",
        ):
            with self.subTest(source=source):
                self.assertTrue(_requires_web(source))

    def test_ambiguous_or_distant_spelling_is_not_guessed(self) -> None:
        for source in ("wat", "currntlyy", "reearchh", "tourrzz"):
            with self.subTest(source=source):
                self.assertEqual(intent_classification_text(source), source)

    def test_normalization_is_bounded(self) -> None:
        self.assertEqual(intent_classification_text("wht u think", limit=3), "what")

    def test_current_information_shape_is_generic_and_conservative(self) -> None:
        for source in (
            "can u chek if Python 3.15 is out rn?",
            "is the new release available yet",
            "when is Eminem touring next yr?",
            "what is the latest weather forecast?",
            "yo whats the weather lookin like in 10001 today",
            "okay, so what is the latest news today",
        ):
            with self.subTest(source=source):
                self.assertTrue(has_current_public_information_shape(source))

        for source in (
            "how are you rn?",
            "is Photoshop open right now?",
            "deploy the latest release",
            "tell me what you think about dogs",
            "version the local file",
        ):
            with self.subTest(source=source):
                self.assertFalse(has_current_public_information_shape(source))


if __name__ == "__main__":
    unittest.main()
