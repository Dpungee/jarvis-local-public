"""Memory extraction baseline plus individually pinned behavioral corrections."""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from jarvis import memory as facade
from jarvis.memory import Memory
from jarvis.memory_runtime import _memory
from tests.structural_ast import structural_dump

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests/fixtures/memory_split_ast_v1.json").read_text())
# Preserve historical bodies and explicitly pin the one reviewed correction.
# Both split-winner and pre-recorded-receipt regressions reject the old behavior.
REVIEWED_METHOD_CHANGES = {
    "promote_strategy_transfer_trial": (
        "0925f4afe65ecb5555fdfb5be9f2adc2b7f968b776a0d15c8770facb7c0fc382",
        "59b74163d79cf7cfa1c27e6a467b3c8e46f730ef62a15f663fd90f91623349e6",
    ),
}


class _OriginalGlobals(ast.NodeTransformer):
    """Undo only the extraction's late-facade lookup for exact AST comparison."""

    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_memory"
            and not node.value.args
            and not node.value.keywords
        ):
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def _classes(module):
    tree = ast.parse((ROOT / "jarvis" / f"{module}.py").read_text(encoding="utf-8"))
    return [node for node in tree.body if isinstance(node, ast.ClassDef)]


class MemorySplitTests(unittest.TestCase):
    def test_every_current_method_has_exactly_one_owner(self):
        owners = {}
        for owner in Memory.__mro__:
            if owner is object:
                continue
            for name, member in vars(owner).items():
                if callable(member) or isinstance(member, (staticmethod, classmethod, property)):
                    self.assertNotIn(name, owners, f"duplicate method: {name}")
                    owners[name] = owner.__module__.split(".")[-1]
        self.assertEqual(len(owners), 510)
        self.assertEqual(owners, {name: row["module"] for name, row in MANIFEST["methods"].items()})
        self.assertEqual(set(vars(Memory)) & set(owners), set(MANIFEST["core_methods"]))

    def test_current_bodies_match_baseline_or_exact_reviewed_correction(self):
        seen = set()
        for module in ["memory", *MANIFEST["domains"]]:
            expected_class = "Memory" if module == "memory" else MANIFEST["domains"][module]
            cls = next(c for c in _classes(module) if c.name == expected_class)
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                with self.subTest(module=module, method=method.name):
                    normalized = _OriginalGlobals().visit(method)
                    digest = hashlib.sha256(structural_dump(normalized).encode()).hexdigest()
                    expected = MANIFEST["methods"][method.name]["sha256"]
                    if method.name in REVIEWED_METHOD_CHANGES:
                        baseline, corrected = REVIEWED_METHOD_CHANGES[method.name]
                        self.assertEqual(expected, baseline)
                        expected = corrected
                    self.assertEqual(digest, expected)
                    self.assertNotIn(method.name, seen)
                    seen.add(method.name)
        self.assertEqual(seen, set(MANIFEST["methods"]))

    def test_reviewed_change_set_is_exact_and_has_adversarial_regressions(self):
        from tests.test_strategy_transfer_trial_memory import StrategyTransferTrialMemoryTests

        self.assertEqual(set(REVIEWED_METHOD_CHANGES), {"promote_strategy_transfer_trial"})
        for name in (
            "test_promotion_reports_transition_winner_when_attestation_winner_waits",
            "test_promotion_recovers_an_already_recorded_attestation",
        ):
            self.assertTrue(callable(getattr(StrategyTransferTrialMemoryTests, name)))

    def test_class_constants_keep_their_exact_current_definitions(self):
        cls = next(c for c in _classes("memory") if c.name == "Memory")
        actual = [
            hashlib.sha256(structural_dump(node).encode()).hexdigest()
            for node in cls.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertEqual(actual, MANIFEST["class_statements"])

    def test_domains_import_without_importing_the_memory_facade(self):
        for module in ["memory_runtime", *MANIFEST["domains"]]:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", f"import sys; import jarvis.{module}; assert 'jarvis.memory' not in sys.modules"],
                    cwd=ROOT, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_late_accessor_preserves_facade_patch_seams(self):
        for module in MANIFEST["domains"]:
            self.assertIs(importlib.import_module(f"jarvis.{module}")._memory, _memory)
        self.assertIs(_memory(), facade)
        with Memory(":memory:") as store:
            with patch("jarvis.memory.now_iso", return_value="2026-01-02T03:04:05+00:00"):
                project = store.add_project("Synthetic project", "@projects/synthetic")
            row = store.db.execute("SELECT created_at FROM agent_projects WHERE id=?", (project,)).fetchone()
            self.assertEqual(row[0], "2026-01-02T03:04:05+00:00")

    def test_all_governance_generations_remain_available(self):
        required = {
            "remember_explicit_project_claim", "retract_explicit_project_claim",
            "erase_explicit_project_claim", "erase_memory", "verify_spine",
            "rebuild_claim_projection", "verify_graph", "graph_chains",
            "stage_ladder_promotion", "apply_ladder_promotion", "rollback_ladder_promotion",
            "withdraw_ladder_promotion", "compact_conversation", "rehydrate",
            "verify_compaction", "prior_conversation_excerpts",
        }
        self.assertLessEqual(required, set(MANIFEST["methods"]))
        self.assertTrue(all(callable(getattr(Memory, name)) for name in required))
