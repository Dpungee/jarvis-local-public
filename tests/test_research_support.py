from __future__ import annotations

import unittest

from jarvis import agent
from jarvis import research_support
from jarvis.research_support import (
    canonical_topic_term,
    compact_research_query,
    normalize_dated_brief_heading,
    research_distinctive_terms,
    research_prose_stats,
    research_relevant_urls,
    research_reports_no_finding,
    research_subject_query,
    research_terms_matching,
    research_topic_coverage,
    research_topic_terms,
    stable_dialogue_prompt_parts,
)


class ResearchSupportExtractionTests(unittest.TestCase):
    def test_agent_compatibility_exports_are_preserved(self):
        helpers = {
            "_canonical_topic_term": canonical_topic_term,
            "_compact_research_query": compact_research_query,
            "_normalize_dated_brief_heading": normalize_dated_brief_heading,
            "_research_distinctive_terms": research_distinctive_terms,
            "_research_prose_stats": research_prose_stats,
            "_research_relevant_urls": research_relevant_urls,
            "_research_reports_no_finding": research_reports_no_finding,
            "_research_subject_query": research_subject_query,
            "_research_terms_matching": research_terms_matching,
            "_research_topic_coverage": research_topic_coverage,
            "_research_topic_terms": research_topic_terms,
            "_stable_dialogue_prompt_parts": stable_dialogue_prompt_parts,
        }
        for name, helper in helpers.items():
            with self.subTest(name=name):
                self.assertIs(getattr(agent, name), helper)

    def test_agent_reuses_the_canonical_research_and_dialogue_constants(self):
        names = (
            "_DIALOGUE_DYNAMIC_TAGS",
            "_DIALOGUE_MEMORY_HEADING",
            "_MEMORY_STOPWORDS",
            "_RESEARCH_ARTIFACT_DELIVERY",
            "_RESEARCH_BRAND_TERMS",
            "_RESEARCH_BUILD_DELIVERY",
            "_RESEARCH_FUNCTION_STOPWORDS",
            "_RESEARCH_NO_FINDING_PREFIXES",
            "_RESEARCH_QUERY_ACTION",
            "_RESEARCH_TOPIC_STOPWORDS",
            "_URL_IN_TEXT",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(agent, name),
                    getattr(research_support, name),
                )

    def test_research_subject_discards_artifact_delivery_but_keeps_constraints(self):
        prompt = (
            "Research current local LLM quantization tradeoffs and put the findings "
            "into a PDF report"
        )
        self.assertEqual(
            research_subject_query(prompt),
            "current local LLM quantization tradeoffs",
        )

    def test_relevance_requires_request_evidence(self):
        relevant_url = "https://example.com/quantization"
        pages = {
            relevant_url: {
                "url": relevant_url,
                "title": "Local LLM quantization tradeoffs",
                "content": "Measured inference latency and memory tradeoffs for quantized models.",
            },
            "https://example.com/weather": {
                "url": "https://example.com/weather",
                "title": "Weather",
                "content": "Rain and wind forecast.",
            },
        }
        prompt = "research local LLM quantization tradeoffs"
        self.assertEqual(research_relevant_urls(prompt, pages), {relevant_url})
        relevant_pages, covered, total = research_topic_coverage(prompt, pages)
        self.assertEqual(relevant_pages, 1)
        self.assertGreaterEqual(covered, 2)
        self.assertGreaterEqual(total, covered)

    def test_all_lexical_helpers_keep_their_bounded_behavior(self):
        self.assertEqual(canonical_topic_term("security"), "secure")
        self.assertEqual(
            compact_research_query(
                "Please research security and securing agent agents with sources"
            ),
            "security agent",
        )
        self.assertEqual(
            research_distinctive_terms({"ai", "quantization", "latency", "memory"}),
            {"quantization", "latency", "memory"},
        )
        self.assertEqual(
            research_terms_matching(
                {"quantization", "latency"},
                {"quantizaton", "latency"},
            ),
            {"quantization", "latency"},
        )
        self.assertEqual(
            research_topic_terms("Topic: security agents and testing. Return sources."),
            {"secure", "agent", "test"},
        )

        words, meaningful = research_prose_stats(
            "Useful measured latency evidence.\nSources:\nhttps://example.com/a"
        )
        self.assertEqual(words, 4)
        self.assertEqual(meaningful, 4)
        self.assertTrue(
            research_reports_no_finding(
                "Research is incomplete because no authoritative page was fetched."
            )
        )
        self.assertFalse(research_reports_no_finding("Here are the verified findings."))
        self.assertEqual(
            normalize_dated_brief_heading(
                "## Dated brief — January 1, 2000\nVerified findings.",
                "August 29, 2026",
            ),
            "## Dated brief — August 29, 2026\nVerified findings.",
        )

    def test_dynamic_memory_is_removed_from_the_reusable_dialogue_prefix(self):
        memory_sentinel = "PRIVATE_MEMORY_SENTINEL"
        claim_sentinel = "PRIVATE_CLAIM_SENTINEL"
        system_content = (
            "<trusted_constitution>stable</trusted_constitution>\n\n"
            f"{research_support._DIALOGUE_MEMORY_HEADING}\n"
            "<untrusted_memory_records>"
            f"{memory_sentinel}"
            "</untrusted_memory_records>\n"
            f"<temporal_claims>{claim_sentinel}</temporal_claims>\n"
        )

        stable, current_turn = stable_dialogue_prompt_parts(system_content)

        self.assertIn("<trusted_constitution>stable</trusted_constitution>", stable)
        self.assertIn("attached to the current user turn", stable)
        self.assertNotIn(research_support._DIALOGUE_MEMORY_HEADING, stable)
        self.assertNotIn(memory_sentinel, stable)
        self.assertNotIn(claim_sentinel, stable)
        self.assertIn(
            f"<untrusted_memory_records>{memory_sentinel}</untrusted_memory_records>",
            current_turn,
        )
        self.assertIn(
            f"<temporal_claims>{claim_sentinel}</temporal_claims>",
            current_turn,
        )

    def test_empty_dynamic_memory_is_not_attached_to_the_current_turn(self):
        system_content = (
            "stable contract\n\n"
            f"{research_support._DIALOGUE_MEMORY_HEADING}\n"
            "<untrusted_memory_records>[]</untrusted_memory_records>\n"
            "<temporal_claims>{}</temporal_claims>\n"
        )

        stable, current_turn = stable_dialogue_prompt_parts(system_content)

        self.assertIn("stable contract", stable)
        self.assertEqual(current_turn, "")


if __name__ == "__main__":
    unittest.main()
