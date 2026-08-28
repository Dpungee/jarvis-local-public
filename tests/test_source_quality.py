import unittest

from jarvis.source_quality import (
    is_authoritative_source,
    prefer_authoritative_sources,
)


class SourceQualityTests(unittest.TestCase):
    def test_recognizes_curated_primary_sources(self):
        for url in (
            "https://docs.ollama.com/context-length",
            "https://qwenlm.github.io/blog/qwen3/",
            "https://github.com/QwenLM/Qwen3",
            "https://docs.python.org/3/library/asyncio.html",
            "https://genai.owasp.org/llm-top-10/",
            "https://www.nist.gov/publication/example",
            "https://arxiv.org/abs/2401.00001",
            "https://learn.microsoft.com/windows/ai/",
            "https://aws.amazon.com/what-is/retrieval-augmented-generation/",
            "https://openai.com/research/",
            "https://docs.anthropic.com/en/docs/",
            "https://ai.google.dev/gemini-api/docs",
            "https://docs.github.com/en/actions",
            "https://attack.mitre.org/matrices/enterprise/",
            "https://www.cyber.gov.au/protect-yourself/staying-secure-online/secure-your-wifi-and-router",
            "https://nodejs.org/en/download",
            "https://git-scm.com/downloads",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_authoritative_source(url))

    def test_rejects_blogs_lookalikes_and_url_confusion(self):
        for url in (
            "https://independent-ai-notes.example/posts/runtime-update/",
            "https://model-benchmarks.example/posts/local-inference/",
            "https://unverified-ml-blog.example/agent-frameworks",
            "https://docs.ollama.com.evil.example/context",
            "https://evil.example/?next=https://docs.ollama.com",
            "https://ollama.com@evil.example/context",
            "file:///docs.ollama.com/context",
            "https://github.com/chaosync-org/awesome-ai-agent-testing",
            "https://huggingface.co/random-user/random-model",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_authoritative_source(url))

    def test_primary_sources_displace_lower_quality_alternatives_only_when_present(self):
        self.assertEqual(
            prefer_authoritative_sources({
                "https://example.com/python-release-rumor",
                "https://www.python.org/downloads/",
            }),
            ["https://www.python.org/downloads/"],
        )
        self.assertEqual(
            prefer_authoritative_sources({"https://exampleartist.com/events"}),
            ["https://exampleartist.com/events"],
        )


if __name__ == "__main__":
    unittest.main()
