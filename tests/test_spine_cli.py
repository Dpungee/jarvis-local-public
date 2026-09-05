from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis import cli
from jarvis.cli import _display_memories, _run_spine
from jarvis.config import Config
from jarvis.memory import Memory


def _command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _args(command: str, **flags: object) -> Namespace:
    fields: dict[str, object] = {"spine_command": command, "json": False}
    if command == "tail":
        fields["limit"] = 20
    if command == "rebuild-claims":
        fields["apply"] = False
        fields["yes"] = False
        fields["plan"] = None
    fields.update(flags)
    return Namespace(**fields)


class SpineCliTests(unittest.TestCase):
    """`python -m jarvis spine` at schema 47: verify counts, rebuild-claims
    --apply [--yes] with its exit codes, rebuild-memories, tail."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.data_dir = root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        # The CLI opens data_dir / "jarvis.db".
        self.db_path = self.data_dir / "jarvis.db"
        memory = Memory(self.db_path)
        try:
            conversation = memory.new_conversation(project_id=1)
            memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrel relay", "listen port", "9090")
            )
            memory.remember_explicit_project_claim(
                conversation, 1, _command("Harrier box", "datacenter", "Fenwick")
            )
            memory.remember_verified(
                "The sprint demo is on Thursday.",
                kind="fact",
                source="fixture",
                origin="explicit_operator_memory",
            )
        finally:
            memory.close()
        self.config_patch = patch(
            "jarvis.cli.Config.load",
            return_value=replace(
                Config.load(), data_dir=self.data_dir, workspace=self.workspace, vault_dir=None
            ),
        )
        self.config_patch.start()
        self.baseline_rebuilds = self._rebuild_receipts()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp.cleanup()

    def _run(self, command: str, **flags: object) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = _run_spine(_args(command, **flags))
        return code, output.getvalue()

    def _raw(self, sql: str, *params: object) -> None:
        raw = sqlite3.connect(str(self.db_path))
        try:
            raw.execute(sql, params)
            raw.commit()
        finally:
            raw.close()

    def _claim_values(self) -> list[tuple[int, str]]:
        with Memory(self.db_path) as memory:
            rows = memory.db.execute(
                "SELECT id, value FROM memory_claims ORDER BY id"
            ).fetchall()
            return [(int(row[0]), str(row[1])) for row in rows]

    def _claim_texts(self) -> list[str]:
        """Every value, subject, predicate, and source text of the claim rows
        and their backing memories: none of it may reach the operator surface."""
        with Memory(self.db_path) as memory:
            texts: list[str] = []
            for row in memory.db.execute(
                "SELECT subject, predicate, value, source FROM memory_claims"
            ):
                texts.extend(str(item) for item in row if item)
            for row in memory.db.execute(
                "SELECT m.source FROM memories AS m JOIN memory_claims AS c ON c.memory_id=m.id"
            ):
                texts.extend(str(item) for item in row if item)
            return [text for text in texts if len(text) >= 4]

    @staticmethod
    def _token_in(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("plan token: "):
                return line.split("plan token: ", 1)[1].strip()
        return ""

    def _rebuild_receipts(self) -> int:
        """How many ``projection.rebuilt`` events the store holds.

        Migration 48 appends one when it builds the graph projection, so a
        command that changes nothing leaves this count where it was rather
        than leaving the kind absent.
        """
        return self._kinds().count("projection.rebuilt")

    def _kinds(self) -> list[str]:
        with Memory(self.db_path) as memory:
            return [
                str(row[0])
                for row in memory.db.execute(
                    "SELECT kind FROM memory_spine_events ORDER BY id"
                )
            ]

    # --- parser -----------------------------------------------------------------

    def test_parser_accepts_the_slice_2_flags(self) -> None:
        parser = cli._parser()
        args = parser.parse_args(
            ["spine", "rebuild-claims", "--apply", "--yes", "--plan", "deadbeefcafe", "--json"]
        )
        self.assertEqual(args.spine_command, "rebuild-claims")
        self.assertTrue(args.apply)
        self.assertTrue(args.yes)
        self.assertEqual(args.plan, "deadbeefcafe")
        self.assertTrue(args.json)
        args = parser.parse_args(["spine", "rebuild-claims"])
        self.assertFalse(args.apply)
        self.assertFalse(args.yes)
        self.assertIsNone(args.plan)
        args = parser.parse_args(["spine", "rebuild-memories", "--json"])
        self.assertEqual(args.spine_command, "rebuild-memories")
        self.assertTrue(args.json)
        args = parser.parse_args(["spine", "verify"])
        self.assertEqual(args.spine_command, "verify")

    # --- verify -------------------------------------------------------------------

    def test_verify_reports_the_memory_lineage_counts(self) -> None:
        code, text = self._run("verify")
        self.assertEqual(code, 0, text)
        self.assertIn("Memory spine OK", text)
        self.assertIn("memories,", text)
        self.assertIn("claim backing rows", text)
        self.assertIn("memory events", text)
        code, text = self._run("verify", json=True)
        self.assertEqual(code, 0, text)
        report = json.loads(text)
        self.assertTrue(report["ok"])
        for key in ("memory_rows", "memory_events", "memory_lineage_ok", "memory_sequence_ok"):
            self.assertIn(key, report)
        self.assertTrue(
            "claim_backing_rows" in report or "claim_rows" in report,
            "the claim backing-row count is missing",
        )
        # Two claim backing rows and one ordinary memory carry lineage.
        self.assertGreaterEqual(int(report["memory_rows"]), 3)
        self.assertGreaterEqual(int(report["memory_events"]), 1)

    # --- rebuild-claims ---------------------------------------------------------------

    def test_dry_run_is_equivalent_on_an_untouched_store(self) -> None:
        code, text = self._run("rebuild-claims")
        self.assertEqual(code, 0, text)
        self.assertIn("equivalent", text)
        code, text = self._run("rebuild-claims", apply=True, yes=True)
        self.assertEqual(code, 0, text)
        self.assertIn("Nothing to apply", text)
        self.assertEqual(self._rebuild_receipts(), self.baseline_rebuilds)

    def test_apply_without_yes_prints_the_plan_and_changes_nothing(self) -> None:
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        before = self._claim_values()

        code, text = self._run("rebuild-claims", apply=True)

        self.assertEqual(code, 2, text)
        self.assertIn("DIVERGENT", text)
        self.assertIn("claim 1: field", text)
        self.assertIn("Would change:", text)
        self.assertIn("--apply --yes", text)
        self.assertEqual(self._claim_values(), before)
        self.assertEqual(self._rebuild_receipts(), self.baseline_rebuilds)

    def test_divergence_output_never_carries_values_or_key_text(self) -> None:
        # M-1: a value edit's dry run, plan, and --json output name the claim
        # id, the field, and digests only; never the live or rebuilt value,
        # the subject, the predicate, or a source text.
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        secrets = ["9090", "9999", *self._claim_texts()]
        self.assertIn("Kestrel relay", secrets)
        self.assertIn("listen port", secrets)
        for flags in (
            {},
            {"json": True},
            {"apply": True},
            {"apply": True, "json": True},
        ):
            with self.subTest(flags=flags):
                code, text = self._run("rebuild-claims", **flags)
                self.assertEqual(code, 2 if flags.get("apply") else 1, text)
                self.assertIn("claim", text)
                self.assertIn("field", text)
                for secret in secrets:
                    self.assertNotIn(secret, text)
        self.assertEqual(self._claim_values()[0], (1, "9999"))

    def test_plan_token_binds_apply_to_the_plan_the_operator_saw(self) -> None:
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        code, text = self._run("rebuild-claims", apply=True)
        self.assertEqual(code, 2, text)
        token = self._token_in(text)
        self.assertRegex(token, r"^[0-9a-f]{12}$")
        self.assertIn(f"--apply --yes --plan {token}", text)
        code, text = self._run("rebuild-claims", apply=True, json=True)
        self.assertEqual(code, 2, text)
        self.assertEqual(json.loads(text)["plan_token"], token)

        code, text = self._run("rebuild-claims", apply=True, yes=True, plan=token)

        self.assertEqual(code, 0, text)
        self.assertIn("Claim projection rebuilt", text)
        self.assertEqual(self._claim_values()[0], (1, "9090"))
        self.assertGreater(self._rebuild_receipts(), self.baseline_rebuilds)
        code, text = self._run("rebuild-claims")
        self.assertEqual(code, 0, text)

    def test_stale_plan_token_refuses_and_changes_nothing(self) -> None:
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        code, text = self._run("rebuild-claims", apply=True)
        self.assertEqual(code, 2, text)
        token = self._token_in(text)
        self.assertTrue(token)
        # The store changes after the operator saw the plan.
        self._raw("UPDATE memory_claims SET value='Elsewhere' WHERE id=2")
        before = self._claim_values()

        code, text = self._run("rebuild-claims", apply=True, yes=True, plan=token)

        self.assertEqual(code, 1, text)
        self.assertIn("stale_plan", text)
        self.assertIn("nothing changed", text)
        self.assertEqual(self._claim_values(), before)
        self.assertEqual(self._rebuild_receipts(), self.baseline_rebuilds)
        code, text = self._run("rebuild-claims", apply=True, yes=True, plan="deadbeefcafe", json=True)
        self.assertEqual(code, 1, text)
        self.assertEqual(json.loads(text)["refusal"], "stale_plan")
        self.assertEqual(self._claim_values(), before)
        # The fresh plan carries a new token, and that one applies.
        code, text = self._run("rebuild-claims", apply=True)
        fresh = self._token_in(text)
        self.assertTrue(fresh)
        self.assertNotEqual(fresh, token)
        code, text = self._run("rebuild-claims", apply=True, yes=True, plan=fresh)
        self.assertEqual(code, 0, text)
        self.assertEqual([value for _id, value in self._claim_values()], ["9090", "Fenwick"])

    def test_apply_without_a_token_prints_the_fresh_plan_before_applying(self) -> None:
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        code, text = self._run("rebuild-claims", apply=True, yes=True)
        self.assertEqual(code, 0, text)
        self.assertIn("Claim projection rebuild (plan)", text)
        self.assertTrue(self._token_in(text))
        self.assertLess(text.index("plan token: "), text.index("Claim projection rebuilt"))
        self.assertEqual(self._claim_values()[0], (1, "9090"))

    def test_plan_without_apply_yes_is_a_usage_error(self) -> None:
        for flags in ({"plan": "deadbeefcafe"}, {"apply": True, "plan": "deadbeefcafe"}):
            with self.subTest(flags=flags):
                code, text = self._run("rebuild-claims", **flags)
                self.assertEqual(code, 2, text)
                self.assertIn("--plan requires --apply --yes", text)

    def test_yes_without_apply_is_a_usage_error(self) -> None:
        code, text = self._run("rebuild-claims", yes=True)
        self.assertEqual(code, 2, text)
        self.assertIn("--yes requires --apply", text)

    def test_apply_yes_reconciles_and_the_dry_run_is_then_equivalent(self) -> None:
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        code, text = self._run("rebuild-claims")
        self.assertEqual(code, 1, text)

        code, text = self._run("rebuild-claims", apply=True, yes=True)

        self.assertEqual(code, 0, text)
        self.assertIn("Claim projection rebuilt", text)
        self.assertIn("rows before 2", text)
        self.assertIn("rows after 2", text)
        self.assertIn("divergences fixed", text)
        self.assertIn("projection.rebuilt event #", text)
        # Values never reach the operator surface; ids do.
        self.assertNotIn("9090", text)
        self.assertNotIn("9999", text)
        self.assertIn("updated: 1", text)
        self.assertEqual(self._claim_values()[0], (1, "9090"))
        self.assertGreater(self._rebuild_receipts(), self.baseline_rebuilds)
        code, text = self._run("rebuild-claims")
        self.assertEqual(code, 0, text)
        code, text = self._run("verify")
        self.assertEqual(code, 0, text)

    def test_apply_yes_json_carries_the_report(self) -> None:
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        code, text = self._run("rebuild-claims", apply=True, yes=True, json=True)
        self.assertEqual(code, 0, text)
        report = json.loads(text)
        self.assertTrue(report["ok"])
        self.assertEqual(report["rows_before"], 2)
        self.assertEqual(report["rows_after"], 2)
        self.assertGreaterEqual(int(report["divergences_fixed"]), 1)
        self.assertEqual(report["updated_ids"], [1])
        self.assertEqual(report["removed_ids"], [])
        self.assertIsInstance(report["event_id"], int)

    def test_apply_refuses_when_the_spine_fails_verification(self) -> None:
        self._raw("DROP TRIGGER memory_spine_events_redaction_only")
        self._raw("UPDATE memory_spine_events SET actor='model' WHERE kind='claim.created'")
        self._raw("UPDATE memory_claims SET value='9999' WHERE id=1")
        before = self._claim_values()

        code, text = self._run("rebuild-claims", apply=True, yes=True)

        self.assertEqual(code, 1, text)
        self.assertIn("refused", text)
        self.assertIn("nothing changed", text)
        self.assertEqual(self._claim_values(), before)
        self.assertEqual(self._rebuild_receipts(), self.baseline_rebuilds)
        # The plan names the spine-side problem instead of promising a fix.
        code, text = self._run("rebuild-claims", apply=True)
        self.assertEqual(code, 2, text)
        self.assertIn("spine-side", text)

    # --- rebuild-memories -----------------------------------------------------------

    def test_rebuild_memories_is_equivalent_then_reports_an_out_of_band_edit(self) -> None:
        code, text = self._run("rebuild-memories")
        self.assertEqual(code, 0, text)
        self.assertIn("Memory projection rebuild (dry run): equivalent", text)

        self._raw(
            "UPDATE memories SET content='The sprint demo is on Friday.' "
            "WHERE content='The sprint demo is on Thursday.'"
        )
        code, text = self._run("rebuild-memories")
        self.assertEqual(code, 1, text)
        self.assertIn("DIVERGENT", text)
        self.assertIn("memory ", text)
        self.assertNotIn("Friday", text)
        self.assertNotIn("Thursday", text)
        code, text = self._run("rebuild-memories", json=True)
        self.assertEqual(code, 1, text)
        report = json.loads(text)
        self.assertFalse(report["ok"])
        kinds = {item["kind"] for item in report["divergences"]}
        self.assertTrue(kinds & {"field", "provenance", "lineage"}, kinds)
        self.assertNotIn("Friday", text)

    # --- tail -------------------------------------------------------------------------

    def test_tail_lists_memory_events_by_key_only(self) -> None:
        code, text = self._run("tail", limit=10)
        self.assertEqual(code, 0, text)
        self.assertIn("memory.created", text)
        self.assertIn("claim.created", text)
        self.assertNotIn("Thursday", text)
        self.assertNotIn("9090", text)


class MemoryListEraseCliTests(unittest.TestCase):
    """``python -m jarvis memory list | erase`` (design 6.1): ids and a
    screened preview, an erase that needs --yes, and three refusals that
    change nothing."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.data_dir = root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.db_path = self.data_dir / "jarvis.db"
        memory = Memory(self.db_path)
        try:
            conversation = memory.new_conversation(project_id=1)
            memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrel relay", "listen port", "9090")
            )
            # remember_verified returns a receipt, not an id, so the ids come
            # back from the store the way an operator gets them: by listing.
            for content in (
                "The sprint demo is on Thursday.",
                "Reach the on-call operator on 415-555-0199 after hours.",
            ):
                memory.remember_verified(
                    content,
                    kind="fact",
                    source="fixture",
                    origin="explicit_operator_memory",
                )
            ids = {
                str(row["content"]): int(row["id"])
                for row in memory.db.execute(
                    "SELECT id, content FROM memories"
                ).fetchall()
            }
            self.plain_id = ids["The sprint demo is on Thursday."]
            self.private_id = ids[
                "Reach the on-call operator on 415-555-0199 after hours."
            ]
            self.claim_backed_id = int(memory.db.execute(
                "SELECT memory_id FROM memory_claims ORDER BY id LIMIT 1"
            ).fetchone()[0])
        finally:
            memory.close()
        self.config_patch = patch(
            "jarvis.cli.Config.load",
            return_value=replace(
                Config.load(),
                data_dir=self.data_dir,
                workspace=self.workspace,
                vault_dir=None,
            ),
        )
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp.cleanup()

    def _memory_args(self, command: str, **flags: object) -> Namespace:
        fields: dict[str, object] = {
            "command": "memory",
            "memory_command": command,
            "memory_id": None,
            "yes": False,
            "json": False,
            "limit": 20,
            "index": False,
        }
        fields.update(flags)
        return Namespace(**fields)

    def _run_memory(self, command: str, **flags: object) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli._run_memory(self._memory_args(command, **flags))
        return code, output.getvalue()

    def _rows(self) -> int:
        raw = sqlite3.connect(str(self.db_path))
        try:
            return int(raw.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            raw.close()

    def test_parser_accepts_list_and_erase_and_keeps_status_the_default(self) -> None:
        parser = cli._parser()
        self.assertEqual(parser.parse_args(["memory"]).memory_command, "status")
        self.assertEqual(parser.parse_args(["memory", "status"]).memory_command, "status")
        listing = parser.parse_args(["memory", "list", "--limit", "5", "--json"])
        self.assertEqual(listing.memory_command, "list")
        self.assertEqual(listing.limit, 5)
        erase = parser.parse_args(["memory", "erase", "12", "--yes"])
        self.assertEqual((erase.memory_command, erase.memory_id), ("erase", 12))
        self.assertTrue(erase.yes)

    def test_list_shows_ids_provenance_and_a_screened_preview(self) -> None:
        code, text = self._run_memory("list")
        self.assertEqual(code, 0, text)
        self.assertIn(f"#{self.plain_id}", text)
        self.assertIn("explicit_operator_memory", text)
        self.assertIn("The sprint demo is on Thursday.", text)
        # The widened screen decides: a phone number is never previewed.
        self.assertNotIn("415-555-0199", text)
        self.assertIn("[PRIVATE]", text)

    def test_list_json_carries_the_same_screened_preview(self) -> None:
        code, text = self._run_memory("list", json=True)
        self.assertEqual(code, 0, text)
        payload = {int(row["id"]): row for row in json.loads(text)}
        self.assertEqual(payload[self.private_id]["preview"], "[PRIVATE]")
        self.assertEqual(
            payload[self.plain_id]["preview"], "The sprint demo is on Thursday."
        )

    def test_erase_without_yes_prints_the_kind_and_exits_two(self) -> None:
        before = self._rows()
        code, text = self._run_memory("erase", memory_id=self.plain_id)
        self.assertEqual(code, 2, text)
        self.assertIn(f"Would erase memory #{self.plain_id}", text)
        self.assertIn("kind: fact", text)
        self.assertIn("--yes", text)
        # No content is echoed on the confirmation path either.
        self.assertNotIn("sprint demo", text)
        self.assertEqual(self._rows(), before)

    def test_erase_with_yes_removes_the_row_and_prints_the_fixed_receipt(self) -> None:
        before = self._rows()
        code, text = self._run_memory("erase", memory_id=self.plain_id, yes=True)
        self.assertEqual(code, 0, text)
        self.assertIn(f"Erased memory #{self.plain_id}", text)
        self.assertNotIn("sprint demo", text)
        self.assertEqual(self._rows(), before - 1)

    def test_a_claim_backing_row_is_refused_and_changes_nothing(self) -> None:
        before = self._rows()
        code, text = self._run_memory(
            "erase", memory_id=self.claim_backed_id, yes=True
        )
        self.assertEqual(code, 1, text)
        self.assertIn("backs a project fact", text)
        self.assertEqual(self._rows(), before)

    def test_a_missing_id_is_refused_and_changes_nothing(self) -> None:
        before = self._rows()
        code, text = self._run_memory("erase", memory_id=9_999, yes=True)
        self.assertEqual(code, 1, text)
        self.assertIn("No memory #9999 exists", text)
        self.assertEqual(self._rows(), before)

    def test_a_missing_id_without_yes_is_a_settled_refusal(self) -> None:
        # Exit 1, not 2: --yes would not change the answer, so this is a
        # refusal like the other two, not a confirmation prompt.
        code, text = self._run_memory("erase", memory_id=9_999)
        self.assertEqual(code, 1, text)
        self.assertIn("No memory #9999 exists", text)
        self.assertIn("nothing changed", text)

    def test_a_claim_backing_row_is_named_before_yes_not_denied(self) -> None:
        """C-1: the confirmation must never say "no such memory" about a row
        that exists.

        ``list_memories`` is a listing and hides ``kind='claim'`` backing
        rows, so confirming through it denied the existence of exactly the
        rows the erase has a fixed refusal for.  ``describe_memory`` reads by
        primary key.
        """
        before = self._rows()
        code, text = self._run_memory("erase", memory_id=self.claim_backed_id)
        self.assertEqual(code, 1, text)
        self.assertNotIn("No memory", text)
        self.assertIn("backs a project fact", text)
        self.assertIn("Erase this project fact:", text)
        self.assertEqual(self._rows(), before)

    def test_the_pre_yes_refusal_matches_what_erase_would_do(self) -> None:
        # The confirmation's reason and the store's own action must agree, or
        # the operator is told one thing and gets another after --yes.
        with Memory(self.db_path) as memory:
            described = memory.describe_memory(self.claim_backed_id)
            self.assertTrue(described["is_claim_backing"])
            self.assertEqual(
                memory.erase_memory(None, self.claim_backed_id)["action"],
                "claim_backing",
            )

    def test_a_vault_note_row_is_refused_before_yes(self) -> None:
        with Memory(self.db_path) as memory:
            memory.db.execute(
                "UPDATE ordinary_memory_provenance SET origin=? WHERE memory_id=?",
                ("verified_vault_note", self.plain_id),
            )
            memory.db.commit()
        before = self._rows()
        code, text = self._run_memory("erase", memory_id=self.plain_id)
        self.assertEqual(code, 1, text)
        self.assertIn("mirrors a vault note", text)
        self.assertEqual(self._rows(), before)

    def test_the_confirmation_names_the_row_without_echoing_it(self) -> None:
        code, text = self._run_memory("erase", memory_id=self.plain_id)
        self.assertEqual(code, 2, text)
        self.assertIn(f"Would erase memory #{self.plain_id}", text)
        self.assertIn("kind: fact", text)
        self.assertIn("characters", text)
        self.assertIn("explicit_operator_memory", text)
        # A confirmation prompt names a row; it never echoes it back.
        self.assertNotIn("sprint demo", text)

    def test_the_confirmation_json_carries_the_described_row(self) -> None:
        code, text = self._run_memory(
            "erase", memory_id=self.plain_id, json=True
        )
        self.assertEqual(code, 2, text)
        payload = json.loads(text)
        self.assertEqual(payload["reason"], "confirmation required")
        self.assertEqual(payload["id"], self.plain_id)
        self.assertEqual(payload["kind"], "fact")
        self.assertGreater(int(payload["content_length"]), 0)
        self.assertNotIn("content", payload)

    def test_erase_without_an_id_is_a_usage_error(self) -> None:
        code, text = self._run_memory("erase")
        self.assertEqual(code, 2, text)
        self.assertIn("requires an id", text)

    def test_chat_memory_listing_shows_the_erasable_id(self) -> None:
        memory = Memory(self.db_path)
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                _display_memories(memory)
        finally:
            memory.close()
        self.assertIn(f"#{self.plain_id} fact:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
