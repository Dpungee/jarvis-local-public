"""`jarvis ladder` and `/ladder` (VTMF M4 design 6.3, 6.2 item 4, 7.11).

The operator surfaces over the calibration ledger and the promotion record.
Three of them -- `ladder list`, `ladder show` and `/ladder` -- are the ONLY
places the confirmation code of a staged promotion is ever printed, and this
file is where that boundary is pinned in both directions: shown for a staged
row, absent for every other stage.

The subcommands that touch the store's schema-49 methods are held back until
those land; the parser, the misuse exit codes, the workspace derivation and
the rendering helpers are exercised unconditionally.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cli, learning_ladder
from jarvis.agent import AgentResult
from jarvis.config import Config
from jarvis.memory import Memory


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)

LADDER_STORE_READY = hasattr(Memory, "grandfather_ladder")
#: A stand-in for secrets.token_urlsafe(12).
_CODE = "Clb-s_cqN7jBq-NA"

#: Design 6.3's ten plus `run`, the consolidation pass the correctness review
#: found had no runtime driver at all (HIGH-2).  A subcommand added without a
#: design change fails here, and so does one quietly removed.
EXPECTED_SUBCOMMANDS = {
    "status", "list", "show", "stage", "approve", "rollback", "discard",
    "seal", "verify", "run", "ledger",
}


def _ladder_parser_choices() -> set[str]:
    parser = cli._parser()
    for action in parser._subparsers._group_actions:  # noqa: SLF001 - argparse
        ladder = action.choices.get("ladder")
        if ladder is not None:
            for sub in ladder._subparsers._group_actions:  # noqa: SLF001
                return set(sub.choices)
    raise AssertionError("the ladder subparser is not registered")


class LadderParserTests(unittest.TestCase):
    def test_the_registered_subcommands_are_exactly_the_expected_set(self) -> None:
        self.assertEqual(_ladder_parser_choices(), EXPECTED_SUBCOMMANDS)

    def test_no_subcommand_takes_a_workspace_argument(self) -> None:
        """Design 6.3 / M-10: the workspace is DERIVED from the row's project.

        A `--workspace` flag would let an operator approve promotion #7 of
        project 3 into the default workspace, writing the document into the
        wrong `.jarvis-skills`.
        """
        parser = cli._parser()
        for action in parser._subparsers._group_actions:  # noqa: SLF001
            ladder = action.choices.get("ladder")
            if ladder is None:
                continue
            for sub in ladder._subparsers._group_actions:  # noqa: SLF001
                for name, subparser in sub.choices.items():
                    with self.subTest(subcommand=name):
                        options = {
                            option
                            for entry in subparser._actions  # noqa: SLF001
                            for option in entry.option_strings
                        }
                        self.assertNotIn("--workspace", options)

    def test_seal_takes_no_boundary(self) -> None:
        """Design 2.2 / M-7: epochs are exactly LADDER_EPOCH_SIZE rows in id
        order, so `--all` is a catch-up and never a cut.  An `--after`,
        `--since` or `--boundary` flag would make the M-7 attack expressible
        ("seal 20 failures and 180 successes as one epoch")."""
        parser = cli._parser()
        for action in parser._subparsers._group_actions:  # noqa: SLF001
            ladder = action.choices.get("ladder")
            if ladder is None:
                continue
            for sub in ladder._subparsers._group_actions:  # noqa: SLF001
                options = {
                    option
                    for entry in sub.choices["seal"]._actions  # noqa: SLF001
                    for option in entry.option_strings
                }
                self.assertEqual(options - {"-h", "--help"}, {"--family", "--all", "--json"})

    def test_rollback_accepts_no_confirmation_code(self) -> None:
        """Design 3.6 and the boss ruling: a rollback needs none.  It only ever
        restores bytes the ladder itself replaced, so requiring a code would
        make the safe direction the harder one."""
        parser = cli._parser()
        for action in parser._subparsers._group_actions:  # noqa: SLF001
            ladder = action.choices.get("ladder")
            if ladder is None:
                continue
            for sub in ladder._subparsers._group_actions:  # noqa: SLF001
                for name in ("rollback", "discard"):
                    options = {
                        option
                        for entry in sub.choices[name]._actions  # noqa: SLF001
                        for option in entry.option_strings
                    }
                    with self.subTest(subcommand=name):
                        self.assertNotIn("--token", options)
                approve = {
                    option
                    for entry in sub.choices["approve"]._actions  # noqa: SLF001
                    for option in entry.option_strings
                }
                self.assertIn("--token", approve)


class LadderRenderingTests(unittest.TestCase):
    """The two boundaries a renderer can get wrong: showing a spent code, and
    telling the operator "regressed" when nothing is actually refused."""

    def test_the_code_is_printed_only_while_the_row_is_staged(self) -> None:
        for stage, expected in (
            ("staged", True),
            ("approved", False),
            ("unapproved_legacy", False),
            ("rolled_back", False),
            ("withdrawn", False),
            ("discarded", False),
        ):
            row = {
                "id": 12,
                "family": "code_fix",
                "stage": stage,
                "skill_name": "learned-code-fix",
                "approval_token": _CODE,
                "staged_sha256": "a" * 64,
            }
            with self.subTest(stage=stage):
                self.assertEqual(bool(cli._ladder_code_of(row)), expected)
                line = cli._ladder_row_line(row)
                self.assertEqual(_CODE in line, expected)
                self.assertIn("#12", line)
                self.assertIn("code_fix", line)

    def test_the_monotone_line_separates_looked_bad_from_refuses(self) -> None:
        """ladder-core's runtime rule needs LADDER_REGRESSION_STREAK consecutive
        regressed epochs to refuse.  An operator shown one word for both states,
        who then cannot stage, reasonably concludes the surface is lying."""
        needed = int(getattr(learning_ladder, "LADDER_REGRESSION_STREAK", 2))
        clean = cli._ladder_monotone_line({
            "monotone": True, "newest_regressed": False,
            "currently_regressed": False, "consecutive_regressed": 0,
        })
        self.assertEqual(clean, "monotone")

        warning = cli._ladder_monotone_line({
            "monotone": False, "newest_regressed": True,
            "currently_regressed": False, "consecutive_regressed": 1,
        })
        self.assertIn("last epoch regressed", warning)
        self.assertIn(f"1 of {needed}", warning)
        self.assertNotIn("refused", warning)

        refusing = cli._ladder_monotone_line({
            "monotone": False, "newest_regressed": True,
            "currently_regressed": True, "consecutive_regressed": 2,
        })
        self.assertIn("2 consecutive epochs", refusing)
        self.assertIn("staging and approval refused", refusing)

        historic = cli._ladder_monotone_line({
            "monotone": False, "newest_regressed": False,
            "currently_regressed": False, "consecutive_regressed": 0,
        })
        self.assertIn("monotone now", historic)


class LadderMisuseTests(unittest.TestCase):
    """Exit codes for flag misuse, on the `graph rebuild` discipline: 2 for
    misuse, 1 for a refusal, 0 for a clean run."""

    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"ladder-cli-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        workspace = self.test_dir / "workspace"
        data_dir = self.test_dir / "data"
        workspace.mkdir()
        data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=workspace,
            data_dir=data_dir,
            vault_dir=None,
            ollama_preload=False,
            memory_embeddings="disabled",
        )
        # Build the store once so the CLI's own open finds a migrated file.
        Memory(data_dir / "jarvis.db").close()

    def tearDown(self) -> None:
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def _run(self, **kwargs: object) -> tuple[int, str]:
        args = argparse.Namespace(**kwargs)
        output = io.StringIO()
        with patch.object(cli.Config, "load", return_value=self.config), \
                redirect_stdout(output):
            code = cli._run_ladder(args)
        return code, output.getvalue()

    def test_yes_without_apply_is_misuse(self) -> None:
        code, rendered = self._run(
            ladder_command="verify", apply=False, yes=True, plan=None,
            project=None, json=False,
        )
        self.assertEqual(code, 2)
        self.assertIn("--yes requires --apply", rendered)

    def test_plan_without_apply_and_yes_is_misuse(self) -> None:
        code, rendered = self._run(
            ladder_command="verify", apply=False, yes=False, plan="abc123abc123",
            project=None, json=False,
        )
        self.assertEqual(code, 2)
        self.assertIn("--plan requires --apply --yes", rendered)

    def test_seal_needs_exactly_one_of_family_or_all(self) -> None:
        for family, everything in ((None, False), ("code_fix", True)):
            with self.subTest(family=family, all=everything):
                code, rendered = self._run(
                    ladder_command="seal", family=family, all=everything, json=False
                )
                self.assertEqual(code, 2)
                self.assertIn("exactly one of --family F or --all", rendered)

    def test_a_write_subcommand_refuses_without_yes(self) -> None:
        for command, extra in (
            ("stage", {"family": "code_fix", "project": 1}),
            ("approve", {"id": 1, "token": _CODE}),
            ("rollback", {"id": 1}),
            ("discard", {"id": 1}),
        ):
            with self.subTest(command=command):
                code, rendered = self._run(
                    ladder_command=command, yes=False, json=False, **extra
                )
                self.assertEqual(code, 2)
                self.assertIn("--yes", rendered)

    def test_staging_an_excluded_family_is_refused_with_its_reason(self) -> None:
        """Design 3.0 / M-2: `conversation` predictions carry evidence_ok NULL,
        so a conversation-family promotion would rest on no verification at
        all.  The refusal names which of the two reasons it is."""
        code, rendered = self._run(
            ladder_command="stage", family="conversation", project=1,
            yes=True, json=False,
        )
        self.assertEqual(code, 1)
        self.assertIn("family_excluded", rendered)

        code, rendered = self._run(
            ladder_command="stage", family="not_a_family", project=1,
            yes=True, json=False,
        )
        self.assertEqual(code, 1)
        self.assertIn("family_unsupported", rendered)

    def test_a_missing_project_workspace_is_reported_not_substituted(self) -> None:
        """S-8: a vanished project directory is `workspace_unavailable`, never
        silently resolved to some other workspace."""
        with patch.object(
            cli, "_ladder_workspace",
            side_effect=cli.LadderWorkspaceUnavailable("workspace_unavailable"),
        ):
            code, rendered = self._run(
                ladder_command="stage", family="code_fix", project=7,
                yes=True, json=False,
            )
        self.assertEqual(code, 1)
        self.assertIn("no reachable workspace", rendered)

    def test_the_json_surface_is_valid_json(self) -> None:
        for command, extra in (
            ("list", {"project": None, "stage": None}),
            ("ledger", {"family": None}),
        ):
            with self.subTest(command=command):
                code, rendered = self._run(
                    ladder_command=command, json=True, **extra
                )
                self.assertEqual(code, 0)
                json.loads(rendered)


@unittest.skipUnless(
    LADDER_STORE_READY, "schema-49 promotion methods have not landed yet"
)
class LadderStoreSurfaceTests(unittest.TestCase):
    """The subcommands that read the schema-49 record."""

    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"ladder-store-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        workspace = self.test_dir / "workspace"
        data_dir = self.test_dir / "data"
        workspace.mkdir()
        data_dir.mkdir()
        self.workspace = workspace
        self.config = replace(
            Config.load(),
            workspace=workspace,
            data_dir=data_dir,
            vault_dir=None,
            ollama_preload=False,
            memory_embeddings="disabled",
        )
        self.memory = Memory(data_dir / "jarvis.db")

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def _run(self, **kwargs: object) -> tuple[int, str]:
        args = argparse.Namespace(**kwargs)
        output = io.StringIO()
        with patch.object(cli.Config, "load", return_value=self.config), \
                redirect_stdout(output):
            code = cli._run_ladder(args)
        return code, output.getvalue()

    def test_status_on_an_empty_store_reports_every_family(self) -> None:
        code, rendered = self._run(
            ladder_command="status", project=None, json=False
        )
        self.assertEqual(code, 0)
        for family in sorted(learning_ladder.LADDER_FAMILIES):
            with self.subTest(family=family):
                self.assertIn(family, rendered)
        self.assertIn("Unverified promotions: 0", rendered)
        # No legacy line when there is nothing to say (design ruling 2).
        self.assertNotIn("legacy skills live without approval", rendered)

    def test_status_json_carries_no_confirmation_code_or_proof_digest(self) -> None:
        """Design 6.4 / Q-11: publishing a digest beside a promotion id is what
        made draft 1's token derivable, and the code is operator-only."""
        code, rendered = self._run(
            ladder_command="status", project=None, json=True
        )
        self.assertEqual(code, 0)
        payload = json.loads(rendered)
        blob = json.dumps(payload)
        for forbidden in ("approval_token", "proof_sha256", "coverage_digest"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_show_reports_the_usage_threshold_honestly(self) -> None:
        """Design 3.3 / M-1, in the operator's own words: LADDER_MIN_VERIFIED_
        REUSES is a usage threshold, not a significance test, and an
        application row is filed when the lesson MATCHED, not when it was used.
        """
        code, rendered = self._run(ladder_command="show", id=999999, json=False)
        self.assertEqual(code, 1)
        self.assertIn("No skill promotion #999999 exists", rendered)

    # ---------------------------------------------------------------- seeding
    FAMILY = "file_ops"

    def _resolved_outcome(self, *, complete: bool = True) -> tuple[int, int]:
        conversation_id = self.memory.new_conversation("outcome", project_id=1)
        prediction = self.memory.record_prediction(
            family=self.FAMILY, profile="cli-test", model="deterministic",
            predicted_success=0.8, predicted_steps=2,
            predicted_verification="tool_success", basis="prior",
            origin="interactive", conversation_id=conversation_id,
        )
        self.memory.resolve_prediction(
            prediction,
            actual_status="complete" if complete else "failed",
            actual_steps=2, evidence_ok=True,
            failure_class=None if complete else "unknown",
            primary_tool="list_files",
        )
        return conversation_id, prediction

    def _seed_to_a_staged_promotion(self) -> None:
        """The whole proof, through public writers only.

        The effectiveness clause is computed from SEALED epochs, so the
        reuses have to fall inside one -- seeding them last would leave them
        in the unsealed tail and staging would refuse
        `insufficient_effectiveness`.
        """
        for index in range(18):
            self._resolved_outcome(complete=bool(index % 6))
        conversation_id, prediction = self._resolved_outcome()
        reflection = self.memory.record_reflection(
            status="complete", summary="CLI fixture outcome.",
            improvements="Prefer a lowercase hyphenated file naming convention.",
            conversation_id=conversation_id, prediction_id=prediction, tool_calls=2,
        )
        row = self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='lesson' AND reflection_id=?",
            (reflection,),
        ).fetchone()
        lesson_id = int(row["id"])
        for _index in range(14):
            conversation = self.memory.new_conversation("reuse", project_id=1)
            reuse = self.memory.record_prediction(
                family=self.FAMILY, profile="cli-test", model="deterministic",
                predicted_success=0.8, predicted_steps=2,
                predicted_verification="tool_success", basis="prior",
                origin="interactive", conversation_id=conversation,
            )
            self.memory.record_lesson_applications(reuse, self.FAMILY, [lesson_id])
            self.memory.resolve_prediction(
                reuse, actual_status="complete", actual_steps=2,
                evidence_ok=True, primary_tool="list_files",
            )
        for index in range(28):
            self._resolved_outcome(complete=bool(index % 6))

    # ------------------------------------------------------------------ tests
    def test_seal_ledger_status_and_the_monotone_line_on_a_real_store(self) -> None:
        self._seed_to_a_staged_promotion()

        code, rendered = self._run(
            ladder_command="seal", family=self.FAMILY, all=False, json=False
        )
        self.assertEqual(code, 0)
        self.assertIn(f"Sealed {self.FAMILY} epoch", rendered)

        code, rendered = self._run(ladder_command="ledger", family=None, json=False)
        self.assertEqual(code, 0)
        self.assertIn("unverified_at_seal=0", rendered)

        code, rendered = self._run(ladder_command="status", project=None, json=False)
        self.assertEqual(code, 0)
        self.assertIn("gate open", rendered)
        self.assertIn("monotone", rendered)
        self.assertIn("observational, not randomized", rendered)
        self.assertIn("Unverified promotions: 0", rendered)

        code, rendered = self._run(ladder_command="seal", family=None, all=True, json=True)
        self.assertEqual(code, 0)
        json.loads(rendered)

    def test_stage_approve_and_rollback_end_to_end_through_the_cli(self) -> None:
        """The three write subcommands, and the code boundary around them."""
        self._seed_to_a_staged_promotion()
        self._run(ladder_command="seal", family=self.FAMILY, all=False, json=False)

        code, rendered = self._run(
            ladder_command="stage", family=self.FAMILY, project=1,
            yes=True, json=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("Staged skill promotion #", rendered)
        self.assertIn("confirmation code:", rendered)
        confirmation = rendered.split("confirmation code:")[1].split()[0]
        self.assertEqual(len(confirmation), 16)
        promotion_id = int(rendered.split("#")[1].split()[0].rstrip("."))

        # list and show print it while the row is staged; status never does.
        code, listed = self._run(ladder_command="list", project=None, stage=None, json=False)
        self.assertEqual(code, 0)
        self.assertIn(confirmation, listed)
        code, shown = self._run(ladder_command="show", id=promotion_id, json=False)
        self.assertEqual(code, 0)
        self.assertIn(confirmation, shown)
        self.assertIn("usage threshold, not a significance test", shown)
        code, status = self._run(ladder_command="status", project=None, json=False)
        self.assertNotIn(confirmation, status)

        # a wrong code refuses, exactly, and does not burn the real one
        code, rendered = self._run(
            ladder_command="approve", id=promotion_id, token=_CODE,
            yes=True, json=False,
        )
        self.assertEqual(code, 1)
        self.assertIn("does not match the staged promotion", rendered)

        code, rendered = self._run(
            ladder_command="approve", id=promotion_id, token=confirmation,
            yes=True, json=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(f"Approved skill promotion #{promotion_id}", rendered)
        self.assertTrue(any((self.workspace / ".jarvis-skills").rglob("SKILL.md")))

        # single use, and the code is gone from every surface now
        code, rendered = self._run(
            ladder_command="approve", id=promotion_id, token=confirmation,
            yes=True, json=False,
        )
        self.assertEqual(code, 1)
        self.assertIn("No staged skill promotion matches", rendered)
        code, listed = self._run(ladder_command="list", project=None, stage=None, json=False)
        self.assertNotIn(confirmation, listed)
        code, shown = self._run(ladder_command="show", id=promotion_id, json=True)
        self.assertIsNone(json.loads(shown)["approval_token"])

        code, rendered = self._run(
            ladder_command="rollback", id=promotion_id, yes=True, json=False
        )
        self.assertEqual(code, 0)
        self.assertIn(f"Rolled back skill promotion #{promotion_id}", rendered)
        self.assertFalse(any((self.workspace / ".jarvis-skills").rglob("SKILL.md")))

    def test_discard_throws_a_staged_document_away(self) -> None:
        self._seed_to_a_staged_promotion()
        self._run(ladder_command="seal", family=self.FAMILY, all=False, json=False)
        code, rendered = self._run(
            ladder_command="stage", family=self.FAMILY, project=1,
            yes=True, json=False,
        )
        self.assertEqual(code, 0)
        promotion_id = int(rendered.split("#")[1].split()[0].rstrip("."))

        code, rendered = self._run(
            ladder_command="discard", id=promotion_id, yes=True, json=False
        )
        self.assertEqual(code, 0)
        self.assertIn("Discarded staged skill promotion", rendered)
        self.assertFalse(any((self.workspace / ".jarvis-skills").rglob("SKILL.md")))

    def test_the_chat_surface_shows_the_code_and_the_legacy_count(self) -> None:
        """`/ladder`, rendered before any model call, like `/facts`."""
        from jarvis.skill_evolution import distill_verified_skill

        distill_verified_skill(
            self.workspace, family=self.FAMILY,
            successful_tools={"list_files"}, verification="tool_success",
        )
        self.memory.grandfather_ladder(self.workspace, project_id=1)
        output = io.StringIO()
        with redirect_stdout(output):
            cli._display_ladder(self.config, self.memory, 1)
        rendered = output.getvalue()
        self.assertIn("unapproved_legacy", rendered)
        self.assertIn("1 legacy skills live without approval", rendered)

    def test_every_subcommand_answers_in_json_and_never_leaks_the_code(
        self,
    ) -> None:
        """The `--json` surface of all ten, on a store that has real rows.

        An operator scripting the ladder reads these; `status` must still omit
        the confirmation code and every digest, while `list` and `show` carry
        the code for a staged row and nothing for any other stage.
        """
        self._seed_to_a_staged_promotion()
        self._run(ladder_command="seal", family=self.FAMILY, all=False, json=False)
        code, rendered = self._run(
            ladder_command="stage", family=self.FAMILY, project=1, yes=True, json=True
        )
        self.assertEqual(code, 0)
        staged = json.loads(rendered)
        promotion_id = int(staged["promotion_id"])
        confirmation = str(staged["approval_token"])

        for command, extra in (
            ("status", {"project": None}),
            ("status", {"project": 1}),
            ("list", {"project": 1, "stage": "staged"}),
            ("list", {"project": None, "stage": "approved"}),
            ("show", {"id": promotion_id}),
            ("ledger", {"family": self.FAMILY}),
            ("ledger", {"family": None}),
        ):
            with self.subTest(command=command, extra=extra):
                code, rendered = self._run(
                    ladder_command=command, json=True, **extra
                )
                self.assertEqual(code, 0)
                payload = json.loads(rendered)
                blob = json.dumps(payload)
                if command == "status":
                    for forbidden in (
                        confirmation, "proof_sha256", "coverage_digest",
                    ):
                        self.assertNotIn(forbidden, blob)

        # approve and rollback in --json, then discard a fresh staging
        code, rendered = self._run(
            ladder_command="approve", id=promotion_id, token=confirmation,
            yes=True, json=True,
        )
        self.assertEqual(code, 0)
        json.loads(rendered)
        code, rendered = self._run(
            ladder_command="approve", id=promotion_id, token=confirmation,
            yes=True, json=True,
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(rendered)["refusal"], "not_staged")
        code, rendered = self._run(
            ladder_command="rollback", id=promotion_id, yes=True, json=True
        )
        self.assertEqual(code, 0)
        json.loads(rendered)

    def test_status_prints_the_legacy_line_when_there_is_one(self) -> None:
        """Design ruling 2 / S-4: their own bucket, in the same words the
        Presence panel and `jarvis doctor` use."""
        from jarvis.skill_evolution import distill_verified_skill

        distill_verified_skill(
            self.workspace, family=self.FAMILY,
            successful_tools={"list_files"}, verification="tool_success",
        )
        self.memory.grandfather_ladder(self.workspace, project_id=1)

        code, rendered = self._run(ladder_command="status", project=None, json=False)

        self.assertEqual(code, 0)
        self.assertIn("1 legacy skills live without approval", rendered)
        self.assertIn("approve or roll back each one", rendered)
        # A legacy document is never counted as an unverified promotion.
        self.assertIn("Unverified promotions: 0", rendered)

    def test_staging_refuses_with_its_reason_on_an_uncalibrated_family(
        self,
    ) -> None:
        for json_output in (False, True):
            with self.subTest(json=json_output):
                code, rendered = self._run(
                    ladder_command="stage", family="code_fix", project=1,
                    yes=True, json=json_output,
                )
                self.assertEqual(code, 1)
                if json_output:
                    self.assertEqual(json.loads(rendered)["reason"], "gate_closed")
                else:
                    self.assertIn("gate_closed", rendered)

    def _verify(self, **kwargs: object) -> tuple[int, str]:
        base = {
            "ladder_command": "verify", "project": None, "apply": False,
            "yes": False, "plan": None, "json": False,
        }
        base.update(kwargs)
        return self._run(**base)

    def _plan_token(self) -> str:
        """The token the dry run prints, which --apply now requires (R-11)."""
        _code, rendered = self._verify(json=True)
        return str(json.loads(rendered)["plan_token"])

    def _verify_apply(self, **kwargs: object) -> tuple[int, str]:
        """Apply the plan as an operator would: read it, then confirm it."""
        kwargs.setdefault("plan", self._plan_token())
        return self._verify(apply=True, yes=True, **kwargs)

    def test_verify_is_clean_on_a_store_with_nothing_to_reconcile(self) -> None:
        code, rendered = self._verify()
        self.assertEqual(code, 0)
        self.assertIn("nothing to reconcile", rendered)
        # A clean run appends no event at all (the M3 graph-rebuild precedent).
        events = self.memory.db.execute(
            "SELECT COUNT(*) AS n FROM memory_spine_events WHERE kind LIKE 'ladder.%'"
        ).fetchone()
        self.assertEqual(int(events["n"]), 0)

        code, rendered = self._verify_apply()
        self.assertEqual(code, 0)
        self.assertIn("Nothing to apply", rendered)

    def test_verify_is_the_grandfather_pass_with_the_plan_token_discipline(
        self,
    ) -> None:
        """Design 6.3, on the exact `graph rebuild` contract.

        A dry run that WOULD change something exits 2 and prints the token an
        operator then confirms; `--apply --yes --plan TOKEN` refuses
        `stale_plan` when the store moved underneath the plan they read.
        """
        from jarvis.skill_evolution import distill_verified_skill

        distill_verified_skill(
            self.workspace, family=self.FAMILY,
            successful_tools={"list_files"}, verification="tool_success",
        )

        code, rendered = self._verify()
        self.assertEqual(code, 2)
        self.assertIn("grandfather", rendered)
        self.assertIn("plan token:", rendered)
        self.assertIn("--apply --yes --plan", rendered)
        token = rendered.split("plan token:")[1].split()[0]
        self.assertEqual(len(token), 12)
        # A dry run changes nothing.
        self.assertEqual(self.memory.ladder_promotions(project_id=1), [])

        # A token from a plan the store has since moved past is refused.
        code, refused = self._verify(
            apply=True, yes=True, plan="0" * 12
        )
        self.assertEqual(code, 1)
        self.assertIn("stale_plan", refused)
        self.assertEqual(self.memory.ladder_promotions(project_id=1), [])

        code, applied = self._verify(apply=True, yes=True, plan=token)
        self.assertEqual(code, 0)
        self.assertIn("applied", applied.casefold())
        rows = [
            dict(row) for row in self.memory.ladder_promotions(project_id=1)
        ]
        self.assertEqual([row["stage"] for row in rows], ["unapproved_legacy"])
        receipts = self.memory.db.execute(
            "SELECT COUNT(*) AS n FROM memory_spine_events "
            "WHERE kind='ladder.grandfathered'"
        ).fetchone()
        self.assertEqual(int(receipts["n"]), 1)

        # Idempotent: a second run has nothing to do and appends nothing.
        code, again = self._verify()
        self.assertEqual(code, 0)
        self.assertIn("nothing to reconcile", again)
        still = self.memory.db.execute(
            "SELECT COUNT(*) AS n FROM memory_spine_events "
            "WHERE kind='ladder.grandfathered'"
        ).fetchone()
        self.assertEqual(int(still["n"]), 1)

    def test_verify_withdraws_a_live_document_an_operator_edited(self) -> None:
        """The record is authoritative and the filesystem is reconciled TO it.

        A live document whose digest has drifted is **withdrawn**, never
        restored: silently overwriting an operator's own edit would be the
        worse failure of the two.
        """
        self._seed_to_a_staged_promotion()
        self._run(ladder_command="seal", family=self.FAMILY, all=False, json=False)
        code, rendered = self._run(
            ladder_command="stage", family=self.FAMILY, project=1, yes=True, json=True
        )
        staged = json.loads(rendered)
        self._run(
            ladder_command="approve", id=int(staged["promotion_id"]),
            token=str(staged["approval_token"]), yes=True, json=False,
        )
        live = next((self.workspace / ".jarvis-skills").rglob("SKILL.md"))
        live.write_text(
            live.read_text(encoding="utf-8") + "\nan operator edit\n",
            encoding="utf-8",
        )

        code, rendered = self._verify()
        self.assertEqual(code, 2)
        self.assertIn("withdraw", rendered.casefold())

        code, rendered = self._verify_apply()
        self.assertEqual(code, 0)
        stages = {
            str(row["stage"])
            for row in self.memory.ladder_promotions(project_id=1)
        }
        self.assertIn("withdrawn", stages)
        # Ruling 16: the document is PARKED, not deleted, so the operator's own
        # bytes survive -- they just stop being offered to the model.
        self.assertFalse(live.exists())
        parked = list(
            (self.workspace / ".jarvis-skills-staging").glob("withdrawn-*/SKILL.md")
        )
        self.assertEqual(len(parked), 1)
        self.assertIn("an operator edit", parked[0].read_text(encoding="utf-8"))

    def test_verify_parks_an_orphan_out_of_the_live_root(self) -> None:
        """Design ruling 16: a withdrawal never deletes and never leaves an
        uncountable live file.

        The earlier contract left an `orphan_document` in place unless an
        opt-in flag was given, which meant a live document the ladder could
        not account for stayed in front of the model indefinitely and counted
        against `unverified_at_seal` forever.  It is now MOVED into the
        staging root under a `withdrawn-` prefix, unconditionally: out of the
        catalog, out of reach of the file tools, and still on disk so nothing
        an operator wrote is destroyed.
        """
        from jarvis.skill_evolution import distill_verified_skill
        from jarvis.skill_library import list_available_skills

        distill_verified_skill(
            self.workspace, family=self.FAMILY,
            successful_tools={"list_files"}, verification="tool_success",
        )
        self.memory.grandfather_ladder(self.workspace, project_id=1)
        row = self.memory.ladder_promotions(project_id=1)[0]
        self.memory.rollback_ladder_promotion(
            int(row["id"]), workspace=self.workspace,
            actor="operator", permission="operator:cli",
        )
        # A live document whose only row is terminal: an orphan.
        distill_verified_skill(
            self.workspace, family=self.FAMILY,
            successful_tools={"list_files"}, verification="tool_success",
        )
        live_before = next((self.workspace / ".jarvis-skills").rglob("SKILL.md"))
        bytes_before = live_before.read_bytes()

        code, rendered = self._verify()
        self.assertEqual(code, 2)
        self.assertIn("orphan_document", rendered)

        code, applied = self._verify_apply()
        self.assertEqual(code, 0)
        self.assertIn("1 action(s) performed", applied)

        # Out of the live root, out of the catalog, still on disk.
        self.assertFalse(list((self.workspace / ".jarvis-skills").rglob("SKILL.md")))
        parked = list(
            (self.workspace / ".jarvis-skills-staging").glob("withdrawn-*/SKILL.md")
        )
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0].read_bytes(), bytes_before)
        self.assertNotIn(
            f"learned-{self.FAMILY.replace('_', '-')}",
            [item["name"] for item in list_available_skills(self.workspace)],
        )
        # And the model's file tools cannot reach where it was parked.
        from jarvis.tools import ToolBox

        toolbox = ToolBox(self.config, self.memory)
        with self.assertRaises(PermissionError):
            toolbox.read_file(
                str(parked[0].relative_to(self.workspace)).replace("\\", "/")
            )

    def test_no_purge_orphans_flag_exists(self) -> None:
        """The store parks an orphan unconditionally and ignores the flag.

        An orphaned live document is moved to the staging root under a
        `withdrawn-` prefix -- never deleted, never left live -- so a CLI flag
        promising deletion would be a lie about what the tool does.
        """
        parser = cli._parser()
        for action in parser._subparsers._group_actions:  # noqa: SLF001
            ladder = action.choices.get("ladder")
            if ladder is None:
                continue
            for sub in ladder._subparsers._group_actions:  # noqa: SLF001
                options = {
                    option
                    for entry in sub.choices["verify"]._actions  # noqa: SLF001
                    for option in entry.option_strings
                }
                self.assertNotIn("--purge-orphans", options)

    def test_verify_withdraws_a_staged_row_whose_file_vanished(self) -> None:
        """`staged_file_missing` lands the row at `withdrawn`, not `discarded`.

        A discard is something an operator chose; this is the runtime failing
        closed on a file that went away underneath it, and the two must not be
        recorded as the same act.
        """
        self._seed_to_a_staged_promotion()
        self._run(ladder_command="seal", family=self.FAMILY, all=False, json=False)
        code, rendered = self._run(
            ladder_command="stage", family=self.FAMILY, project=1, yes=True, json=True
        )
        self.assertEqual(code, 0)
        promotion_id = int(json.loads(rendered)["promotion_id"])
        staged_file = next(
            (self.workspace / ".jarvis-skills-staging").rglob("SKILL.md")
        )
        staged_file.unlink()

        code, plan = self._verify()
        self.assertEqual(code, 2)
        self.assertIn("staged_file_missing", plan)

        code, applied = self._verify_apply()
        self.assertEqual(code, 0)
        row = dict(self.memory.ladder_promotion(promotion_id))
        self.assertEqual(row["stage"], "withdrawn")
        self.assertNotEqual(row["stage"], "discarded")
        self.assertEqual(row["stage_reason"], "staged_file_missing")

    def test_verify_answers_in_json_at_every_stage(self) -> None:
        from jarvis.skill_evolution import distill_verified_skill

        distill_verified_skill(
            self.workspace, family=self.FAMILY,
            successful_tools={"list_files"}, verification="tool_success",
        )
        code, rendered = self._verify(json=True)
        self.assertEqual(code, 2)
        plan = json.loads(rendered)
        self.assertTrue(plan["actions"])
        self.assertEqual(len(str(plan["plan_token"])), 12)

        code, rendered = self._verify(
            apply=True, yes=True, plan="0" * 12, json=True
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(rendered)["refusal"], "stale_plan")

        code, rendered = self._verify_apply(json=True)
        self.assertEqual(code, 0)
        json.loads(rendered)

        code, rendered = self._verify(json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(rendered)["actions"], [])

    def test_verify_reports_a_vanished_workspace_rather_than_substituting(
        self,
    ) -> None:
        with patch.object(
            cli, "_ladder_workspace",
            side_effect=cli.LadderWorkspaceUnavailable("workspace_unavailable"),
        ):
            code, rendered = self._verify()
            self.assertEqual(code, 1)
            self.assertIn("workspace_unavailable", rendered)
            code, rendered = self._verify(json=True)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(rendered)["refusal"], "workspace_unavailable")

    def test_a_vanished_project_workspace_is_reported_everywhere(self) -> None:
        """S-8: `workspace_unavailable`, never a substituted workspace.

        Approving promotion #7 of a project whose directory has gone must not
        fall back to the default workspace and write the document into the
        wrong `.jarvis-skills`.
        """
        self._seed_to_a_staged_promotion()
        self._run(ladder_command="seal", family=self.FAMILY, all=False, json=False)
        code, rendered = self._run(
            ladder_command="stage", family=self.FAMILY, project=1, yes=True, json=True
        )
        promotion_id = int(json.loads(rendered)["promotion_id"])

        with patch.object(
            cli, "_ladder_workspace",
            side_effect=cli.LadderWorkspaceUnavailable("workspace_unavailable"),
        ):
            for command, extra in (
                ("approve", {"token": _CODE}),
                ("rollback", {}),
                ("discard", {}),
            ):
                with self.subTest(command=command):
                    code, rendered = self._run(
                        ladder_command=command, id=promotion_id, yes=True,
                        json=False, **extra
                    )
                    self.assertEqual(code, 1)
                    self.assertIn("belongs to another project", rendered)
            # status degrades to a note rather than raising.
            code, rendered = self._run(
                ladder_command="status", project=None, json=False
            )
            self.assertEqual(code, 0)
            self.assertIn("workspace is unavailable", rendered)

    def test_the_workspace_derivation_refuses_an_unknown_project(self) -> None:
        with self.assertRaises(cli.LadderWorkspaceUnavailable):
            cli._ladder_workspace(self.config, self.memory, 987654)

    def test_a_missing_promotion_is_reported_not_raised(self) -> None:
        for command, extra in (
            ("approve", {"token": _CODE}),
            ("rollback", {}),
            ("discard", {}),
        ):
            with self.subTest(command=command):
                code, rendered = self._run(
                    ladder_command=command, id=4242, yes=True, json=False, **extra
                )
                self.assertEqual(code, 1)
                # A fixed receipt from the shared table, never a traceback.
                # The approval table's `missing` wording deliberately does not
                # name the id -- it must not confirm that #4242 exists.
                self.assertIn("nothing changed.", rendered)
                self.assertNotIn("Traceback", rendered)


class LadderConsolidationPassTests(unittest.TestCase):
    """Correctness review HIGH-2: the ladder needs a runtime driver.

    With the ungoverned distiller removed, `seal_calibration_epoch` and
    `stage_ladder_promotion` each had exactly one caller -- the CLI -- so in
    the product nothing ever sealed an epoch or staged a candidate.  The whole
    mechanism was inert.  These cover the driver and its worker call site.
    """

    FAMILY = "file_ops"

    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"ladder-pass-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            vault_dir=None,
            ollama_preload=False,
            memory_embeddings="disabled",
        )
        self.memory = Memory(self.data_dir / "jarvis.db")

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def _resolved_outcome(self, *, complete: bool = True) -> tuple[int, int]:
        conversation_id = self.memory.new_conversation("outcome", project_id=1)
        prediction = self.memory.record_prediction(
            family=self.FAMILY, profile="pass-test", model="deterministic",
            predicted_success=0.8, predicted_steps=2,
            predicted_verification="tool_success", basis="prior",
            origin="interactive", conversation_id=conversation_id,
        )
        self.memory.resolve_prediction(
            prediction,
            actual_status="complete" if complete else "failed",
            actual_steps=2, evidence_ok=True,
            failure_class=None if complete else "unknown",
            primary_tool="list_files",
        )
        return conversation_id, prediction

    def _seed_a_stageable_family(self) -> None:
        """The reuses must land inside a SEALED epoch, so they are seeded in
        the middle -- the effectiveness clause reads sealed epochs only."""
        for index in range(18):
            self._resolved_outcome(complete=bool(index % 6))
        conversation_id, prediction = self._resolved_outcome()
        reflection = self.memory.record_reflection(
            status="complete", summary="Pass fixture outcome.",
            improvements="Prefer a lowercase hyphenated file naming convention.",
            conversation_id=conversation_id, prediction_id=prediction, tool_calls=2,
        )
        row = self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='lesson' AND reflection_id=?",
            (reflection,),
        ).fetchone()
        lesson_id = int(row["id"])
        for _index in range(14):
            conversation = self.memory.new_conversation("reuse", project_id=1)
            reuse = self.memory.record_prediction(
                family=self.FAMILY, profile="pass-test", model="deterministic",
                predicted_success=0.8, predicted_steps=2,
                predicted_verification="tool_success", basis="prior",
                origin="interactive", conversation_id=conversation,
            )
            self.memory.record_lesson_applications(reuse, self.FAMILY, [lesson_id])
            self.memory.resolve_prediction(
                reuse, actual_status="complete", actual_steps=2,
                evidence_ok=True, primary_tool="list_files",
            )
        for index in range(28):
            self._resolved_outcome(complete=bool(index % 6))

    def test_one_pass_seals_epochs_and_stages_a_candidate(self) -> None:
        self._seed_a_stageable_family()
        self.assertEqual(self.memory.calibration_ledger(family=self.FAMILY), [])

        outcomes = cli.run_ladder_consolidation(self.config, self.memory)

        self.assertTrue(outcomes)
        first = outcomes[0]
        self.assertTrue(first.get("ok", True), first)
        self.assertTrue(self.memory.calibration_ledger(family=self.FAMILY))
        staged = [
            dict(row)
            for row in self.memory.ladder_promotions(project_id=1)
            if str(row.get("stage")) == "staged"
        ]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["family"], self.FAMILY)
        # The pass NEVER approves: that is a typed operator command.
        self.assertFalse(
            [
                row for row in self.memory.ladder_promotions(project_id=1)
                if str(row.get("stage")) == "approved"
            ]
        )
        self.assertFalse(list((self.workspace / ".jarvis-skills").rglob("SKILL.md")))

    def test_a_second_pass_is_a_no_op(self) -> None:
        """Idempotent: the worker runs this every cycle, forever."""
        self._seed_a_stageable_family()
        cli.run_ladder_consolidation(self.config, self.memory)
        epochs = len(self.memory.calibration_ledger(family=self.FAMILY))
        rows = [dict(row) for row in self.memory.ladder_promotions(project_id=1)]

        second = cli.run_ladder_consolidation(self.config, self.memory)

        self.assertTrue(all(item.get("ok", True) for item in second))
        self.assertEqual(len(self.memory.calibration_ledger(family=self.FAMILY)), epochs)
        self.assertEqual(
            [dict(row) for row in self.memory.ladder_promotions(project_id=1)], rows
        )

    def test_every_pass_leaves_an_activity_receipt(self) -> None:
        """A refusal nobody hears about is the defect the old distiller had."""
        self._seed_a_stageable_family()

        cli.run_ladder_consolidation(self.config, self.memory, source="worker")

        rows = self.memory.db.execute(
            "SELECT COUNT(*) AS n FROM activity_log WHERE category='ladder'"
        ).fetchone()
        self.assertGreaterEqual(int(rows["n"]), 1)

    def test_a_pass_over_an_empty_store_records_a_receipt_and_changes_nothing(
        self,
    ) -> None:
        outcomes = cli.run_ladder_consolidation(self.config, self.memory)

        self.assertTrue(all(item.get("ok", True) for item in outcomes))
        self.assertEqual(self.memory.ladder_promotions(project_id=1), [])
        rows = self.memory.db.execute(
            "SELECT COUNT(*) AS n FROM activity_log WHERE category='ladder'"
        ).fetchone()
        self.assertGreaterEqual(int(rows["n"]), 1)

    def test_an_exception_is_reported_never_swallowed(self) -> None:
        """The old call site was `except Exception: pass`; this one is not."""
        with patch.object(
            cli.learning_ladder, "run_ladder_pass",
            side_effect=RuntimeError("synthetic pass failure"),
        ):
            outcomes = cli.run_ladder_consolidation(self.config, self.memory)

        self.assertTrue(outcomes)
        self.assertFalse(outcomes[0]["ok"])
        self.assertEqual(outcomes[0]["reason"], "RuntimeError")
        rows = self.memory.db.execute(
            "SELECT COUNT(*) AS n FROM activity_log "
            "WHERE category='ladder' AND status='failed'"
        ).fetchone()
        self.assertGreaterEqual(int(rows["n"]), 1)

    def test_a_project_with_no_workspace_is_reported_and_skipped(self) -> None:
        with patch.object(
            cli, "_ladder_workspace",
            side_effect=cli.LadderWorkspaceUnavailable("workspace_unavailable"),
        ):
            outcomes = cli.run_ladder_consolidation(self.config, self.memory)

        self.assertTrue(outcomes)
        self.assertFalse(outcomes[0]["ok"])
        self.assertEqual(outcomes[0]["reason"], "workspace_unavailable")

    def test_the_ladder_run_subcommand_calls_the_same_pass(self) -> None:
        self._seed_a_stageable_family()
        output = io.StringIO()
        with patch.object(cli.Config, "load", return_value=self.config), \
                redirect_stdout(output):
            code = cli._run_ladder(
                argparse.Namespace(ladder_command="run", project=None, json=False)
            )
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("sealed", rendered)
        self.assertIn("staged", rendered)
        # The confirmation code is NOT printed by a pass: `ladder list` and
        # `ladder show` are the surfaces that show it.
        staged = [
            dict(row)
            for row in self.memory.ladder_promotions(project_id=1, include_token=True)
            if str(row.get("stage")) == "staged"
        ]
        self.assertEqual(len(staged), 1)
        self.assertNotIn(str(staged[0]["approval_token"]), rendered)

    def test_seal_all_routes_through_the_same_pass(self) -> None:
        self._seed_a_stageable_family()
        output = io.StringIO()
        with patch.object(cli.Config, "load", return_value=self.config), \
                redirect_stdout(output):
            code = cli._run_ladder(
                argparse.Namespace(
                    ladder_command="seal", family=None, all=True, json=False
                )
            )
        self.assertEqual(code, 0)
        self.assertTrue(self.memory.calibration_ledger(family=self.FAMILY))

    def test_a_store_without_the_ladder_surface_is_skipped_quietly(self) -> None:
        """Not every store a caller passes is a full `Memory`.

        A store below schema 49 has no ladder, and the worker tests drive
        `worker()` with a stub that implements only the task-lease methods.
        Asking such a store for `get_project` raised `AttributeError` out of
        the pass, through the worker's `except Exception` recovery, and
        abandoned the entire cycle -- so the task was never claimed and
        sixteen unrelated tests failed on `IndexError: list index out of
        range`, a symptom with no visible connection to the ladder.
        """

        class TaskLeaseOnlyStore:
            """Exactly the surface the worker tests' stub implements."""

            def claim_task(self, *_args, **_kwargs):
                return None

            def recover_stale_tasks(self):
                return {"requeued": 0, "failed": 0}

        outcomes = cli.run_ladder_consolidation(self.config, TaskLeaseOnlyStore())

        self.assertEqual(outcomes, [])

    def test_the_pass_never_guesses_a_project(self) -> None:
        """It returns nothing rather than falling back to project 1.

        Guessing is what made the earlier fallback questionable: if the store
        cannot say which projects exist, consolidating "the one that probably
        does" invents a fact, and if every project is disabled then
        consolidating one of them is wrong outright.  A real store always
        carries project 1, so the guess only fired in the abnormal cases where
        it was least defensible.
        """

        class Enumeration:
            """A full ladder surface whose project list is the variable."""

            def __init__(self, projects, raises=False):
                self._projects = projects
                self._raises = raises

            def list_projects(self):
                if self._raises:
                    raise RuntimeError("synthetic enumeration failure")
                return self._projects

            def get_project(self, _identifier):
                raise AssertionError("no project should have been consulted")

            def ladder_candidates(self, **_kwargs):
                return []

            def seal_calibration_epoch(self, *_args, **_kwargs):
                return []

        for label, store in (
            ("enumeration raised", Enumeration([], raises=True)),
            ("no projects at all", Enumeration([])),
            ("every project disabled", Enumeration(
                [{"id": 1, "enabled": 0}, {"id": 2, "enabled": False}]
            )),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    cli.run_ladder_consolidation(self.config, store), []
                )

    def test_a_store_missing_list_projects_is_skipped_by_construction(self) -> None:
        """`list_projects` is in the capability tuple, so the enumeration path
        is unreachable on a store that cannot answer it -- rather than
        unreachable only because the capability check happens to run first.
        That ordering dependency is the same shape as the defect that put a
        background pass upstream of `claim_task`."""

        class NoEnumeration:
            def get_project(self, _identifier):
                raise AssertionError("unreachable")

            def ladder_candidates(self, **_kwargs):
                return []

            def seal_calibration_epoch(self, *_args, **_kwargs):
                return []

        self.assertEqual(
            cli.run_ladder_consolidation(self.config, NoEnumeration()), []
        )

    def test_a_failing_pass_never_costs_the_worker_its_task_loop(self) -> None:
        """The wrapper, and the reason it is broader than the capability check.

        Whatever goes wrong in a BACKGROUND consolidation, the worker's job is
        the task loop.  Dropping a task because a background pass failed is a
        worse outcome than the pass not running, and it presents as a symptom
        nowhere near the cause.
        """
        self._seed_a_stageable_family()
        claimed: list[str] = []

        class WatchedMemory:
            """Delegates everything, and records that the loop got its turn."""

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                if name == "claim_task":
                    def claim(*args, **kwargs):
                        claimed.append("claimed")
                        return self._inner.claim_task(*args, **kwargs)
                    return claim
                return getattr(self._inner, name)

            # Dunders bypass __getattr__, and worker() uses `with memory:`.
            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *details):
                return self._inner.__exit__(*details)

        with patch.object(cli.Config, "load", return_value=self.config), \
                patch.object(
                    cli, "run_ladder_consolidation",
                    side_effect=RuntimeError("synthetic pass failure"),
                ), \
                patch.object(cli, "_runtime_state", return_value="running"):
            code = cli.worker(
                1,
                max_cycles=1,
                sleep=lambda _seconds: None,
                memory_factory=lambda *_a, **_k: WatchedMemory(
                    Memory(self.data_dir / "jarvis.db")
                ),
                agent_factory=lambda *_a, **_k: SimpleNamespace(),
                manage_process_lock=False,
                status_heartbeat=False,
            )

        self.assertIn(code, (0, 1))
        # The loop reached the task lease despite the pass blowing up.
        self.assertTrue(
            claimed, "a failing background pass abandoned the task loop"
        )

    def test_a_ladderless_stub_store_still_claims_and_completes_its_task(
        self,
    ) -> None:
        """The exact regression, driven the way test_cli drives the worker.

        `test_cli.WorkerTests` uses a hand-rolled stub implementing only the
        task-lease surface.  The consolidation pass asked it for `get_project`,
        the `AttributeError` reached the worker's `except Exception`, and the
        iteration was abandoned -- so the task was never claimed and sixteen
        assertions failed on `IndexError: list index out of range`, a symptom
        with no visible connection to the ladder.

        This asserts the whole chain the failure broke: the task IS claimed,
        the agent IS run, and the task IS finished, on a store that has no
        ladder at all.
        """
        finished: list[tuple] = []
        claimed: list[dict] = []

        class LadderlessStore:
            """Only the task-lease surface, exactly as test_cli's stub has."""

            def __init__(self) -> None:
                self.task = {
                    "id": 1, "prompt": "do the thing", "project_id": 1,
                    "attempts": 0, "max_attempts": 3,
                }

            def __enter__(self):
                return self

            def __exit__(self, *_details):
                return False

            def recover_stale_tasks(self):
                return {"requeued": 0, "failed": 0}

            def queue_due_learning(self):
                return 0

            def claim_task(self, **kwargs):
                claimed.append(kwargs)
                task, self.task = self.task, None
                return task

            def finish_task(self, *args, **kwargs):
                finished.append((args, kwargs))
                return True

            def fail_task(self, *args, **kwargs):
                return "queued"

            def renew_task_lease(self, *_args, **_kwargs):
                return True

        store = LadderlessStore()

        class DoneAgent:
            def run(self, *_args, **_kwargs):
                return AgentResult("done", status="complete", tool_calls=1)

        with patch.object(cli.Config, "load", return_value=self.config), \
                patch.object(cli, "_runtime_state", return_value="running"):
            cli.worker(
                1,
                max_cycles=2,
                sleep=lambda _seconds: None,
                memory_factory=lambda *_a, **_k: store,
                agent_factory=lambda *_a, **_k: DoneAgent(),
                heartbeat_factory=lambda *_a, **_k: SimpleNamespace(
                    start=lambda: None, stop=lambda: None, lost=False
                ),
                manage_process_lock=False,
                status_heartbeat=False,
            )

        self.assertTrue(claimed, "the task was never claimed")
        self.assertTrue(finished, "the task was claimed but never finished")

    def test_the_worker_runs_the_pass_each_cycle(self) -> None:
        """The call site, not just the function: a driver nobody calls is the
        defect this whole item is about."""
        self._seed_a_stageable_family()
        seen: list[object] = []
        real = cli.run_ladder_consolidation

        def spy(config, memory, **kwargs):
            seen.append(kwargs.get("source", "worker"))
            return real(config, memory, **kwargs)

        with patch.object(cli.Config, "load", return_value=self.config), \
                patch.object(cli, "run_ladder_consolidation", side_effect=spy), \
                patch.object(cli, "_runtime_state", return_value="running"):
            code = cli.worker(
                1,
                max_cycles=1,
                sleep=lambda _seconds: None,
                # A SECOND connection to the same file: worker() closes the
                # store it is given, and the fixture still needs its own.
                memory_factory=lambda *_a, **_k: Memory(
                    self.data_dir / "jarvis.db"
                ),
                agent_factory=lambda *_a, **_k: SimpleNamespace(),
                manage_process_lock=False,
                status_heartbeat=False,
            )

        self.assertIn(code, (0, 1))
        self.assertTrue(seen, "the worker cycle never called the ladder pass")
        self.assertTrue(self.memory.calibration_ledger(family=self.FAMILY))


if __name__ == "__main__":
    unittest.main()
