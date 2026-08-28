from __future__ import annotations

import json
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.agent import (
    Agent,
    _research_audit_targets,
    _research_page_records,
    _training_candidate_verified,
)
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.router import Route
from tests.test_agent import FakeResponse, FakeToolBox, ScriptedClient


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)

PYTHON_URL = "https://docs.python.org/3/library/venv.html"
PACKAGING_URL = (
    "https://packaging.python.org/en/latest/guides/"
    "installing-using-pip-and-virtual-environments/"
)
ACADEMIC_URL = "https://example.edu/research/dependency-environments"
BAD_CLAIM = "pip freeze captures only top-level packages."
SOURCE_EVIDENCE = (
    "pip freeze outputs installed packages in requirements format, including direct "
    "and transitive packages present in the environment."
)
OLLAMA_TOOL_URL = "https://docs.ollama.com/capabilities/tool-calling"
OLLAMA_FALSE_CLAIM = (
    "The documentation recommends limiting scopes, validating arguments, and logging "
    "every call for auditability."
)
OLLAMA_MECHANICS_EVIDENCE = (
    "Ollama supports tool calling (also known as function calling) which allows a model "
    "to invoke tools and incorporate their results into its replies."
)


def fetched_pages() -> list[dict[str, str]]:
    return [
        {
            "title": "Python virtual environments",
            "url": PYTHON_URL,
            "content": (
                SOURCE_EVIDENCE
                + " Virtual environments isolate project packages from the base interpreter."
            ),
        },
        {
            "title": "Packaging guide",
            "url": PACKAGING_URL,
            "content": (
                "The packaging guide recommends isolated environments and declared dependency "
                "metadata for repeatable project setup."
            ),
        },
        {
            "title": "Dependency environment study",
            "url": ACADEMIC_URL,
            "content": (
                "The study reports that lock information and controlled environments reduce "
                "unexplained dependency drift across repeated installations."
            ),
        },
    ]


def web_evidence(pages: list[dict[str, str]] | None = None) -> list[dict[str, object]]:
    return [{
        "tool": "web_search",
        "success": True,
        "response": {
            "ok": True,
            "result": {
                "results": [{"url": "https://snippet.invalid", "content": "unverified"}],
                "verified_pages": list(pages or fetched_pages()),
                "fetch_errors": [],
            },
        },
    }]


def deep_answer(*, bad_claim: bool = False) -> str:
    disputed = BAD_CLAIM if bad_claim else (
        "pip freeze describes packages installed in the selected environment rather than only "
        "the direct declarations."
    )
    return (
        "## Findings\n\n"
        "Python's environment documentation supports isolating each project so dependency changes "
        "do not silently alter the base interpreter or unrelated applications. "
        f"{PYTHON_URL} {disputed} The packaging guidance separately emphasizes declared project "
        "metadata and an isolated installation workflow, which makes setup easier to inspect and "
        f"repeat across machines. {PACKAGING_URL} An independent environment study reports that "
        "controlled environments and lock information reduce unexplained drift during repeated "
        f"installation attempts. {ACADEMIC_URL} Together, these sources support treating an "
        "environment snapshot as an observation of installed state, while treating declared "
        "requirements and lock data as the intended reproducibility contract. Teams should keep "
        "those roles separate during diagnosis, deployment, and maintenance.\n\n"
        "## Recommendation\n\n"
        "Create an isolated environment per project, install from reviewed manifest or lock data, "
        "and verify the resulting environment in continuous integration before release. Record the "
        "interpreter and platform alongside dependency information so later comparisons remain "
        "meaningful and actionable.\n\n"
        "## Limitations and uncertainty\n\n"
        "Exact resolver behavior, platform-specific wheels, optional extras, private indexes, and "
        "environment markers can still change results. The sources do not establish that every "
        "ecosystem has identical lock semantics, so projects should test their own supported "
        "platforms and document remaining uncertainty."
    )


def grounded_issue() -> dict[str, str]:
    return {
        "claim": BAD_CLAIM,
        "source_url": PYTHON_URL,
        "source_evidence": SOURCE_EVIDENCE,
        "problem": "contradicted",
        "correction": (
            "State that pip freeze reports packages installed in the environment, including "
            "transitive packages, rather than only top-level declarations."
        ),
    }


def supported_audited_claims() -> list[dict[str, str]]:
    return [
        {
            "claim": (
                "Python's environment documentation supports isolating each project so "
                "dependency changes do not silently alter the base interpreter or unrelated "
                "applications."
            ),
            "source_url": PYTHON_URL,
            "source_evidence": (
                "Virtual environments isolate project packages from the base interpreter."
            ),
            "verdict": "supported",
        },
        {
            "claim": (
                "pip freeze describes packages installed in the selected environment rather "
                "than only the direct declarations."
            ),
            "source_url": PYTHON_URL,
            "source_evidence": SOURCE_EVIDENCE,
            "verdict": "supported",
        },
        {
            "claim": (
                "The packaging guidance separately emphasizes declared project metadata and "
                "an isolated installation workflow, which makes setup easier to inspect and "
                "repeat across machines."
            ),
            "source_url": PACKAGING_URL,
            "source_evidence": (
                "The packaging guide recommends isolated environments and declared dependency "
                "metadata for repeatable project setup."
            ),
            "verdict": "supported",
        },
        {
            "claim": (
                "An independent environment study reports that controlled environments and "
                "lock information reduce unexplained drift during repeated installation attempts."
            ),
            "source_url": ACADEMIC_URL,
            "source_evidence": (
                "The study reports that lock information and controlled environments reduce "
                "unexplained dependency drift across repeated installations."
            ),
            "verdict": "supported",
        },
    ]


def review_payload(
    *,
    passed: bool,
    issues: list[dict[str, str]] | None = None,
    audited_claims: list[dict[str, str]] | None = None,
) -> str:
    issue_list = list(issues or [])
    if audited_claims is None:
        if issue_list:
            audited_claims = [
                {
                    "claim": issue["claim"],
                    "source_url": issue["source_url"],
                    "source_evidence": issue["source_evidence"],
                    "verdict": issue["problem"],
                    "correction": issue["correction"],
                }
                for issue in issue_list
            ]
        elif passed:
            audited_claims = supported_audited_claims()
        else:
            audited_claims = []
    return json.dumps({
        "passed": passed,
        "audited_claims": audited_claims,
        "issues": issue_list,
    })


class ResearchSemanticReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"research-review-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            model="auto",
            workspace=self.workspace,
            data_dir=self.data_dir,
            vault_dir=None,
            max_steps=8,
            context_length=16384,
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            fast_context_length=16384,
            reasoning_context_length=16384,
            coding_context_length=16384,
            ollama_preload=False,
            execution_mode="trusted-host",
            autonomy="autonomous",
            computer_access="disabled",
        )
        self.memory = Memory(self.data_dir / "agent.db")
        self.route = Route("reasoning", "gpt-oss:20b", "test reasoning route")

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def make_agent(self, responses: list[FakeResponse]) -> tuple[Agent, ScriptedClient]:
        client = ScriptedClient(responses)
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            record_training=False,
            coding_review=False,
            coding_planning=False,
        )
        return agent, client

    @staticmethod
    def page_map() -> dict[str, dict[str, str]]:
        return {page["url"]: page for page in fetched_pages()}

    def test_parser_accepts_only_an_exact_grounded_pip_freeze_contradiction(self) -> None:
        passed, issues, invalid, conclusive = Agent._parse_research_review(
            review_payload(passed=False, issues=[grounded_issue()]),
            deep_answer(bad_claim=True),
            self.page_map(),
        )

        self.assertFalse(passed)
        self.assertEqual(issues, [grounded_issue()])
        self.assertEqual(invalid, 0)
        self.assertTrue(conclusive)

    def test_parser_rejects_ungrounded_or_underspecified_issues(self) -> None:
        answer = deep_answer(bad_claim=True)
        base = grounded_issue()
        cases = {
            "paraphrased claim": {**base, "claim": "The answer says pip freeze lists direct dependencies."},
            "unknown source": {**base, "source_url": "https://unknown.example/source"},
            "wrong source quote": {**base, "source_evidence": (
                "The packaging guide recommends isolated environments and declared dependency metadata."
            )},
            "tiny claim": {**base, "claim": "pip freeze captures"},
            "tiny evidence": {**base, "source_evidence": "installed packages only"},
            "bad problem": {**base, "problem": "possibly-wrong"},
            "empty correction": {**base, "correction": ""},
        }
        for label, issue in cases.items():
            with self.subTest(label=label):
                passed, issues, invalid, conclusive = Agent._parse_research_review(
                    review_payload(passed=False, issues=[issue]),
                    answer,
                    self.page_map(),
                )
                self.assertFalse(passed)
                self.assertEqual(issues, [])
                self.assertEqual(invalid, 1)
                self.assertFalse(conclusive)

    def test_parser_tri_state_requires_a_clean_pass_and_keeps_only_grounded_issues(self) -> None:
        answer = deep_answer()
        bad_answer = deep_answer(bad_claim=True)
        pages = self.page_map()

        self.assertEqual(
            Agent._parse_research_review(review_payload(passed=True), answer, pages),
            (True, [], 0, True),
        )
        self.assertEqual(
            Agent._parse_research_review(review_payload(passed=False), answer, pages),
            (False, [], 0, False),
        )
        malformed = Agent._parse_research_review("not-json", answer, pages)
        self.assertEqual(malformed, (False, [], 1, False))

        ungrounded = {**grounded_issue(), "claim": "A sentence absent from the answer entirely."}
        passed, issues, invalid, conclusive = Agent._parse_research_review(
            review_payload(passed=True, issues=[ungrounded]), answer, pages
        )
        self.assertFalse(passed)
        self.assertEqual(issues, [])
        self.assertEqual(invalid, 1)
        self.assertFalse(conclusive)

        passed, issues, invalid, conclusive = Agent._parse_research_review(
            review_payload(passed=True, issues=[grounded_issue(), ungrounded]), bad_answer, pages
        )
        self.assertFalse(passed)
        self.assertEqual(issues, [grounded_issue()])
        self.assertEqual(invalid, 1)
        self.assertTrue(conclusive)

    def test_target_extractor_covers_parenthesized_inline_and_numbered_claims(self) -> None:
        inline_claim = (
            "Ollama tool calling lets a model invoke an\napplication-provided function"
        )
        numbered_claim = (
            "Ollama parallel tool calling can request several independent functions together"
        )
        answer = (
            f"{inline_claim}\n({OLLAMA_TOOL_URL}).\n"
            f"{numbered_claim} [1].\n\n"
            f"References\n[1] {OLLAMA_TOOL_URL}"
        )

        targets = _research_audit_targets(answer, {OLLAMA_TOOL_URL})
        normalized_answer = " ".join(answer.split())
        normalized_claims = {" ".join(claim.split()) for claim, _url in targets}

        self.assertEqual(len(targets), 2)
        self.assertEqual({url for _claim, url in targets}, {OLLAMA_TOOL_URL})
        self.assertTrue(any(" ".join(inline_claim.split()) in claim for claim in normalized_claims))
        self.assertTrue(any(numbered_claim in claim for claim in normalized_claims))
        self.assertTrue(all(claim in normalized_answer for claim in normalized_claims))

    def test_target_extractor_does_not_merge_markdown_heading_into_first_claim(self) -> None:
        targets = _research_audit_targets(
            deep_answer(),
            {PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
        )

        self.assertEqual(targets[0], (
            supported_audited_claims()[0]["claim"],
            PYTHON_URL,
        ))

    def test_bare_reviewer_pass_without_audited_claims_is_inconclusive(self) -> None:
        passed, issues, invalid, conclusive = Agent._parse_research_review(
            json.dumps({"passed": True, "issues": []}),
            deep_answer(),
            self.page_map(),
        )

        self.assertFalse(passed)
        self.assertEqual(issues, [])
        self.assertGreaterEqual(invalid, 1)
        self.assertFalse(conclusive)

    def test_supported_audit_cannot_launder_live_ollama_misattribution(self) -> None:
        answer = (
            "## Finding\n\n"
            f"{OLLAMA_FALSE_CLAIM} {OLLAMA_TOOL_URL}\n\n"
            "## Recommendation\n\nUse bounded application-side authorization.\n\n"
            "## Limitations and uncertainty\n\nThe source only documents invocation mechanics."
        )
        pages = {
            OLLAMA_TOOL_URL: {
                "title": "Tool calling - Ollama",
                "url": OLLAMA_TOOL_URL,
                "content": OLLAMA_MECHANICS_EVIDENCE,
            },
        }
        false_supported_audit = [{
            "claim": OLLAMA_FALSE_CLAIM,
            "source_url": OLLAMA_TOOL_URL,
            "source_evidence": OLLAMA_MECHANICS_EVIDENCE,
            "verdict": "supported",
        }]

        passed, issues, invalid, conclusive = Agent._parse_research_review(
            review_payload(passed=True, audited_claims=false_supported_audit),
            answer,
            pages,
        )

        self.assertFalse(passed)
        self.assertEqual(issues, [])
        self.assertGreaterEqual(invalid, 1)
        self.assertFalse(conclusive)

    def test_negative_audit_without_grounded_issue_and_correction_is_inconclusive(self) -> None:
        answer = f"{OLLAMA_FALSE_CLAIM} {OLLAMA_TOOL_URL}"
        pages = {
            OLLAMA_TOOL_URL: {
                "title": "Tool calling - Ollama",
                "url": OLLAMA_TOOL_URL,
                "content": OLLAMA_MECHANICS_EVIDENCE,
            },
        }
        negative_audit_without_issue = [{
            "claim": OLLAMA_FALSE_CLAIM,
            "source_url": OLLAMA_TOOL_URL,
            "source_evidence": OLLAMA_MECHANICS_EVIDENCE,
            "verdict": "unsupported",
        }]

        passed, issues, _invalid, conclusive = Agent._parse_research_review(
            review_payload(
                passed=False,
                audited_claims=negative_audit_without_issue,
            ),
            answer,
            pages,
        )

        self.assertFalse(passed)
        self.assertEqual(issues, [])
        self.assertFalse(conclusive)

    def test_clean_pass_requires_supported_coverage_for_every_traceable_url(self) -> None:
        passed, issues, _invalid, conclusive = Agent._parse_research_review(
            review_payload(
                passed=True,
                audited_claims=supported_audited_claims()[:2],
            ),
            deep_answer(),
            self.page_map(),
        )

        self.assertFalse(passed)
        self.assertEqual(issues, [])
        self.assertFalse(conclusive)

    def test_clean_pass_requires_one_grounded_audit_per_source_not_every_sentence(self) -> None:
        first_claim = (
            "Ollama supports tool calling so a model can invoke tools and incorporate "
            "tool results into its replies."
        )
        second_claim = (
            "Ollama supports parallel tool calls that can return several tool results "
            "in one follow-up message."
        )
        answer = (
            f"{first_claim} {OLLAMA_TOOL_URL}\n\n"
            f"{second_claim} {OLLAMA_TOOL_URL}"
        )
        pages = {
            OLLAMA_TOOL_URL: {
                "title": "Tool calling - Ollama",
                "url": OLLAMA_TOOL_URL,
                "content": f"{first_claim} {second_claim}",
            },
        }
        cherry_picked_audit = [{
            "claim": first_claim,
            "source_url": OLLAMA_TOOL_URL,
            "source_evidence": first_claim,
            "verdict": "supported",
        }]

        passed, issues, _invalid, conclusive = Agent._parse_research_review(
            review_payload(passed=True, audited_claims=cherry_picked_audit),
            answer,
            pages,
        )

        self.assertTrue(passed)
        self.assertEqual(issues, [])
        self.assertTrue(conclusive)

    def test_clean_source_coverage_ignores_malformed_extra_audit(self) -> None:
        audits = supported_audited_claims() + [{
            "claim": "A sentence absent from the answer entirely.",
            "source_url": PYTHON_URL,
            "source_evidence": "A source excerpt absent from the fetched page text.",
            "verdict": "supported",
        }]

        passed, issues, invalid, conclusive = Agent._parse_research_review(
            review_payload(passed=True, audited_claims=audits),
            deep_answer(),
            self.page_map(),
        )

        self.assertTrue(passed)
        self.assertEqual(issues, [])
        self.assertEqual(invalid, 1)
        self.assertTrue(conclusive)

    def test_grounded_supported_audit_covers_all_traceable_urls_and_passes(self) -> None:
        passed, issues, invalid, conclusive = Agent._parse_research_review(
            review_payload(
                passed=True,
                audited_claims=supported_audited_claims(),
            ),
            deep_answer(),
            self.page_map(),
        )

        self.assertTrue(passed)
        self.assertEqual(issues, [])
        self.assertEqual(invalid, 0)
        self.assertTrue(conclusive)

    def test_page_extractor_uses_only_successful_fetched_content_and_supported_shapes(self) -> None:
        records = _research_page_records([
            *web_evidence([fetched_pages()[0]]),
            {
                "tool": "web_fetch",
                "success": True,
                "response": {"ok": True, "result": fetched_pages()[1]},
            },
            {
                "tool": "research_question",
                "success": True,
                "response": {
                    "ok": True,
                    "result": {"evidence": [{
                        "url": ACADEMIC_URL,
                        "title": "Study",
                        "excerpt": fetched_pages()[2]["content"],
                    }]},
                },
            },
            {
                "tool": "web_search",
                "success": False,
                "response": {"ok": True, "result": {"verified_pages": [{
                    "url": "https://failed.example.edu/page",
                    "content": "This failed record must never become reviewer evidence.",
                }]}},
            },
        ])

        self.assertEqual(set(records), {PYTHON_URL, PACKAGING_URL, ACADEMIC_URL})
        self.assertEqual(records[ACADEMIC_URL]["content"], fetched_pages()[2]["content"])
        self.assertNotIn("https://failed.example.edu/page", records)

    def test_reviewer_prioritizes_answer_sources_within_eight_bounded_pages(self) -> None:
        pages = [
            {
                "title": f"Source {index}",
                "url": f"https://source{index}.example.edu/page",
                "content": f"Verified source number {index} contains enough exact factual source words.",
            }
            for index in range(10)
        ]
        priority_url = pages[9]["url"]
        unverified = {
            "title": "Unverified",
            "url": "https://unverified.example.edu/page",
            "content": "This page was fetched but is absent from the verified URL set.",
        }
        evidence = web_evidence([*pages, unverified])
        verified = {page["url"] for page in pages}
        priority_claim = pages[9]["content"]
        agent, client = self.make_agent([
            FakeResponse(content=review_payload(
                passed=True,
                audited_claims=[{
                    "claim": priority_claim,
                    "source_url": priority_url,
                    "source_evidence": priority_claim,
                    "verdict": "supported",
                }],
            )),
        ])

        result = agent._review_deep_research(
            "Deep research the dependency question.",
            f"{priority_claim} {priority_url}",
            evidence,
            verified,
        )

        self.assertEqual(result[:2], (True, []))
        request = client.requests[0]
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["model"], "gpt-oss:20b")
        self.assertEqual(request["temperature"], 0.0)
        self.assertEqual(request["think"], "low")
        self.assertEqual(request["seed"], 0)
        self.assertIsInstance(request["response_format"], dict)
        schema = request["response_format"]
        self.assertIn("audited_claims", schema["required"])
        audited_item = schema["properties"]["audited_claims"]["items"]
        self.assertEqual(
            set(audited_item["required"]),
            {"claim", "source_url", "source_evidence", "verdict"},
        )
        self.assertEqual(
            audited_item["properties"]["verdict"]["enum"],
            ["supported", "unsupported", "contradicted"],
        )
        user = request["messages"][1]["content"]
        encoded = user.split("<untrusted_fetched_pages>\n", 1)[1].split(
            "\n</untrusted_fetched_pages>", 1
        )[0]
        selected = json.loads(encoded)
        self.assertEqual(len(selected), 8)
        self.assertIn(priority_url, {page["url"] for page in selected})
        self.assertNotIn(unverified["url"], {page["url"] for page in selected})

    def test_helper_passes_after_one_clean_grounded_review(self) -> None:
        agent, client = self.make_agent([
            FakeResponse(content=review_payload(passed=True)),
        ])
        evidence = web_evidence()
        successful = {"web_search"}
        original = deep_answer()

        content, route, failure = agent._audit_and_revise_deep_research(
            prompt="Perform deep research on reproducible dependency environments.",
            content=original,
            evidence=evidence,
            route=self.route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools=successful,
            learning_task=False,
        )

        self.assertEqual(content, original)
        self.assertEqual(route, self.route)
        self.assertIsNone(failure)
        self.assertIn("__deep_research_review_passed__", successful)
        self.assertNotIn("__deep_research_review_failed__", successful)
        self.assertEqual(len(client.requests), 1)

    def test_dedicated_learning_model_also_runs_the_grounded_audit(self) -> None:
        self.config = replace(
            self.config,
            learning_model="openai:gpt-5.6-luna",
        )
        agent, client = self.make_agent([
            FakeResponse(content=review_payload(passed=True)),
        ])
        learning_route = Route(
            "custom",
            "openai:gpt-5.6-luna",
            "dedicated learning model",
        )

        _content, _route, failure = agent._audit_and_revise_deep_research(
            prompt="Continuously learn about dependency environments.",
            content=deep_answer(),
            evidence=web_evidence(),
            route=learning_route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools={"web_search"},
            learning_task=True,
        )

        self.assertIsNone(failure)
        self.assertEqual(client.requests[0]["model"], "openai:gpt-5.6-luna")

    def test_helper_performs_one_grounded_revision_then_requires_confirmation(self) -> None:
        revised = deep_answer(bad_claim=False)
        agent, client = self.make_agent([
            FakeResponse(content=review_payload(passed=False, issues=[grounded_issue()])),
            FakeResponse(content=revised),
            FakeResponse(content=review_payload(passed=True)),
        ])
        evidence = web_evidence()
        successful = {"web_search"}

        content, _route, failure = agent._audit_and_revise_deep_research(
            prompt="Perform deep research on reproducible dependency environments.",
            content=deep_answer(bad_claim=True),
            evidence=evidence,
            route=self.route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools=successful,
            learning_task=False,
        )

        self.assertIsNone(failure)
        self.assertEqual(content, revised)
        self.assertNotIn(BAD_CLAIM, content)
        self.assertIn("__deep_research_review_passed__", successful)
        self.assertEqual(len(client.requests), 3)
        self.assertTrue(all(request["tools"] == [] for request in client.requests))
        review_records = [
            item["tool"] for item in evidence
            if item.get("tool", "").startswith("grounded_research_review")
        ]
        self.assertEqual(review_records, [
            "grounded_research_review", "grounded_research_review_confirmation",
        ])

    def test_learning_repairs_a_new_conflict_in_a_second_bounded_revision(self) -> None:
        clean = deep_answer(bad_claim=False)
        agent, client = self.make_agent([
            FakeResponse(content=review_payload(passed=False, issues=[grounded_issue()])),
            # Simulate a first reviser that reintroduces the disputed wording.
            FakeResponse(content=deep_answer(bad_claim=True)),
            FakeResponse(content=review_payload(passed=False, issues=[grounded_issue()])),
            FakeResponse(content=clean),
            FakeResponse(content=review_payload(passed=True)),
        ])
        evidence = web_evidence()
        successful = {"web_search", "__research_topic_coverage_passed__"}
        events: list[str] = []
        agent.on_event = events.append

        content, _route, failure = agent._audit_and_revise_deep_research(
            prompt="Continuously learn about reproducible dependency environments.",
            content=deep_answer(bad_claim=True),
            evidence=evidence,
            route=self.route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools=successful,
            learning_task=True,
        )

        self.assertIsNone(failure)
        self.assertEqual(content, clean)
        self.assertNotIn(BAD_CLAIM, content)
        self.assertEqual(len(client.requests), 5)
        self.assertIn(
            "grounded research conflicts remain - bounded revision 2/3",
            events,
        )
        self.assertIn("grounded research revision 2 passed", events)
        self.assertIn("__deep_research_review_passed__", successful)

    def test_learning_still_fails_closed_after_three_bounded_revisions(self) -> None:
        failure_review = FakeResponse(
            content=review_payload(passed=False, issues=[grounded_issue()])
        )
        agent, client = self.make_agent([
            failure_review,
            FakeResponse(content=deep_answer(bad_claim=True)),
            failure_review,
            FakeResponse(content=deep_answer(bad_claim=True)),
            failure_review,
            FakeResponse(content=deep_answer(bad_claim=True)),
            failure_review,
        ])
        successful = {"web_search", "__research_topic_coverage_passed__"}

        _content, _route, failure = agent._audit_and_revise_deep_research(
            prompt="Continuously learn about reproducible dependency environments.",
            content=deep_answer(bad_claim=True),
            evidence=web_evidence(),
            route=self.route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools=successful,
            learning_task=True,
        )

        self.assertIn("after 3 bounded revisions", failure)
        self.assertEqual(len(client.requests), 7)
        self.assertIn("__deep_research_review_failed__", successful)
        self.assertNotIn("__deep_research_review_passed__", successful)

    def test_audit_targets_skip_evidence_anchor_and_bind_preceding_finding(self) -> None:
        answer = (
            "- Use supported router encryption and replace obsolete hardware.\n"
            "  Evidence anchor: ‘Use WPA3 Personal or WPA2 Personal.’\n"
            f"  {PYTHON_URL}"
        )

        targets = _research_audit_targets(answer, {PYTHON_URL})

        self.assertEqual(targets, [(
            "Use supported router encryption and replace obsolete hardware.",
            PYTHON_URL,
        )])

    def test_every_continuous_learning_prompt_requires_deep_semantic_review(self) -> None:
        prompt = (
            "Continuously learn about this topic: current official home Wi-Fi "
            "and router security guidance for families."
        )

        self.assertTrue(Agent._is_learning_task(prompt))
        self.assertTrue(Agent._is_deep_research_task(prompt))

    def test_learning_candidate_cannot_persist_without_coverage_and_semantic_pass(self) -> None:
        urls = {PYTHON_URL, PACKAGING_URL, ACADEMIC_URL}
        answer = deep_answer()

        self.assertFalse(_training_candidate_verified(
            content=answer,
            requires_web=True,
            requires_coding=False,
            successful_tools={"web_search"},
            verified_urls=urls,
            learning_task=True,
        ))
        self.assertTrue(_training_candidate_verified(
            content=answer,
            requires_web=True,
            requires_coding=False,
            successful_tools={
                "web_search",
                "__research_topic_coverage_passed__",
                "__deep_research_review_passed__",
            },
            verified_urls=urls,
            learning_task=True,
        ))

    def test_helper_discloses_inconclusive_review_without_vetoing_deterministic_result(self) -> None:
        agent, client = self.make_agent([FakeResponse(content="not-json")])
        evidence = web_evidence()
        successful = {"web_search"}

        content, _route, failure = agent._audit_and_revise_deep_research(
            prompt="Perform deep research on dependencies.",
            content=deep_answer(),
            evidence=evidence,
            route=self.route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools=successful,
            learning_task=False,
        )

        self.assertIn(deep_answer(), content)
        self.assertIn("Audit note:", content)
        self.assertIn("independently verify material claims", content)
        self.assertIsNone(failure)
        self.assertIn("__deep_research_review_inconclusive__", successful)
        self.assertNotIn("__deep_research_review_failed__", successful)
        self.assertFalse(_training_candidate_verified(
            content=content,
            requires_web=True,
            requires_coding=False,
            successful_tools=successful,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
        ))
        self.assertEqual(len(client.requests), 1)

    def test_helper_stops_before_confirmation_when_revision_fails_deterministic_gate(self) -> None:
        agent, client = self.make_agent([
            FakeResponse(content=review_payload(passed=False, issues=[grounded_issue()])),
            FakeResponse(content=(
                "Too short to be a deep research answer. "
                f"{PYTHON_URL} {PACKAGING_URL} {ACADEMIC_URL}"
            )),
        ])
        evidence = web_evidence()
        successful = {"web_search"}

        content, _route, failure = agent._audit_and_revise_deep_research(
            prompt="Perform deep research on dependencies.",
            content=deep_answer(bad_claim=True),
            evidence=evidence,
            route=self.route,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            successful_tools=successful,
            learning_task=False,
        )

        self.assertIn("Too short", content)
        self.assertIn("at least 80 prose words", failure)
        self.assertIn("__deep_research_review_failed__", successful)
        self.assertEqual(len(client.requests), 2)

    def test_finalize_synthesis_path_cannot_bypass_grounded_review(self) -> None:
        agent, client = self.make_agent([
            FakeResponse(content=deep_answer()),
            FakeResponse(content=review_payload(passed=True)),
        ])
        conversation = self.memory.new_conversation("deep synthesis")
        evidence = web_evidence()
        successful = {"web_search"}

        result = agent._finalize_with_synthesis(
            conversation_id=conversation,
            prompt="Perform comprehensive deep research on dependency environments.",
            evidence=evidence,
            route=self.route,
            task_context="",
            tool_calls=1,
            requires_web=True,
            requires_coding=False,
            learning_task=False,
            successful_tools=successful,
            verified_urls={PYTHON_URL, PACKAGING_URL, ACADEMIC_URL},
            reason="maximum model steps reached",
            deep_research_task=True,
        )

        self.assertEqual(result.status, "complete")
        self.assertIn("__deep_research_review_passed__", successful)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[-1]["tools"], [])

    def test_runtime_owned_deep_research_completion_cannot_bypass_grounded_review(self) -> None:
        pages = fetched_pages()
        toolbox = FakeToolBox(verified_pages=pages)
        client = ScriptedClient([
            FakeResponse(content=deep_answer()),
            FakeResponse(content=review_payload(passed=True)),
        ])
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            record_training=False,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run(
            "Perform comprehensive deep research on reproducible dependency environments using primary sources."
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual(
            [name for name, _arguments in toolbox.calls],
            ["web_search", "web_search", "web_search"],
        )
        self.assertEqual(len(client.requests), 2)
        self.assertTrue(all(request["tools"] == [] for request in client.requests))
        self.assertEqual(client.requests[-1]["tools"], [])
        self.assertIn("independent evidence auditor", client.requests[-1]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
