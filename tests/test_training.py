from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from jarvis.memory import Memory, training_prompt_split
from jarvis.finetune import _load_records, main as finetune_main
from jarvis.training import (
    READINESS_MIN_CAPABILITY_EXAMPLES,
    READINESS_MIN_ENABLED_EVALUATIONS,
    READINESS_MIN_QUALITY,
    READINESS_MIN_TRAIN_EXAMPLES,
    READINESS_MIN_TEST_EXAMPLES,
    READINESS_MIN_VALIDATION_EXAMPLES,
    READINESS_MIN_VERIFIED_EXAMPLES,
    TRAINING_QUALITY_CONTRACT_VERSION,
    dataset_status,
    export_verified_dataset,
    parse_expected_terms,
)


LEARNING_RESPONSE = (
    "Current official guidance confirms that bounded tools, validated arguments, explicit "
    "authorization, isolated memory, audit logging, and adversarial evaluation improve agent "
    "reliability. Comparing the two sources shows that runtime controls and application security "
    "must work together. The practical recommendation is to test every privileged action and "
    "retain traceable evidence. Limitations remain because deployments and threat models differ. "
    "https://docs.ollama.com/context-length https://owasp.org/"
)


def _quality_evidence(task_kind: str) -> dict:
    verification = {
        "accepted_complete": True,
        "inspected_before_write": False,
        "content_write_completed": False,
        "inspected_after_write": False,
        "verified_after_write": False,
        "adversarial_probe_passed": False,
        "deep_research_review_passed": task_kind == "learning",
        "research_topic_coverage_passed": task_kind == "learning",
    }
    tools = ["system_snapshot"]
    cited_urls = []
    if task_kind == "coding":
        tools = ["read_file", "write_file", "run_process"]
        verification.update({
            "inspected_before_write": True,
            "content_write_completed": True,
            "inspected_after_write": True,
            "verified_after_write": True,
            "adversarial_probe_passed": True,
        })
    elif task_kind == "learning":
        tools = ["web_search", "web_fetch"]
        cited_urls = [
            "https://docs.ollama.com/context-length",
            "https://owasp.org/",
        ]
    return {
        "quality_contract_version": TRAINING_QUALITY_CONTRACT_VERSION,
        "verification": verification,
        "successful_tools": tools,
        "cited_verified_urls": cited_urls,
        "verified_urls": cited_urls,
    }


class _StatusMemory:
    def __init__(self, examples, evaluations):
        self.examples = examples
        self.evaluations = evaluations

    def list_training_examples(self, *, verified_only=False):
        self.requested_verified_only = verified_only
        return list(self.examples)

    def list_evaluation_cases(self):
        return list(self.evaluations)


class TrainingPipelineTests(unittest.TestCase):
    def test_training_readiness_requires_conservative_boundary_coverage(self):
        self.assertEqual(READINESS_MIN_VERIFIED_EXAMPLES, 100)
        self.assertEqual(READINESS_MIN_TRAIN_EXAMPLES, 70)
        self.assertEqual(READINESS_MIN_VALIDATION_EXAMPLES, 10)
        self.assertEqual(READINESS_MIN_TEST_EXAMPLES, 10)
        self.assertEqual(READINESS_MIN_ENABLED_EVALUATIONS, 10)
        self.assertEqual(READINESS_MIN_CAPABILITY_EXAMPLES, 10)

        kinds = ["coding", "local", "learning"]
        targets = {"train": 80, "validation": 10, "test": 10}
        examples = []
        candidate = 0
        while any(targets.values()):
            task_kind = kinds[candidate % len(kinds)]
            prompt = f"verified task {candidate}"
            split = training_prompt_split(prompt, task_kind)
            candidate += 1
            if targets[split] <= 0:
                continue
            targets[split] -= 1
            examples.append({
                "verified": 1,
                "quality_score": READINESS_MIN_QUALITY,
                "split": split,
                "task_kind": task_kind,
                "prompt": prompt,
                "response": (
                    LEARNING_RESPONSE if task_kind == "learning"
                    else "The requested verified operation completed successfully."
                ),
                "evidence_json": json.dumps(_quality_evidence(task_kind)),
            })
        memory = _StatusMemory(
            examples,
            [{"enabled": 1} for _ in range(READINESS_MIN_ENABLED_EVALUATIONS)],
        )

        status = dataset_status(memory)

        self.assertFalse(memory.requested_verified_only)
        self.assertTrue(status["ready_for_candidate_training"])
        self.assertEqual(status["readiness_blockers"], [])
        self.assertEqual(status["training_eligible"], 100)
        self.assertEqual(status["eligible_splits"]["validation"], 10)
        self.assertEqual(status["eligible_splits"]["test"], 10)
        self.assertGreaterEqual(status["capability_counts"]["research"], 10)

    def test_training_readiness_reports_every_material_blocker(self):
        examples = [{
            "verified": 1,
            "quality_score": READINESS_MIN_QUALITY - 0.01,
            "split": "train",
            "task_kind": "local",
        }]
        memory = _StatusMemory(
            examples,
            [{"enabled": 1} for _ in range(9)] + [{"enabled": 0}],
        )

        status = dataset_status(memory)
        blockers = " ".join(status["readiness_blockers"])

        self.assertFalse(status["ready_for_candidate_training"])
        self.assertEqual(status["training_eligible"], 0)
        self.assertEqual(status["enabled_evaluation_cases"], 9)
        self.assertIn("100 verified examples", blockers)
        self.assertIn("70 training examples", blockers)
        self.assertIn("10 validation examples", blockers)
        self.assertIn("10 test examples", blockers)
        self.assertIn("10 enabled evaluation cases", blockers)
        self.assertIn("coding=0", blockers)
        self.assertIn("local=0", blockers)
        self.assertIn("research=0", blockers)

    def test_examples_are_deduplicated_and_split_deterministically(self):
        with Memory(Path(":memory:")) as memory:
            conversation = memory.new_conversation("training")
            first = memory.add_training_example(
                prompt="Inspect the module",
                response="The module is valid.",
                model="qwen3:8b",
                profile="fast",
                task_kind="local",
                evidence={"successful_tools": ["read_file"]},
                quality_score=0.85,
                verified=True,
                conversation_id=conversation,
            )
            duplicate = memory.add_training_example(
                prompt="Inspect the module",
                response="The module is valid.",
                model="qwen3:8b",
                profile="fast",
                task_kind="local",
                evidence={"successful_tools": ["read_file"]},
                quality_score=0.99,
                verified=True,
                conversation_id=conversation,
            )
            self.assertIsNotNone(first)
            self.assertIsNone(duplicate)
            rows = memory.list_training_examples()
            self.assertEqual(len(rows), 1)
            expected_split = training_prompt_split("Inspect the module", "local")
            self.assertEqual(rows[0]["split"], expected_split)

            memory.add_training_example(
                prompt="  INSPECT   the module ",
                response="A second independently verified module result.",
                model="qwen3:8b",
                profile="fast",
                task_kind="local",
                evidence={"successful_tools": ["system_snapshot"]},
                quality_score=0.85,
                verified=True,
                conversation_id=conversation,
            )
            rows = memory.list_training_examples()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["split"] for row in rows}, {expected_split})

    def test_export_includes_only_verified_examples_above_threshold(self):
        with Memory(Path(":memory:")) as memory, tempfile.TemporaryDirectory() as temporary:
            for index, (verified, quality) in enumerate(((True, 1.0), (True, 0.5), (False, 1.0))):
                memory.add_training_example(
                    prompt=f"prompt {index}",
                    response=f"System snapshot {index} completed with verified output.",
                    model="qwen3:8b",
                    profile="fast",
                    task_kind="local",
                    evidence={
                        **_quality_evidence("local"),
                        "verified_urls": [
                            "https://one.example/source",
                            "https://two.example/source",
                        ],
                        "note": "access_token=must-not-export",
                        "structured": '{"api_key":"hunter2"}',
                        "github": "github_pat_" + "A" * 30,
                        "jwt": "eyJabcdefghij.eyJklmnopqrst.eyJuvwxyzABCD",
                    },
                    quality_score=quality,
                    verified=verified,
                )
            output = Path(temporary) / "dataset"
            manifest = export_verified_dataset(memory, output, min_quality=0.8)
            self.assertEqual(manifest["total_examples"], 1)
            total_lines = 0
            exported_records = []
            for _split, details in manifest["files"].items():
                content = (output / details["file"]).read_text(encoding="utf-8")
                self.assertEqual(details["sha256"], hashlib.sha256(content.encode()).hexdigest())
                total_lines += len(content.splitlines())
                exported_records.extend(
                    json.loads(line) for line in content.splitlines()
                )
            self.assertEqual(total_lines, 1)
            self.assertEqual(manifest["format_version"], 4)
            self.assertTrue(manifest["selection"]["authoritative_web_sources"])
            self.assertEqual(
                manifest["selection"]["current_quality_contract"],
                TRAINING_QUALITY_CONTRACT_VERSION,
            )
            self.assertTrue(manifest["selection"]["prompt_grouped_splits"])
            evidence = exported_records[0]["metadata"]["evidence"]
            self.assertEqual(
                evidence["verified_urls"],
                ["https://one.example/source", "https://two.example/source"],
            )
            self.assertNotIn("must-not-export", json.dumps(evidence))
            self.assertNotIn("hunter2", json.dumps(evidence))
            self.assertNotIn("github_pat_", json.dumps(evidence))
            self.assertNotIn("eyJabcdefghij", json.dumps(evidence))
            self.assertIn("[REDACTED]", evidence["note"])
            exported = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(exported, manifest)

    def test_low_authority_web_examples_are_quarantined_from_export(self):
        with Memory(Path(":memory:")) as memory, tempfile.TemporaryDirectory() as temporary:
            memory.add_training_example(
                prompt="learn blogs",
                response="blog brief",
                model="qwen3.5:9b",
                profile="fast",
                task_kind="learning",
                evidence={
                    **_quality_evidence("learning"),
                    "cited_verified_urls": [
                        "https://independent-notes.example/posts/claim/",
                        "https://community-benchmarks.example/posts/claim/",
                    ]
                },
                quality_score=0.95,
                verified=True,
            )
            status = dataset_status(memory)
            self.assertEqual(status["verified"], 1)
            self.assertEqual(status["training_eligible"], 0)
            self.assertEqual(status["source_quarantined"], 1)
            manifest = export_verified_dataset(memory, Path(temporary) / "dataset")
            self.assertEqual(manifest["total_examples"], 0)

    def test_current_quality_contract_quarantines_observed_bad_examples(self):
        with Memory(Path(":memory:")) as memory, tempfile.TemporaryDirectory() as temporary:
            cases = [
                (
                    "legacy local promise",
                    "Perfect, I will build that dashboard for you right now.",
                    "local",
                    {"successful_tools": ["list_files"]},
                ),
                (
                    "placeholder research",
                    "Dated Brief 2026-01-XX. " + LEARNING_RESPONSE,
                    "learning",
                    _quality_evidence("learning"),
                ),
                (
                    "failed research",
                    "I'm sorry, but I couldn't locate any reliable, up-to-date information. "
                    + LEARNING_RESPONSE,
                    "learning",
                    _quality_evidence("learning"),
                ),
                (
                    "inspection only",
                    "I will create the requested application next.",
                    "local",
                    {
                        **_quality_evidence("local"),
                        "successful_tools": ["list_files", "read_file"],
                    },
                ),
            ]
            for prompt, response, task_kind, evidence in cases:
                memory.add_training_example(
                    prompt=prompt,
                    response=response,
                    model="test-model",
                    profile="fast",
                    task_kind=task_kind,
                    evidence=evidence,
                    quality_score=0.95,
                    verified=True,
                )

            status = dataset_status(memory)
            self.assertEqual(status["verified"], 4)
            self.assertEqual(status["training_eligible"], 0)
            self.assertEqual(status["quality_quarantined"], 4)
            self.assertEqual(status["quarantine_reasons"], {
                "legacy_quality_contract": 1,
                "placeholder_content": 1,
                "no_research_finding": 1,
                "local_outcome_not_proven": 1,
            })
            manifest = export_verified_dataset(memory, Path(temporary) / "dataset")
            self.assertEqual(manifest["candidate_examples"], 4)
            self.assertEqual(manifest["total_examples"], 0)
            self.assertEqual(manifest["quarantined"]["current_quality_contract"], 4)

    def test_evaluation_cases_are_upserted_and_parsed(self):
        with Memory(Path(":memory:")) as memory:
            first = memory.add_evaluation_case("identity", "Who are you?", ["JARVIS"])
            second = memory.add_evaluation_case("identity", "State your name", ["Jarvis", "local"])
            self.assertEqual(first, second)
            cases = memory.list_evaluation_cases()
            self.assertEqual(len(cases), 1)
            self.assertEqual(parse_expected_terms(cases[0]["expected_contains_json"]), ["Jarvis", "local"])
            self.assertEqual(dataset_status(memory)["evaluation_cases"], 1)

    def test_finetune_dry_run_validates_export_without_training_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "train.jsonl"
            dataset.write_text(json.dumps({
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "Hello."},
                ]
            }) + "\n", encoding="utf-8")
            self.assertEqual(len(_load_records(dataset)), 1)
            with patch("sys.stdout"):
                finetune_main([
                    "--dataset", str(dataset),
                    "--output", str(Path(temporary) / "adapter"),
                    "--dry-run",
                ])


if __name__ == "__main__":
    unittest.main()
