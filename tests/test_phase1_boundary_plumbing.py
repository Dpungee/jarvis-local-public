"""Moved implementations retain repair protection and byte-bound runtime pins."""
from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from jarvis.long_horizon_eval import long_horizon_runtime_sha256
from jarvis.learning_ladder import LADDER_RUNTIME_FILES, learning_ladder_runtime_sha256
from jarvis.memory_graph import MEMORY_GRAPH_RUNTIME_FILES, memory_graph_runtime_sha256
from jarvis.memory_compaction import COMPACTION_RUNTIME_FILES, compaction_runtime_sha256
from jarvis import self_diagnosis
from jarvis.strategy_transfer_trial import strategy_transfer_runtime_sha256, sha256_json


MEMORY_PIN_FILES = (
    "memory_retrieval.py",
    "memory_embeddings.py",
    "memory_runtime.py",
    "memory_schema_migrations.py",
    "memory_projects_budget.py",
    "memory_predictions.py",
    "memory_conversations.py",
    "memory_presence_companion.py",
    "memory_ordinary_recall.py",
    "memory_claims.py",
    "memory_embedding_store.py",
    "memory_lessons.py",
    "memory_strategy_transfer.py",
    "memory_strategy_trial.py",
    "memory_tasks_scheduling.py",
    "memory_operator_state.py",
    "memory_approvals.py",
    "memory_learning_ladder.py",
    "memory_governance.py",
    "memory_conversation_compaction.py",
)
TOOL_SPLIT_FILES = (
    "tools_memory_agent.py", "tools_external_services.py", "tools_documents_media.py",
    "tools_workspace_files.py", "tools_desktop_system.py", "tools_skills_features.py",
    "tools_processes.py", "tools_dependencies.py", "tools_web_research.py",
)
ROOT = Path(__file__).resolve().parents[1] / "jarvis"


class Phase1BoundaryPlumbingTests(unittest.TestCase):
    def test_every_moved_implementation_exists_and_is_immutable_to_self_repair(self):
        for name in (*MEMORY_PIN_FILES, *TOOL_SPLIT_FILES, "agent_coding_verification.py"):
            with self.subTest(module=name):
                self.assertTrue((ROOT / name).is_file())
                relative = "jarvis/" + name
                self.assertIn(relative, self_diagnosis._IMMUTABLE_REPAIR_FILES)
                self.assertIsNotNone(self_diagnosis._repair_path_reason(relative))

    def test_memory_files_are_pinned_by_both_consumers_and_each_byte_change_matters(self):
        consumers = (
            (long_horizon_runtime_sha256,
             {"long_horizon.py", "memory.py", "long_horizon_eval_worker.py", *MEMORY_PIN_FILES}),
            (strategy_transfer_runtime_sha256,
             {"agent.py", "agent_coding_verification.py", "memory.py",
              "strategy_transfer.py", "strategy_transfer_trial.py", *MEMORY_PIN_FILES}),
        )
        for consumer, expected in consumers:
            with self.subTest(consumer=consumer.__name__):
                calls = []
                def read_bytes(path, calls=calls):
                    calls.append(path.name)
                    return path.name.encode()
                with patch.object(Path, "read_bytes", read_bytes):
                    baseline = consumer()
                self.assertEqual(set(calls), expected)
                self.assertEqual(len(calls), len(expected), "No duplicate or omitted hash inputs")
                material = {name: hashlib.sha256(name.encode()).hexdigest()
                            for name in expected}
                self.assertEqual(baseline, sha256_json(material))
                for changed in expected:
                    def mutated_bytes(path, changed=changed):
                        return (path.name + (" changed" if path.name == changed else "")).encode()
                    with patch.object(Path, "read_bytes", mutated_bytes):
                        self.assertNotEqual(consumer(), baseline, changed)

    def test_missing_pinned_implementation_fails_closed(self):
        def missing_file(path):
            if path.name == "memory_governance.py":
                raise FileNotFoundError(path.name)
            return path.name.encode()
        for consumer in (long_horizon_runtime_sha256, strategy_transfer_runtime_sha256,
                         learning_ladder_runtime_sha256, memory_graph_runtime_sha256,
                         compaction_runtime_sha256):
            with self.subTest(consumer=consumer.__name__):
                with patch.object(Path, "read_bytes", missing_file):
                    with self.assertRaises(FileNotFoundError):
                        consumer()

    def test_governance_pins_cover_extracted_bodies_and_every_mutation_changes_digest(self):
        for consumer, registry in (
            (learning_ladder_runtime_sha256, LADDER_RUNTIME_FILES),
            (memory_graph_runtime_sha256, MEMORY_GRAPH_RUNTIME_FILES),
            (compaction_runtime_sha256, COMPACTION_RUNTIME_FILES),
        ):
            with self.subTest(consumer=consumer.__name__):
                names = [Path(name).name for name in registry]
                self.assertTrue(set(MEMORY_PIN_FILES) <= set(names))
                self.assertEqual(len(names), len(set(names)))
                self.assertNotIn("agent.py", names)
                self.assertNotIn("tools.py", names)
                with patch.object(Path, "read_bytes", lambda path: path.name.encode()):
                    baseline = consumer()
                for changed in names:
                    def mutated_bytes(path, changed=changed):
                        return (path.name + (" changed" if path.name == changed else "")).encode()
                    with patch.object(Path, "read_bytes", mutated_bytes):
                        self.assertNotEqual(consumer(), baseline, changed)


if __name__ == "__main__":
    unittest.main()
