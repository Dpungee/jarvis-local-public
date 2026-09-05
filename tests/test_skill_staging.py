"""The staging root: unreachable from the catalog, and byte-exact (M4, 7.6).

The second half of the invisibility guarantee -- that the model's file tools
refuse ``.jarvis-skills-staging`` -- is asserted in ``tests/test_tools_hardening.py``
beside the live root's identical probe, because ``tools.py`` is not this
module's file.  Everything here is the library layer: bytes move correctly, or
they do not move at all.
"""
from __future__ import annotations

import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import learning_ladder as ladder
from jarvis import skill_library as library
from jarvis.skill_evolution import auto_skill_name, matching_auto_distilled_skills

_FAMILY = "code_fix"
_GATE = {
    "family": _FAMILY, "allowed": True, "attempts": 30,
    "brier": 0.16, "calibration_error": 0.0,
}

# A GitHub personal-access-token shape, used only as negative test data:
# the screens under test must refuse it.  Assembled at import time so the
# scanned source never carries a literal matching the ``github-pat`` rule,
# exactly as ``ec4e655`` did for the AWS shape in
# ``tests/test_memory_spine_integration.py``.  The runtime value is
# unchanged and the screen still sees the whole token.
# DO NOT rejoin this into one string literal.
_PAT_SHAPED = "ghp_" + "16c7e42f292c6912e7710c838347ae178b4a"


class SkillStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.name = auto_skill_name(_FAMILY)
        self.repository = Path(__file__).resolve().parent.parent
        self.working = Path.cwd()

    def tearDown(self) -> None:
        # M3 L-3: 46 stray sidecar files came from tests that wrote outside
        # their own temporary directory.
        for root in (self.repository, self.working):
            for directory in (
                library.LEARNED_SKILL_DIRECTORY, library.STAGED_SKILL_DIRECTORY
            ):
                self.assertFalse(
                    (root / directory).exists(), f"stray {directory} in {root}"
                )

    # --- helpers ----------------------------------------------------------

    def _body(self, **overrides: object) -> str:
        arguments: dict[str, object] = {
            "family": _FAMILY, "reuses": 3, "contexts": 3,
            "tool_names": ["read_file"], "oracles": ["tool_success"],
            "gate": _GATE, "epoch": 1, "monotone": True, "lift_pp": 8.0,
        }
        arguments.update(overrides)
        return ladder.build_staged_document(**arguments)   # type: ignore[arg-type]

    def _stage(self, body: str | None = None, outcomes: int = 3) -> dict[str, object]:
        return library.stage_learned_skill(
            self.workspace,
            self.name,
            ladder.staged_skill_description(_FAMILY),
            self._body() if body is None else body,
            family=_FAMILY,
            verified_outcomes=outcomes,
        )

    @property
    def _staged_file(self) -> Path:
        return (
            self.workspace / library.STAGED_SKILL_DIRECTORY / self.name / "SKILL.md"
        )

    @property
    def _live_file(self) -> Path:
        return (
            self.workspace / library.LEARNED_SKILL_DIRECTORY / self.name / "SKILL.md"
        )

    # --- placement --------------------------------------------------------

    def test_the_staging_root_is_a_sibling_of_the_live_root(self) -> None:
        self._stage()
        staged_root = self.workspace / library.STAGED_SKILL_DIRECTORY
        live_root = self.workspace / library.LEARNED_SKILL_DIRECTORY
        self.assertNotEqual(library.STAGED_SKILL_DIRECTORY, library.LEARNED_SKILL_DIRECTORY)
        self.assertEqual(staged_root.parent, self.workspace)
        self.assertTrue(staged_root.is_dir())
        self.assertFalse(live_root.exists())
        self.assertNotIn(live_root, staged_root.parents)

    def test_a_staged_document_is_invisible_to_every_catalog_reader(self) -> None:
        staged = self._stage()
        catalog = library.list_available_skills(self.workspace)
        self.assertNotIn(self.name, {item["name"] for item in catalog})
        with self.assertRaises(KeyError):
            library.read_available_skill(self.name, self.workspace)
        self.assertEqual(
            matching_auto_distilled_skills(self.workspace, _FAMILY), []
        )
        self.assertEqual(
            [item["name"] for item in library.list_staged_skills(self.workspace)],
            [self.name],
        )
        self.assertEqual(
            library.read_staged_skill(self.name, self.workspace)["sha256"],
            staged["sha256"],
        )

    def test_a_staged_document_carries_its_own_origin_and_trust(self) -> None:
        staged = self._stage()
        self.assertEqual(staged["origin"], "workspace-staged")
        self.assertIn("never reaches the model", str(staged["trust"]))
        self.assertTrue(staged["staged"])
        self.assertTrue(staged["auto_distilled"])
        self.assertEqual(staged["family"], _FAMILY)

    def test_an_empty_workspace_lists_no_staged_skills(self) -> None:
        self.assertEqual(library.list_staged_skills(self.workspace), [])
        with self.assertRaises(KeyError):
            library.read_staged_skill(self.name, self.workspace)
        with self.assertRaises(KeyError):
            library.discard_staged_skill(self.workspace, self.name)
        with self.assertRaises(KeyError):
            library.promote_staged_skill(
                self.workspace, self.name, expected_staged_sha256="a" * 64
            )

    # --- promotion --------------------------------------------------------

    def test_promotion_installs_the_exact_staged_bytes_and_clears_staging(self) -> None:
        staged = self._stage()
        result = library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        self.assertEqual(result["approved_sha256"], staged["sha256"])
        self.assertIsNone(result["prior_document"])
        self.assertIsNone(result["prior_sha256"])
        self.assertEqual(result["name"], self.name)
        self.assertEqual(library.list_staged_skills(self.workspace), [])
        self.assertFalse(self._staged_file.exists())
        self.assertIn(
            self.name,
            {item["name"] for item in library.list_available_skills(self.workspace)},
        )
        self.assertEqual(
            library.read_available_skill(self.name, self.workspace)["sha256"],
            staged["sha256"],
        )

    def test_a_second_promotion_hands_back_the_previous_bytes_exactly(self) -> None:
        first = self._stage()
        promoted = library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(first["sha256"])
        )
        before = self._live_file.read_bytes()
        second = self._stage(self._body(reuses=5, lift_pp=None), outcomes=5)
        self.assertNotEqual(second["sha256"], first["sha256"])
        result = library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(second["sha256"])
        )
        self.assertEqual(result["prior_document"], before)
        self.assertEqual(result["prior_sha256"], promoted["approved_sha256"])
        self.assertEqual(result["approved_sha256"], second["sha256"])

    def test_a_stale_expected_digest_refuses_and_moves_nothing(self) -> None:
        staged = self._stage()
        with self.assertRaises(RuntimeError):
            library.promote_staged_skill(
                self.workspace, self.name, expected_staged_sha256="f" * 64
            )
        self.assertTrue(self._staged_file.exists())
        self.assertFalse(self._live_file.exists())
        self.assertEqual(
            library.read_staged_skill(self.name, self.workspace)["sha256"],
            staged["sha256"],
        )

    def test_a_malformed_expected_digest_is_refused_before_any_read(self) -> None:
        self._stage()
        for bad in ("", "not-a-digest", "A" * 64, "a" * 63):
            with self.assertRaises(ValueError):
                library.promote_staged_skill(
                    self.workspace, self.name, expected_staged_sha256=bad
                )

    def test_a_hard_link_on_either_side_refuses_the_promotion(self) -> None:
        staged = self._stage()
        live_directory = self.workspace / library.LEARNED_SKILL_DIRECTORY / self.name
        live_directory.mkdir(parents=True)
        os.link(self._staged_file, self._live_file)
        self.assertGreater(os.lstat(self._staged_file).st_nlink, 1)
        with self.assertRaises(PermissionError) as caught:
            library.promote_staged_skill(
                self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
            )
        self.assertIn("Hard-linked", str(caught.exception))
        self.assertTrue(self._staged_file.exists())

    def test_a_hard_linked_live_document_refuses_the_promotion(self) -> None:
        first = self._stage()
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(first["sha256"])
        )
        second = self._stage(self._body(reuses=4), outcomes=4)
        shadow = self.workspace / "shadow.md"
        os.link(self._live_file, shadow)
        with self.assertRaises(PermissionError):
            library.promote_staged_skill(
                self.workspace, self.name, expected_staged_sha256=str(second["sha256"])
            )
        self.assertEqual(self._live_file.read_bytes(), shadow.read_bytes())

    def test_a_bundled_name_can_never_be_staged_or_promoted(self) -> None:
        bundled = library.list_builtin_skills()[0]["name"]
        with self.assertRaises(PermissionError):
            library.stage_learned_skill(
                self.workspace, bundled, "A description.", "Body.",
                family=_FAMILY, verified_outcomes=1,
            )
        with self.assertRaises(PermissionError):
            library.promote_staged_skill(
                self.workspace, bundled, expected_staged_sha256="a" * 64
            )

    def test_a_staged_directory_with_extra_files_is_refused(self) -> None:
        staged = self._stage()
        (self._staged_file.parent / "NOTES.md").write_text("x", encoding="utf-8")
        with self.assertRaises(PermissionError):
            library.promote_staged_skill(
                self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
            )
        with self.assertRaises(PermissionError):
            library.discard_staged_skill(self.workspace, self.name)

    # --- staging writes ---------------------------------------------------

    def test_restaging_replaces_the_derived_document_rather_than_refusing(self) -> None:
        first = self._stage()
        second = self._stage(self._body(reuses=9), outcomes=9)
        self.assertNotEqual(second["sha256"], first["sha256"])
        self.assertEqual(
            library.read_staged_skill(self.name, self.workspace)["sha256"],
            second["sha256"],
        )
        self.assertEqual(len(library.list_staged_skills(self.workspace)), 1)

    def test_a_secret_shaped_body_is_never_staged(self) -> None:
        with self.assertRaises(ValueError):
            library.stage_learned_skill(
                self.workspace, self.name, ladder.staged_skill_description(_FAMILY),
                f"Use {_PAT_SHAPED} when calling.",
                family=_FAMILY, verified_outcomes=1,
            )
        self.assertEqual(library.list_staged_skills(self.workspace), [])

    def test_an_oversized_body_is_never_staged(self) -> None:
        with self.assertRaises(ValueError):
            library.stage_learned_skill(
                self.workspace, self.name, ladder.staged_skill_description(_FAMILY),
                "x" * (library.MAX_SKILL_BYTES + 1),
                family=_FAMILY, verified_outcomes=1,
            )

    def test_a_staged_document_needs_a_family_and_a_verified_outcome(self) -> None:
        for family, outcomes in ((None, 3), (_FAMILY, 0), ("Not A Family", 3)):
            with self.assertRaises(ValueError):
                library.stage_learned_skill(
                    self.workspace, self.name,
                    ladder.staged_skill_description(_FAMILY), self._body(),
                    family=family, verified_outcomes=outcomes,   # type: ignore[arg-type]
                )

    def test_an_invalid_skill_name_is_refused_everywhere(self) -> None:
        for bad in ("Learned Code Fix", "learned_code_fix", "", "a" * 70):
            with self.assertRaises(ValueError):
                library.stage_learned_skill(
                    self.workspace, bad, "A description.", "Body.",
                    family=_FAMILY, verified_outcomes=1,
                )
            # A name too long to stage can never name a staged document, so
            # the read is a KeyError rather than a shape refusal.
            with self.assertRaises((ValueError, KeyError)):
                library.read_staged_skill(bad, self.workspace)

    # --- discard and restore ---------------------------------------------

    def test_discard_removes_the_document_and_its_directory(self) -> None:
        staged = self._stage()
        result = library.discard_staged_skill(self.workspace, self.name)
        self.assertTrue(result["discarded"])
        self.assertEqual(result["sha256"], staged["sha256"])
        self.assertEqual(library.list_staged_skills(self.workspace), [])
        self.assertFalse(self._staged_file.parent.exists())

    def test_discard_refuses_a_hard_linked_document(self) -> None:
        self._stage()
        os.link(self._staged_file, self.workspace / "shadow.md")
        with self.assertRaises(PermissionError):
            library.discard_staged_skill(self.workspace, self.name)
        self.assertTrue(self._staged_file.exists())

    def test_discard_refuses_a_bad_name_and_an_absent_document(self) -> None:
        self._stage()
        with self.assertRaises(ValueError):
            library.discard_staged_skill(self.workspace, "Not A Name")
        with self.assertRaises(KeyError):
            library.discard_staged_skill(self.workspace, "learned-file-ops")
        with self.assertRaises(KeyError):
            library.read_staged_skill("learned-file-ops", self.workspace)

    def test_an_unreadable_staged_entry_is_skipped_not_fatal(self) -> None:
        self._stage()
        broken = self.workspace / library.STAGED_SKILL_DIRECTORY / "learned-file-ops"
        broken.mkdir()
        (broken / "SKILL.md").write_text("not a skill", encoding="utf-8")
        (self.workspace / library.STAGED_SKILL_DIRECTORY / "loose.txt").write_text(
            "x", encoding="utf-8"
        )
        self.assertEqual(
            [item["name"] for item in library.list_staged_skills(self.workspace)],
            [self.name],
        )

    def test_a_secret_shaped_prior_document_is_never_restored(self) -> None:
        staged = self._stage()
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        good = self._live_file.read_bytes()
        poisoned = (
            f"---\nname: {self.name}\ndescription: Guidance.\n"
            f"family: {_FAMILY}\nauto_distilled: true\nverified_outcomes: 1\n---\n\n"
            f"Use {_PAT_SHAPED} to authenticate.\n"
        ).encode("utf-8")
        with self.assertRaises(ValueError):
            library.restore_learned_skill(self.workspace, self.name, poisoned)
        self.assertEqual(self._live_file.read_bytes(), good)

    def test_restore_writes_back_the_exact_prior_bytes(self) -> None:
        first = self._stage()
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(first["sha256"])
        )
        original = self._live_file.read_bytes()
        second = self._stage(self._body(reuses=7), outcomes=7)
        promoted = library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(second["sha256"])
        )
        result = library.restore_learned_skill(
            self.workspace, self.name, promoted["prior_document"]
        )
        self.assertTrue(result["restored"])
        self.assertFalse(result["removed"])
        self.assertEqual(self._live_file.read_bytes(), original)
        self.assertEqual(result["restored_sha256"], first["sha256"])
        self.assertEqual(
            library.read_available_skill(self.name, self.workspace)["sha256"],
            first["sha256"],
        )

    def test_restoring_none_removes_the_live_document(self) -> None:
        staged = self._stage()
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        result = library.restore_learned_skill(self.workspace, self.name, None)
        self.assertTrue(result["removed"])
        self.assertFalse(result["restored"])
        self.assertEqual(result["removed_sha256"], staged["sha256"])
        self.assertFalse(self._live_file.parent.exists())
        self.assertNotIn(
            self.name,
            {item["name"] for item in library.list_available_skills(self.workspace)},
        )

    def test_restoring_none_over_nothing_is_a_quiet_no_op(self) -> None:
        result = library.restore_learned_skill(self.workspace, self.name, None)
        self.assertFalse(result["removed"])
        self.assertFalse(result["restored"])

    def test_restore_refuses_a_hard_linked_live_document(self) -> None:
        staged = self._stage()
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        document = self._live_file.read_bytes()
        os.link(self._live_file, self.workspace / "shadow.md")
        with self.assertRaises(PermissionError):
            library.restore_learned_skill(self.workspace, self.name, None)
        with self.assertRaises(PermissionError):
            library.restore_learned_skill(self.workspace, self.name, document)
        self.assertEqual(self._live_file.read_bytes(), document)

    def test_a_corrupt_prior_document_never_replaces_a_good_live_one(self) -> None:
        staged = self._stage()
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        good = self._live_file.read_bytes()
        for corrupt in (
            b"not a skill document at all",
            b"---\nname: someone-else\ndescription: x\n---\n\nBody.\n",
            b"x" * (library.MAX_SKILL_BYTES + 1),
        ):
            with self.assertRaises(ValueError):
                library.restore_learned_skill(self.workspace, self.name, corrupt)
            self.assertEqual(self._live_file.read_bytes(), good)

    def test_restore_creates_the_document_when_none_is_live(self) -> None:
        staged = self._stage()
        promoted = library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        document = self._live_file.read_bytes()
        library.restore_learned_skill(self.workspace, self.name, None)
        self.assertFalse(self._live_file.exists())
        result = library.restore_learned_skill(self.workspace, self.name, document)
        self.assertEqual(result["restored_sha256"], promoted["approved_sha256"])
        self.assertEqual(self._live_file.read_bytes(), document)

    # --- the bundled-catalog memo (HIGH performance) ----------------------

    def test_the_bundled_catalog_is_memoized_and_clearable(self) -> None:
        """2.03 ms of frontmatter parsing, repeated several times per call."""
        library.clear_builtin_cache()
        first = library.list_builtin_skills()
        second = library.list_builtin_skills()
        self.assertEqual(first, second)
        self.assertTrue(first)
        # Returned fresh, so a caller cannot poison the memo.
        first[0]["name"] = "tampered"
        self.assertNotEqual(library.list_builtin_skills()[0]["name"], "tampered")
        library.clear_builtin_cache()
        self.assertEqual(library.list_builtin_skills(), second)

    def test_read_learned_documents_is_complete_and_typed(self) -> None:
        self._stage()
        staged = library.list_staged_skills(self.workspace)
        self.assertTrue(staged)
        # Staged documents are not live and must not appear in the index.
        self.assertEqual(library.read_learned_documents(self.workspace), {})
        promoted = library.promote_staged_skill(
            self.workspace, self.name,
            expected_staged_sha256=str(self._stage()["sha256"]),
        )
        index = library.read_learned_documents(self.workspace)
        self.assertEqual(set(index), {self.name})
        entry = index[self.name]
        self.assertEqual(
            set(entry), {"name", "family", "verified_outcomes", "sha256", "content"}
        )
        self.assertEqual(entry["sha256"], promoted["approved_sha256"])
        self.assertEqual(entry["family"], _FAMILY)

    def test_read_learned_documents_skips_a_hand_written_skill(self) -> None:
        """Only auto-distilled documents are ladder artefacts."""
        library.create_learned_skill(
            self.workspace, "hand-written", "Operator guidance.", "Body text.",
        )
        self.assertEqual(library.read_learned_documents(self.workspace), {})

    def test_read_learned_documents_of_an_empty_workspace(self) -> None:
        self.assertEqual(library.read_learned_documents(self.workspace), {})

    # --- R-1(b): withdrawal moves the document, never leaves it live ------

    def _approve(self) -> dict[str, object]:
        staged = self._stage()
        return library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )

    def test_withdrawal_moves_the_live_document_out_of_the_catalog(self) -> None:
        """R-1: leaving the file live made the family unrecoverable forever."""
        approved = self._approve()
        document = self._live_file.read_bytes()
        result = library.withdraw_learned_skill(self.workspace, self.name)
        self.assertTrue(result["withdrawn"])
        self.assertTrue(result["moved"])
        self.assertEqual(result["sha256"], approved["approved_sha256"])
        self.assertIsNone(result["replaced_sha256"])
        self.assertEqual(
            result["withdrawn_name"],
            f"{library.WITHDRAWN_SKILL_PREFIX}{self.name}",
        )
        # Gone from the live root and from every catalog reader.
        self.assertFalse(self._live_file.exists())
        self.assertFalse(self._live_file.parent.exists())
        self.assertNotIn(
            self.name,
            {item["name"] for item in library.list_available_skills(self.workspace)},
        )
        with self.assertRaises(KeyError):
            library.read_available_skill(self.name, self.workspace)
        self.assertEqual(
            matching_auto_distilled_skills(self.workspace, _FAMILY), []
        )
        # And the bytes survive, in the staging root, unchanged.
        parked = Path(str(result["path"]))
        self.assertEqual(parked.read_bytes(), document)
        self.assertEqual(parked.parent.parent.name, library.STAGED_SKILL_DIRECTORY)

    def test_a_withdrawn_document_is_listed_with_its_stage(self) -> None:
        self._approve()
        library.withdraw_learned_skill(self.workspace, self.name)
        listed = library.list_staged_skills(self.workspace)
        self.assertEqual(len(listed), 1)
        entry = listed[0]
        self.assertEqual(entry["name"], self.name)
        self.assertEqual(entry["stage"], "withdrawn")
        self.assertTrue(entry["withdrawn"])
        self.assertEqual(
            entry["directory"], f"{library.WITHDRAWN_SKILL_PREFIX}{self.name}"
        )
        self.assertEqual(
            [item["name"] for item in library.list_withdrawn_skills(self.workspace)],
            [self.name],
        )

    def test_a_staged_candidate_and_a_withdrawn_parking_coexist(self) -> None:
        self._approve()
        library.withdraw_learned_skill(self.workspace, self.name)
        self._stage()
        stages = {
            item["directory"]: item["stage"]
            for item in library.list_staged_skills(self.workspace)
        }
        self.assertEqual(
            stages,
            {
                self.name: "staged",
                f"{library.WITHDRAWN_SKILL_PREFIX}{self.name}": "withdrawn",
            },
        )
        self.assertEqual(
            [item["name"] for item in library.list_withdrawn_skills(self.workspace)],
            [self.name],
        )

    def test_a_withdrawn_parking_is_inert_and_never_promotable(self) -> None:
        """Ruling 16: restorable only through a new promotion."""
        self._approve()
        library.withdraw_learned_skill(self.workspace, self.name)
        parked = f"{library.WITHDRAWN_SKILL_PREFIX}{self.name}"
        with self.assertRaises(PermissionError):
            library.promote_staged_skill(
                self.workspace, parked, expected_staged_sha256="a" * 64
            )
        with self.assertRaises(PermissionError):
            library.withdraw_learned_skill(self.workspace, parked)
        # Addressed by its real name it is simply absent: the parking is not a
        # staged candidate and cannot be mistaken for one.
        with self.assertRaises(KeyError):
            library.promote_staged_skill(
                self.workspace, self.name, expected_staged_sha256="a" * 64
            )
        with self.assertRaises(KeyError):
            library.read_staged_skill(self.name, self.workspace)

    def test_a_new_promotion_brings_the_skill_back(self) -> None:
        self._approve()
        library.withdraw_learned_skill(self.workspace, self.name)
        staged = self._stage(self._body(reuses=6), outcomes=6)
        library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        self.assertIn(
            self.name,
            {item["name"] for item in library.list_available_skills(self.workspace)},
        )

    def test_withdrawing_an_absent_document_is_a_quiet_no_op(self) -> None:
        """A withdrawal that can fail is R-1 all over again."""
        result = library.withdraw_learned_skill(self.workspace, self.name)
        self.assertFalse(result["withdrawn"])
        self.assertFalse(result["moved"])
        self.assertIsNone(result["sha256"])
        self.assertIsNone(result["path"])
        # Idempotent after a real withdrawal, too.
        self._approve()
        library.withdraw_learned_skill(self.workspace, self.name)
        again = library.withdraw_learned_skill(self.workspace, self.name)
        self.assertFalse(again["withdrawn"])

    def test_a_second_withdrawal_replaces_and_reports_what_it_displaced(self) -> None:
        """Never silent, never a failure: the caller is told the digest."""
        first = self._approve()
        library.withdraw_learned_skill(self.workspace, self.name)
        second_staged = self._stage(self._body(reuses=8), outcomes=8)
        library.promote_staged_skill(
            self.workspace, self.name,
            expected_staged_sha256=str(second_staged["sha256"]),
        )
        result = library.withdraw_learned_skill(self.workspace, self.name)
        self.assertTrue(result["withdrawn"])
        self.assertEqual(result["replaced_sha256"], first["approved_sha256"])
        self.assertEqual(result["sha256"], second_staged["sha256"])
        self.assertEqual(len(library.list_withdrawn_skills(self.workspace)), 1)

    def test_withdrawal_refuses_a_hard_linked_document(self) -> None:
        self._approve()
        os.link(self._live_file, self.workspace / "shadow.md")
        with self.assertRaises(PermissionError):
            library.withdraw_learned_skill(self.workspace, self.name)
        self.assertTrue(self._live_file.exists())

    def test_the_withdrawn_copy_is_written_before_the_live_one_is_removed(
        self,
    ) -> None:
        """A crash between the two must leave the bytes somewhere, not nowhere."""
        approved = self._approve()
        original = self._live_file.read_bytes()
        calls: list[str] = []
        real_unlink = Path.unlink

        def refuse(self_path, *args, **kwargs):
            if self_path.name == "SKILL.md" and ".jarvis-skills-staging" not in str(
                self_path
            ):
                calls.append("live-unlink")
                raise OSError("simulated crash after the copy")
            return real_unlink(self_path, *args, **kwargs)

        with patch.object(Path, "unlink", refuse):
            with self.assertRaises(OSError):
                library.withdraw_learned_skill(self.workspace, self.name)
        self.assertEqual(calls, ["live-unlink"])
        parked = (
            self.workspace / library.STAGED_SKILL_DIRECTORY
            / f"{library.WITHDRAWN_SKILL_PREFIX}{self.name}" / "SKILL.md"
        )
        self.assertTrue(parked.exists(), "the copy must exist before the removal")
        self.assertEqual(parked.read_bytes(), original)
        self.assertEqual(self._live_file.read_bytes(), original)
        del approved

    # --- the round trip ---------------------------------------------------

    def test_stage_approve_rollback_returns_the_workspace_to_its_start(self) -> None:
        """Design 3.6's rollback equivalence, at the library layer."""
        before_catalog = [
            item["name"] for item in library.list_available_skills(self.workspace)
        ]
        staged = self._stage()
        promoted = library.promote_staged_skill(
            self.workspace, self.name, expected_staged_sha256=str(staged["sha256"])
        )
        library.restore_learned_skill(
            self.workspace, self.name, promoted["prior_document"]
        )
        self.assertEqual(
            [item["name"] for item in library.list_available_skills(self.workspace)],
            before_catalog,
        )
        self.assertEqual(library.list_staged_skills(self.workspace), [])


class RandomizedLadderBatteryTests(unittest.TestCase):
    """Design 7.7 at the library layer: forty randomized steps, byte-exact.

    The store-side battery lives in ``tests/test_learning_ladder_integration.py``
    and owns the rows and the receipts.  This one owns the bytes: after every
    rollback the live document must equal, byte for byte, whatever it was
    immediately before the matching approval -- including its absence -- and
    the catalog must return exactly what it returned then.  A withdrawal is in
    the mix because R-1 was a withdrawal that left the file live.
    """

    FAMILIES = ("code_fix", "file_ops", "deep_research")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        ladder.clear_catalog_cache()
        self.addCleanup(ladder.clear_catalog_cache)

    def _live_bytes(self, name: str) -> bytes | None:
        document = (
            self.workspace / library.LEARNED_SKILL_DIRECTORY / name / "SKILL.md"
        )
        return document.read_bytes() if document.exists() else None

    def _catalog(self) -> list[str]:
        return [
            item["name"] for item in library.list_available_skills(self.workspace)
        ]

    def test_forty_randomized_steps_restore_byte_exactly(self) -> None:
        # Seed chosen so all five branches actually fire inside forty steps; a
        # script that never withdraws would not test what R-1 broke.
        rng = random.Random(20260906)
        names = {family: auto_skill_name(family) for family in self.FAMILIES}
        staged_digest: dict[str, str] = {}
        # name -> (bytes before the approval, catalog before the approval)
        approved_from: dict[str, tuple[bytes | None, list[str]]] = {}
        counter = 0
        performed = {"stage": 0, "approve": 0, "rollback": 0,
                     "withdraw": 0, "discard": 0}

        for step in range(40):
            family = rng.choice(self.FAMILIES)
            name = names[family]
            action = rng.choice(
                ("stage", "approve", "rollback", "withdraw", "discard")
            )

            if action == "stage":
                counter += 1
                body = ladder.build_staged_document(
                    family=family, reuses=counter, contexts=counter,
                    tool_names=["read_file"], oracles=["tool_success"],
                    gate=_GATE, epoch=1, monotone=True, lift_pp=1.0,
                )
                staged = library.stage_learned_skill(
                    self.workspace, name,
                    ladder.staged_skill_description(family), body,
                    family=family, verified_outcomes=counter,
                )
                staged_digest[name] = str(staged["sha256"])
                performed["stage"] += 1

            elif action == "approve":
                if name not in staged_digest:
                    continue
                before_bytes = self._live_bytes(name)
                before_catalog = self._catalog()
                result = library.promote_staged_skill(
                    self.workspace, name,
                    expected_staged_sha256=staged_digest.pop(name),
                )
                ladder.clear_catalog_cache()
                self.assertEqual(result["prior_document"], before_bytes)
                approved_from[name] = (before_bytes, before_catalog)
                performed["approve"] += 1

            elif action == "rollback":
                if name not in approved_from:
                    continue
                before_bytes, before_catalog = approved_from.pop(name)
                library.restore_learned_skill(self.workspace, name, before_bytes)
                ladder.clear_catalog_cache()
                self.assertEqual(
                    self._live_bytes(name), before_bytes,
                    f"step {step}: rollback of {name} was not byte-exact",
                )
                self.assertEqual(self._catalog(), before_catalog)
                performed["rollback"] += 1

            elif action == "withdraw":
                live = self._live_bytes(name)
                result = library.withdraw_learned_skill(self.workspace, name)
                ladder.clear_catalog_cache()
                if live is None:
                    self.assertFalse(result["withdrawn"])
                else:
                    self.assertTrue(result["withdrawn"])
                    # R-1: the file must not remain in the live root.
                    self.assertIsNone(self._live_bytes(name))
                    self.assertNotIn(name, self._catalog())
                    parked = Path(str(result["path"]))
                    self.assertEqual(parked.read_bytes(), live)
                    approved_from.pop(name, None)
                    performed["withdraw"] += 1

            else:
                if name not in staged_digest:
                    continue
                library.discard_staged_skill(self.workspace, name)
                staged_digest.pop(name)
                performed["discard"] += 1

            # Invariants that must hold after every single step.
            for other in names.values():
                staged_entries = [
                    item for item in library.list_staged_skills(self.workspace)
                    if item["name"] == other and not item["withdrawn"]
                ]
                self.assertLessEqual(len(staged_entries), 1)
                # A staged or withdrawn document is never in the catalog.
                for entry in library.list_staged_skills(self.workspace):
                    if entry["withdrawn"]:
                        self.assertNotIn(
                            entry["directory"], self._catalog()
                        )
            self.assertFalse(
                (self.workspace / library.STAGED_SKILL_DIRECTORY).samefile(
                    self.workspace / library.LEARNED_SKILL_DIRECTORY
                )
                if (self.workspace / library.LEARNED_SKILL_DIRECTORY).exists()
                else False
            )

        # The script must actually have exercised every branch.
        for action, count in performed.items():
            self.assertGreater(count, 0, f"{action} never ran")

    def tearDown(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        for root in (repository, Path.cwd()):
            for directory in (
                library.LEARNED_SKILL_DIRECTORY, library.STAGED_SKILL_DIRECTORY
            ):
                self.assertFalse((root / directory).exists())


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
