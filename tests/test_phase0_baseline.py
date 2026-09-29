from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.build_phase0_baseline as phase0_baseline
from scripts.build_phase0_baseline import (
    BaselineError,
    PROJECT_ROOT,
    _bundle_sha256,
    _public_tool_manifest,
    collect_baseline,
)


class PhaseZeroBaselineTests(unittest.TestCase):
    def test_migration_digest_includes_the_extracted_migration_implementation(self) -> None:
        self.assertIn("jarvis/memory_schema_migrations.py", phase0_baseline._MIGRATION_SOURCE_FILES)

    def test_baseline_is_sanitized_and_binds_current_source(self) -> None:
        with patch.dict(os.environ, {"JARVIS_PUBLIC_PRESENCE_ENABLED": "false"}):
            baseline = collect_baseline(PROJECT_ROOT, require_clean=False)

        rendered = json.dumps(baseline, sort_keys=True)
        self.assertEqual(baseline["schema"], "jarvis.phase0-release-baseline.v1")
        self.assertRegex(baseline["repository"]["commit"], r"^[0-9a-f]{40}$")
        self.assertGreater(baseline["runtime"]["private_schema_version"], 0)
        self.assertGreater(baseline["runtime"]["public_schema_version"], 0)
        self.assertGreaterEqual(baseline["private_tool_manifest"]["count"], 100)
        self.assertEqual(
            baseline["public_tool_manifest"]["count"],
            len(_public_tool_manifest()),
        )
        self.assertEqual(baseline["public_tool_manifest"]["publishing_methods"], [])
        self.assertFalse(baseline["safe_configuration"]["public_presence_enabled"])
        self.assertFalse(baseline["safe_configuration"]["external_communication"])
        self.assertNotRegex(rendered, r"(?i)[a-z]:\\users\\")
        self.assertNotIn("@", rendered)

    def test_baseline_refuses_enabled_public_presence(self) -> None:
        with patch.dict(os.environ, {"JARVIS_PUBLIC_PRESENCE_ENABLED": "true"}):
            with self.assertRaisesRegex(BaselineError, "must be disabled"):
                collect_baseline(PROJECT_ROOT, require_clean=False)

    def test_clean_gate_rejects_the_current_dirty_tree(self) -> None:
        real_git = phase0_baseline._git

        def dirty_status(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ("status", "--porcelain=v1"):
                return " M synthetic"
            return real_git(repo, *arguments)

        with patch.object(phase0_baseline, "_git", side_effect=dirty_status):
            with self.assertRaisesRegex(BaselineError, "must be clean"):
                collect_baseline(PROJECT_ROOT)

    def test_baseline_rejects_assume_unchanged_or_skip_worktree_flags(self) -> None:
        real_git = phase0_baseline._git

        def flagged_index(repo: Path, *arguments: str) -> str:
            if arguments[:3] == ("ls-files", "-v", "-z"):
                return "S private.txt\0"
            return real_git(repo, *arguments)

        with patch.object(phase0_baseline, "_git", side_effect=flagged_index):
            with self.assertRaisesRegex(BaselineError, "tracked-file flags"):
                collect_baseline(PROJECT_ROOT)

    def test_baseline_rechecks_commit_after_collecting_evidence(self) -> None:
        real_git = phase0_baseline._git
        revisions = iter([real_git(PROJECT_ROOT, "rev-parse", "HEAD"), "2" * 40])

        def moving_head(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ("rev-parse", "HEAD"):
                return next(revisions)
            return real_git(repo, *arguments)

        with patch.object(phase0_baseline, "_git", side_effect=moving_head):
            with self.assertRaisesRegex(BaselineError, "changed during"):
                collect_baseline(PROJECT_ROOT, require_clean=False)

    def test_bundle_digest_binds_relative_paths_as_well_as_bytes(self) -> None:
        first = PROJECT_ROOT / "tests" / ".tmp-phase0-a"
        second = PROJECT_ROOT / "tests" / ".tmp-phase0-b"
        try:
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            one = _bundle_sha256(PROJECT_ROOT, ("tests/.tmp-phase0-a",))
            two = _bundle_sha256(PROJECT_ROOT, ("tests/.tmp-phase0-b",))
            self.assertNotEqual(one, two)
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)

    def test_ci_normalizes_and_compares_repeat_distribution_builds(self) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("scripts/normalize_sdist.py --source-epoch", workflow)
        self.assertIn("Repeat distribution build failed", workflow)
        self.assertIn("Distribution is not reproducible", workflow)
        self.assertIn("Normalized source distribution installation failed", workflow)


if __name__ == "__main__":
    unittest.main()
