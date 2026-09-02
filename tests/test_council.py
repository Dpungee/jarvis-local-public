"""Tests for the JARVIS Council room.

Nothing here opens a Tk window or reaches a model: the roster, the model
policy, the turn scheduler, the reply parser and the minutes/report writers are
all pure, and the runtime is exercised against a scripted fake client.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import council
from jarvis.council import (
    CHAIR_KEY,
    COUNCIL_SEATS,
    DEPTH_PLANS,
    MEMBER_KEYS,
    OPERATOR_KEY,
    TABLE_KEY,
    CouncilMeeting,
    CouncilPlan,
    CouncilRuntime,
    advance,
    bounded_text,
    council_contract,
    directive_prompt,
    item_script,
    list_meetings,
    minutes_markdown,
    model_badge,
    next_directive,
    open_meeting,
    panel_for_item,
    parse_agenda,
    parse_agenda_addition,
    parse_reply,
    parse_tags,
    report_markdown,
    resolve_models,
    seat_for_name,
    seat_name,
    write_artifacts,
)
from jarvis.specialists import SPECIALISTS


def _config(**overrides):
    values = {
        "cloud_enabled": True,
        "openai_api_enabled": False,
        "codex_cli_enabled": False,
        "reasoning_model": "gpt-oss:20b",
        "fast_model": "qwen3.5:9b",
        "model": "qwen3.5:9b",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RosterTests(unittest.TestCase):
    def test_roster_is_derived_from_the_real_specialists(self):
        self.assertEqual(len(COUNCIL_SEATS), len(SPECIALISTS) + 1)
        self.assertTrue(COUNCIL_SEATS[0].chair)
        self.assertEqual(COUNCIL_SEATS[0].key, CHAIR_KEY)
        derived = {seat.key: seat.name for seat in COUNCIL_SEATS if not seat.chair}
        self.assertEqual(
            derived, {item.key: item.name for item in SPECIALISTS}
        )
        for seat in COUNCIL_SEATS:
            if not seat.chair:
                self.assertEqual(
                    seat.mandate,
                    next(s.purpose for s in SPECIALISTS if s.key == seat.key),
                )

    def test_every_seat_has_a_distinct_accent(self):
        accents = [seat.accent for seat in COUNCIL_SEATS]
        self.assertEqual(len(accents), len(set(accents)))

    def test_seat_names_resolve_both_ways(self):
        self.assertEqual(seat_name(CHAIR_KEY), "JARVIS")
        self.assertEqual(seat_name(OPERATOR_KEY), "Operator")
        self.assertEqual(seat_name(TABLE_KEY), "the table")
        self.assertEqual(seat_for_name("Sentinel").key, "cybersecurity")
        self.assertEqual(seat_for_name("  jarvis. ").key, CHAIR_KEY)
        self.assertIsNone(seat_for_name("Nobody"))


class ModelPolicyTests(unittest.TestCase):
    def test_openai_api_tier_puts_the_chair_on_sol_and_members_on_5_5(self):
        models = resolve_models(
            _config(openai_api_enabled=True), {"OPENAI_API_KEY": "sk-test"}
        )
        self.assertEqual(models.mode, "openai")
        self.assertEqual(models.chair_model, "openai:gpt-5.6-sol")
        self.assertEqual(models.chair_effort, "high")
        self.assertEqual(models.member_model, "openai:gpt-5.5")
        self.assertEqual(models.member_effort, "medium")
        chair = COUNCIL_SEATS[0]
        member = council.SEAT_BY_KEY["coding"]
        self.assertEqual(models.for_seat(chair), ("openai:gpt-5.6-sol", "high"))
        self.assertEqual(models.for_seat(member), ("openai:gpt-5.5", "medium"))

    def test_codex_cli_is_the_second_tier(self):
        models = resolve_models(_config(codex_cli_enabled=True), {})
        self.assertEqual(models.mode, "codex-cli")
        self.assertEqual(models.chair_model, "codex-cli:gpt-5.6-sol")
        self.assertEqual(models.member_model, "codex-cli:gpt-5.5")

    def test_api_key_without_the_flag_does_not_claim_the_cloud(self):
        models = resolve_models(_config(), {"OPENAI_API_KEY": "sk-test"})
        self.assertEqual(models.mode, "local")
        models = resolve_models(
            _config(openai_api_enabled=True, cloud_enabled=False),
            {"OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(models.mode, "local")

    def test_local_fallback_uses_the_configured_profiles(self):
        models = resolve_models(_config(), {})
        self.assertEqual(models.mode, "local")
        self.assertEqual(models.chair_model, "gpt-oss:20b")
        self.assertEqual(models.member_model, "qwen3.5:9b")
        self.assertIs(models.chair_effort, True)
        self.assertIs(models.member_effort, False)

    def test_environment_overrides_are_validated_not_trusted(self):
        base = _config(openai_api_enabled=True)
        key = {"OPENAI_API_KEY": "sk-test"}
        good = resolve_models(base, {**key, "JARVIS_COUNCIL_MEMBER_EFFORT": "low"})
        self.assertEqual(good.member_effort, "low")
        good = resolve_models(base, {**key, "JARVIS_COUNCIL_CHAIR_MODEL": "openai:gpt-5.5"})
        self.assertEqual(good.chair_model, "openai:gpt-5.5")
        for bad in ("has space", "x" * 260, "line\nbreak", ""):
            models = resolve_models(base, {**key, "JARVIS_COUNCIL_CHAIR_MODEL": bad})
            self.assertEqual(models.chair_model, "openai:gpt-5.6-sol")
        models = resolve_models(base, {**key, "JARVIS_COUNCIL_CHAIR_EFFORT": "nonsense"})
        self.assertEqual(models.chair_effort, "high")

    def test_badges_are_short_and_never_truncate_the_effort(self):
        self.assertEqual(model_badge("openai:gpt-5.6-sol", "high"), "SOL 5.6 · HIGH")
        self.assertEqual(model_badge("openai:gpt-5.5", "medium"), "GPT-5.5 · MED")
        self.assertEqual(model_badge("codex-cli:gpt-5.6-sol", "xhigh"), "SOL 5.6 · XHIGH")
        self.assertEqual(model_badge("qwen3.5:9b", True), "QWEN3.5:9B · THINK")
        self.assertEqual(model_badge("qwen3.5:9b", False), "QWEN3.5:9B")


class SchedulerTests(unittest.TestCase):
    def test_the_first_directive_is_always_the_chair_setting_the_agenda(self):
        meeting = open_meeting("Improve recall", DEPTH_PLANS["Standard"], started_at=1.0)
        directive = next_directive(meeting)
        self.assertEqual(directive.action, "agenda")
        self.assertEqual(directive.speaker, CHAIR_KEY)

    def test_members_answer_the_previous_speaker_not_only_the_chair(self):
        members = ("coding", "research", "cybersecurity")
        script = item_script(members, CouncilPlan(crosstalk=1))
        self.assertEqual(script[0][:3], ("open_item", CHAIR_KEY, TABLE_KEY))
        self.assertEqual(script[1][:3], ("member", "coding", CHAIR_KEY))
        self.assertEqual(script[2][:3], ("member", "research", "coding"))
        self.assertEqual(script[3][:3], ("member", "cybersecurity", "research"))
        self.assertEqual(script[4][0], "crosstalk")
        self.assertEqual(script[-1][:3], ("rule", CHAIR_KEY, TABLE_KEY))

    def test_a_single_member_gets_no_crosstalk_with_themselves(self):
        script = item_script(("coding",), CouncilPlan(crosstalk=2))
        self.assertEqual([step[0] for step in script], ["open_item", "member", "rule"])

    def test_the_lead_seat_rotates_between_agenda_items(self):
        plan = CouncilPlan(items=3, panel=3, crosstalk=0)
        leads = [panel_for_item(index, plan)[0] for index in range(3)]
        self.assertEqual(len(set(leads)), 3)
        for index in range(3):
            self.assertEqual(len(panel_for_item(index, plan)), 3)

    def test_a_full_table_plan_seats_every_member(self):
        panel = panel_for_item(0, DEPTH_PLANS["Standard"])
        self.assertEqual(sorted(panel), sorted(MEMBER_KEYS))

    def test_a_meeting_walks_every_item_and_then_reports(self):
        meeting = open_meeting("Topic", CouncilPlan(items=2, panel=2, crosstalk=1), 1.0)
        meeting.agenda = ["One", "Two"]
        actions = []
        for _ in range(60):
            directive = next_directive(meeting)
            actions.append(directive.action)
            if directive.action in {"report", "done"}:
                break
            meeting.add_turn(
                directive.speaker, directive.addressee, directive.action, "x", directive.item
            )
            advance(meeting, directive)
        self.assertEqual(actions[-1], "report")
        self.assertEqual(actions.count("rule"), 2)
        self.assertEqual(actions.count("open_item"), 2)
        self.assertEqual(meeting.item, 2)

    def test_an_operator_interjection_preempts_the_next_speaker(self):
        meeting = open_meeting("Topic", DEPTH_PLANS["Brief"], 1.0)
        meeting.agenda = ["One"]
        first = next_directive(meeting)
        self.assertEqual(first.action, "open_item")
        meeting.interject("Wait — what about cost?")
        preempted = next_directive(meeting)
        self.assertEqual(preempted.action, "answer_operator")
        self.assertEqual(preempted.addressee, OPERATOR_KEY)
        # Answering does not consume an agenda step.
        advance(meeting, preempted)
        self.assertEqual(meeting.step, 0)

    def test_advance_does_not_move_past_a_closed_meeting(self):
        meeting = open_meeting("Topic", DEPTH_PLANS["Brief"], 1.0)
        meeting.status = "closed"
        self.assertEqual(next_directive(meeting).action, "done")


class ParsingTests(unittest.TestCase):
    def test_the_to_line_selects_the_addressee_and_leaves_the_body(self):
        addressee, body = parse_reply("TO: Sentinel\nThe cap can be shaped.", "jarvis")
        self.assertEqual(addressee, "cybersecurity")
        self.assertEqual(body, "The cap can be shaped.")

    def test_an_at_prefix_and_the_operator_both_resolve(self):
        self.assertEqual(parse_reply("@Forge\nhi", "jarvis")[0], "coding")
        self.assertEqual(parse_reply("TO: operator\nhi", "jarvis")[0], OPERATOR_KEY)
        self.assertEqual(parse_reply("TO: everyone\nhi", "jarvis")[0], TABLE_KEY)

    def test_a_missing_or_unknown_name_keeps_the_scheduled_addressee(self):
        self.assertEqual(parse_reply("Just talking.", "research")[0], "research")
        addressee, body = parse_reply("TO: Nobody\nJust talking.", "research")
        self.assertEqual(addressee, "research")
        self.assertEqual(body, "Just talking.")

    def test_an_empty_tag_line_is_dropped_but_a_filled_one_is_kept(self):
        _, body = parse_reply("TO: JARVIS\nWe will audit.\nPROPOSE:", "jarvis")
        self.assertEqual(body, "We will audit.")
        _, body = parse_reply("TO: JARVIS\nWe will audit.\nPROPOSE: keep it", "jarvis")
        self.assertEqual(body, "We will audit.\nPROPOSE: keep it")
        _, body = parse_reply("TO: JARVIS\nRISK -\nAGENDA:\nStill here.", "jarvis")
        self.assertEqual(body, "Still here.")

    def test_code_fences_are_stripped_from_a_reply(self):
        _, body = parse_reply("```\nTO: Forge\nUse SQL.\n```", "jarvis")
        self.assertEqual(body, "Use SQL.")

    def test_tags_are_collected_with_their_labels(self):
        tags = parse_tags("Body.\nPROPOSE: cap the pool\nRISK: truncation hides rows")
        self.assertEqual(
            tags, (("PROPOSE", "cap the pool"), ("RISK", "truncation hides rows"))
        )

    def test_agenda_parsing_handles_numbers_bullets_and_duplicates(self):
        items = parse_agenda("1. Fix recall\n2) Fix recall\n- Cut ranking cost\n", limit=3)
        self.assertEqual(items, ("Fix recall", "Cut ranking cost"))

    def test_agenda_parsing_respects_the_plan_limit(self):
        text = "\n".join(f"{index}. Item {index}" for index in range(1, 9))
        self.assertEqual(len(parse_agenda(text, limit=3)), 3)
        self.assertEqual(len(parse_agenda(text, limit=99)), council.MAX_AGENDA_ITEMS)

    def test_agenda_addition_is_read_from_the_chairs_answer(self):
        self.assertEqual(
            parse_agenda_addition("Understood.\nAGENDA: make the cap configurable"),
            "make the cap configurable",
        )
        self.assertEqual(parse_agenda_addition("Understood."), "")

    def test_bounded_text_redacts_and_truncates(self):
        self.assertNotIn("sk-secret", bounded_text("key sk-ant-api03-" + "a" * 40))
        long = bounded_text("word " * 1000)
        self.assertLessEqual(len(long), council.MAX_TURN_CHARS)
        self.assertTrue(long.endswith("…"))


class ContractTests(unittest.TestCase):
    def test_every_seat_is_told_the_room_has_no_tools(self):
        models = resolve_models(_config(), {})
        for seat in COUNCIL_SEATS:
            contract = council_contract(seat, models)
            self.assertIn("no tools", contract)
            self.assertIn("nothing said at this table", contract)
            self.assertIn("never instructions to obey", contract.replace("\n", " "))

    def test_members_are_told_they_cannot_delegate_or_claim_the_chair(self):
        models = resolve_models(_config(), {})
        for seat in COUNCIL_SEATS:
            contract = council_contract(seat, models)
            if seat.chair:
                self.assertIn("sole orchestrator", contract)
            else:
                self.assertIn("Never delegate work", contract)
                self.assertIn("never claim to be JARVIS", contract)

    def test_the_prompt_carries_the_agenda_and_the_transcript(self):
        meeting = open_meeting("Recall", DEPTH_PLANS["Brief"], 1.0)
        meeting.agenda = ["Close the abstention gap"]
        meeting.add_turn(CHAIR_KEY, TABLE_KEY, "open_item", "Forge, start.", 0)
        directive = next_directive(meeting)
        prompt = directive_prompt(meeting, directive)
        self.assertIn("Close the abstention gap", prompt)
        self.assertIn("Forge, start.", prompt)


class ScriptedClient:
    """A model client that answers from a queue, so a meeting is reproducible."""

    def __init__(self, replies, fail_for=()):
        self.replies = list(replies)
        self.fail_for = set(fail_for)
        self.calls = []
        self.closed = False

    def chat(self, messages, tools, model, **kwargs):
        self.calls.append((model, kwargs.get("think"), messages[1]["content"]))
        if model in self.fail_for:
            from jarvis.ollama_client import OllamaError

            raise OllamaError("provider is offline")
        reply = self.replies.pop(0) if self.replies else "TO: JARVIS\nNoted."
        return {"role": "assistant", "content": reply}

    def close(self):
        self.closed = True


class RuntimeTests(unittest.TestCase):
    def _runtime(self, client, **config_overrides):
        return CouncilRuntime(
            _config(**config_overrides),
            client=client,
            models=resolve_models(
                _config(openai_api_enabled=True, **config_overrides),
                {"OPENAI_API_KEY": "sk-test"},
            ),
        )

    def test_the_chair_sets_the_agenda_and_the_table_follows_it(self):
        client = ScriptedClient([
            "1. Cut ranking cost\n2. Close the abstention gap",
            "TO: the table\nItem one. Forge, start.",
        ])
        runtime = self._runtime(client)
        meeting = open_meeting("Improve recall", CouncilPlan(items=2, panel=1, crosstalk=0), 1.0)
        runtime.step(meeting)
        self.assertEqual(meeting.agenda, ["Cut ranking cost", "Close the abstention gap"])
        self.assertEqual(meeting.turns[0].kind, "agenda")
        runtime.step(meeting)
        self.assertEqual(meeting.turns[1].kind, "open_item")

    def test_the_chair_and_the_members_run_on_different_models(self):
        client = ScriptedClient(["1. One", "TO: table\nOpen.", "TO: JARVIS\nHere."])
        runtime = self._runtime(client)
        meeting = open_meeting("Topic", CouncilPlan(items=1, panel=1, crosstalk=0), 1.0)
        for _ in range(3):
            runtime.step(meeting)
        chair_calls = [call for call in client.calls if call[0] == "openai:gpt-5.6-sol"]
        member_calls = [call for call in client.calls if call[0] == "openai:gpt-5.5"]
        self.assertEqual(len(chair_calls), 2)
        self.assertEqual(len(member_calls), 1)
        self.assertEqual({call[1] for call in chair_calls}, {"high"})
        self.assertEqual({call[1] for call in member_calls}, {"medium"})

    def test_a_meeting_runs_to_a_filed_report(self):
        replies = ["1. Cut ranking cost"] + ["TO: JARVIS\nA point.\nPROPOSE: cap the pool"] * 40
        client = ScriptedClient(replies)
        runtime = self._runtime(client)
        meeting = open_meeting("Improve recall", CouncilPlan(items=1, panel=2, crosstalk=1), 1.0)
        for _ in range(30):
            directive, _turn = runtime.step(meeting)
            if directive.action == "done":
                break
        self.assertEqual(meeting.status, "closed")
        self.assertTrue(meeting.decision)
        kinds = [turn.kind for turn in meeting.turns]
        self.assertIn("rule", kinds)
        self.assertIn("report", kinds)

    def test_an_unreachable_member_is_recorded_and_the_meeting_continues(self):
        client = ScriptedClient(
            ["1. One", "TO: table\nOpen."] + ["TO: JARVIS\nfine"] * 20,
            fail_for={"openai:gpt-5.5"},
        )
        runtime = self._runtime(client)
        meeting = open_meeting("Topic", CouncilPlan(items=1, panel=2, crosstalk=0), 1.0)
        for _ in range(8):
            directive, _turn = runtime.step(meeting)
            if directive.action == "done":
                break
        notices = [turn for turn in meeting.turns if turn.kind == "notice"]
        self.assertTrue(notices)
        self.assertIn("could not be reached", notices[0].text)
        self.assertEqual(meeting.status, "closed")

    def test_an_operator_interjection_is_answered_before_the_next_speaker(self):
        client = ScriptedClient([
            "1. One",
            "TO: operator\nUnderstood.\nAGENDA: make the cap configurable",
        ])
        runtime = self._runtime(client)
        meeting = open_meeting("Topic", CouncilPlan(items=1, panel=1, crosstalk=0), 1.0)
        runtime.step(meeting)
        meeting.interject("Keep the cap configurable.")
        directive, turn = runtime.step(meeting)
        self.assertEqual(directive.action, "answer_operator")
        self.assertEqual(turn.addressee, OPERATOR_KEY)
        self.assertNotIn("AGENDA:", turn.text)
        self.assertIn("make the cap configurable", meeting.agenda)
        self.assertEqual(meeting.pending_operator, [])

    def test_a_cancelled_turn_reads_as_an_interruption(self):
        client = ScriptedClient(["1. One"], fail_for={"openai:gpt-5.5"})
        runtime = self._runtime(client)
        meeting = open_meeting("Topic", CouncilPlan(items=1, panel=1, crosstalk=0), 1.0)
        runtime.step(meeting)
        runtime.step(meeting)
        _directive, turn = runtime.step(meeting, cancelled=lambda: True)
        self.assertIn("interrupted by the operator", turn.text)

    def test_closing_the_runtime_closes_the_client(self):
        client = ScriptedClient([])
        runtime = self._runtime(client)
        runtime.close()
        self.assertTrue(client.closed)


class DocumentTests(unittest.TestCase):
    def _meeting(self) -> CouncilMeeting:
        meeting = open_meeting("Improve recall", DEPTH_PLANS["Brief"], 1_700_000_000.0)
        meeting.agenda = ["Cut ranking cost"]
        meeting.add_turn(CHAIR_KEY, TABLE_KEY, "open_item", "Forge, start.", 0)
        meeting.add_turn("coding", CHAIR_KEY, "member", "Score in SQL.\nPROPOSE: cap at 512", 0)
        meeting.add_turn(
            "cybersecurity", "coding", "crosstalk",
            "That truncation hides rows.\nRISK: suppression channel", 0,
        )
        meeting.interject("Keep the cap configurable.")
        meeting.add_turn(CHAIR_KEY, OPERATOR_KEY, "answer_operator", "Understood.", 0)
        meeting.add_turn(CHAIR_KEY, TABLE_KEY, "rule", "Decision: measure, then cap.", 0)
        meeting.decision = "Focus next on measuring recall loss before the cap lands."
        meeting.status = "closed"
        return meeting

    def test_minutes_carry_the_turns_the_ruling_and_attendance(self):
        models = resolve_models(_config(), {})
        text = minutes_markdown(self._meeting(), models)
        self.assertIn("# Council minutes - Improve recall", text)
        self.assertIn("## Item 1 - Cut ranking cost", text)
        self.assertIn("**Forge → JARVIS**", text)
        self.assertIn("> **Decision.** Decision: measure, then cap.", text)
        self.assertIn("### Proposals", text)
        self.assertIn("### Risks raised", text)
        self.assertIn("## Operator interventions", text)
        self.assertIn("Keep the cap configurable.", text)
        self.assertIn("## Attendance", text)
        for seat in COUNCIL_SEATS:
            self.assertIn(seat.name, text)

    def test_the_report_leads_with_the_decision_and_states_its_provenance(self):
        models = resolve_models(_config(), {})
        text = report_markdown(self._meeting(), models)
        self.assertIn("## What Jarvis works on next", text)
        self.assertIn("measuring recall loss", text)
        self.assertIn("## Decisions by item", text)
        self.assertIn("has been executed or verified", text)

    def test_artifacts_are_written_as_lf_and_listed_back(self):
        meeting = self._meeting()
        models = resolve_models(_config(), {})
        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            written = write_artifacts(data_dir, meeting, models)
            for key in ("agenda", "minutes", "report", "transcript"):
                path = Path(written[key])
                self.assertTrue(path.is_file())
                self.assertNotIn(b"\r\n", path.read_bytes())
            rows = list_meetings(data_dir)
            self.assertEqual(len(rows), 1)
            self.assertIn("Improve recall", rows[0]["title"])

    def test_listing_an_absent_council_directory_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(list_meetings(Path(raw) / "missing"), [])


class TierVerificationTests(unittest.TestCase):
    """A cloud flag is a wish; the runtime must check the tier can answer."""

    class StatusClient:
        def __init__(self, status):
            self.status = status
            self.closed = False

        def provider_status(self):
            return self.status

        def chat(self, *args, **kwargs):
            raise AssertionError("verify_tier must not call the model")

        def close(self):
            self.closed = True

    def _runtime(self, status, **config_overrides):
        config = _config(**config_overrides)
        return CouncilRuntime(config, client=self.StatusClient(status), models=resolve_models(config, {}))

    def test_a_signed_out_codex_profile_falls_back_to_local_with_the_login_hint(self):
        runtime = self._runtime(
            {"codex_cli_configured": True, "codex_cli_auth_method": "signed-out"},
            codex_cli_enabled=True,
        )
        self.assertEqual(runtime.models.mode, "codex-cli")
        note = runtime.verify_tier()
        self.assertIn("provider_setup --login codex", note)
        self.assertEqual(runtime.models.mode, "local")
        self.assertEqual(runtime.models.chair_model, "gpt-oss:20b")

    def test_a_chatgpt_login_keeps_the_codex_tier(self):
        runtime = self._runtime(
            {"codex_cli_configured": True, "codex_cli_auth_method": "chatgpt"},
            codex_cli_enabled=True,
        )
        self.assertEqual(runtime.verify_tier(), "")
        self.assertEqual(runtime.models.chair_model, "codex-cli:gpt-5.6-sol")

    def test_an_unknown_probe_result_does_not_demote_the_tier(self):
        runtime = self._runtime(
            {"codex_cli_configured": True, "codex_cli_auth_method": "unknown"},
            codex_cli_enabled=True,
        )
        self.assertEqual(runtime.verify_tier(), "")
        self.assertEqual(runtime.models.mode, "codex-cli")

    def test_a_missing_openai_client_falls_back(self):
        runtime = CouncilRuntime(
            _config(),
            client=self.StatusClient({"openai_configured": False}),
            models=resolve_models(_config(openai_api_enabled=True), {"OPENAI_API_KEY": "sk"}),
        )
        self.assertEqual(runtime.models.mode, "openai")
        self.assertTrue(runtime.verify_tier())
        self.assertEqual(runtime.models.mode, "local")

    def test_the_local_tier_needs_no_verification(self):
        runtime = self._runtime({})
        self.assertEqual(runtime.models.mode, "local")
        self.assertEqual(runtime.verify_tier(), "")


class NightSessionTests(unittest.TestCase):
    def test_the_plan_rebuilds_from_settings_without_trusting_a_field(self):
        plan = council.NightPlan.from_mapping({
            "enabled": True, "window": "22:00-06:30", "cap": 99, "depth": "Deep",
            "focus": "  ideas  ", "idle_seconds": 5,
        })
        self.assertTrue(plan.enabled)
        self.assertEqual(plan.window, "22:00-06:30")
        self.assertEqual(plan.cap, council.NIGHT_CAP_MAX)
        self.assertEqual(plan.depth, "Deep")
        self.assertEqual(plan.focus, "ideas")
        self.assertEqual(plan.idle_seconds, council.NIGHT_IDLE_MIN_SECONDS)
        bad = council.NightPlan.from_mapping({"window": "25:00-99:99", "cap": "x", "depth": "Huge", "focus": ""})
        self.assertEqual(bad.window, council.NIGHT_WINDOW_DEFAULT)
        self.assertEqual(bad.cap, council.NIGHT_CAP_DEFAULT)
        self.assertEqual(bad.depth, council.NIGHT_DEPTH_DEFAULT)
        self.assertEqual(bad.focus, council.NIGHT_FOCUS_DEFAULT)
        self.assertEqual(council.NightPlan.from_mapping("nonsense"), council.NightPlan())
        self.assertEqual(council.NightPlan.from_mapping(plan.as_dict()), plan)

    def test_windows_that_cross_midnight_are_one_night(self):
        from datetime import datetime

        window = "23:30-07:00"
        self.assertTrue(council.inside_window(window, datetime(2026, 9, 2, 23, 45)))
        self.assertTrue(council.inside_window(window, datetime(2026, 9, 3, 2, 0)))
        self.assertFalse(council.inside_window(window, datetime(2026, 9, 3, 7, 0)))
        self.assertFalse(council.inside_window(window, datetime(2026, 9, 3, 12, 0)))
        self.assertTrue(council.inside_window("09:00-17:00", datetime(2026, 9, 3, 12, 0)))
        self.assertFalse(council.inside_window("09:00-09:00", datetime(2026, 9, 3, 9, 0)))
        self.assertFalse(council.inside_window("garbage", datetime(2026, 9, 3, 9, 0)))
        self.assertEqual(council.night_key(window, datetime(2026, 9, 3, 2, 0)), "2026-09-02")
        self.assertEqual(council.night_key(window, datetime(2026, 9, 2, 23, 45)), "2026-09-02")
        self.assertEqual(council.night_key("09:00-17:00", datetime(2026, 9, 3, 12, 0)), "2026-09-03")

    def test_the_council_only_sits_when_every_gate_is_open(self):
        from datetime import datetime

        plan = council.NightPlan(enabled=True, window="23:30-07:00", cap=2, idle_seconds=600)
        night = datetime(2026, 9, 3, 1, 0)
        self.assertEqual(council.night_should_sit(plan, night, 900, False, 0), (True, "Ready to sit"))
        self.assertFalse(council.night_should_sit(council.NightPlan(), night, 900, False, 0)[0])
        self.assertFalse(council.night_should_sit(plan, night, 900, True, 0)[0])
        self.assertFalse(council.night_should_sit(plan, datetime(2026, 9, 3, 12, 0), 900, False, 0)[0])
        self.assertFalse(council.night_should_sit(plan, night, 900, False, 2)[0])
        allowed, reason = council.night_should_sit(plan, night, 100, False, 0)
        self.assertFalse(allowed)
        self.assertIn("idle minute", reason)

    def test_the_topic_is_read_back_from_a_messy_reply(self):
        self.assertEqual(council.parse_topic('"A morning briefing app"'), "A morning briefing app")
        self.assertEqual(council.parse_topic("TO: operator\n1. Topic: Downloads janitor\n"), "Downloads janitor")
        self.assertEqual(council.parse_topic("```\nWeekly planner that reads the calendar\n```"), "Weekly planner that reads the calendar")
        self.assertEqual(council.parse_topic(""), "")

    def test_the_topic_prompt_carries_focus_spark_and_recent_titles(self):
        plan = council.NightPlan(focus="apps for me")
        prompt = council.topic_prompt(plan, ["Downloads janitor"], "something playful")
        self.assertIn("apps for me", prompt)
        self.assertIn("something playful", prompt)
        self.assertIn("Downloads janitor", prompt)
        self.assertIn("do not repeat", prompt)

    def test_the_chair_picks_a_topic_and_a_bad_reply_falls_back_to_the_focus(self):
        import random

        plan = council.NightPlan(enabled=True, focus="apps for the operator")
        runtime = CouncilRuntime(_config(), client=ScriptedClient(["Downloads janitor that files by project"]), models=resolve_models(_config(), {}))
        topic, spark = runtime.pick_topic(plan, [], random.Random(1))
        self.assertEqual(topic, "Downloads janitor that files by project")
        self.assertIn(spark, council.SPARKS)
        runtime = CouncilRuntime(_config(), client=ScriptedClient([""]), models=resolve_models(_config(), {}))
        topic, _ = runtime.pick_topic(plan, [], random.Random(1))
        self.assertEqual(topic, "apps for the operator")

    def test_the_digest_lists_every_sitting_and_is_written_as_lf(self):
        meeting = open_meeting("Downloads janitor", DEPTH_PLANS["Brief"], 1_700_000_000.0)
        meeting.add_turn("coding", CHAIR_KEY, "member", "Use rules.\nPROPOSE: sort by project", 0)
        meeting.decision = "Build the janitor first."
        meeting.artifacts = {"folder": "x", "report": "x/report.md"}
        row = council.night_row(meeting)
        self.assertEqual(row["proposals"], ["sort by project"])
        text = council.night_digest_markdown("2026-09-02", [row], "apps for me")
        self.assertIn("night of 2026-09-02", text)
        self.assertIn("## 1. Downloads janitor", text)
        self.assertIn("Build the janitor first.", text)
        self.assertIn("- sort by project", text)
        self.assertIn("Nothing here was built", text)
        self.assertIn("did not sit", council.night_digest_markdown("2026-09-02", [], "apps"))
        with tempfile.TemporaryDirectory() as raw:
            path = council.write_night_digest(raw, "2026-09-02", [row], "apps for me")
            self.assertNotIn(b"\r\n", path.read_bytes())
            latest = council.latest_night_digest(raw)
            self.assertEqual(latest["night"], "2026-09-02")
            self.assertIn("Downloads janitor", latest["text"])
            self.assertIsNone(council.latest_night_digest(Path(raw) / "missing"))


class DesktopSurfaceTests(unittest.TestCase):
    """The council view's pure helpers; no Tk window is created."""

    def test_colour_helpers_blend_and_shade_within_range(self):
        from jarvis.ui import mix_colors, shade_color

        self.assertEqual(mix_colors("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(mix_colors("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(mix_colors("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(mix_colors("#000", "#fff", 1.0), "#ffffff")
        self.assertEqual(shade_color("#808080", 1.0), "#ffffff")
        self.assertEqual(shade_color("#808080", -1.0), "#000000")
        # Out-of-range input must clamp rather than raise.
        self.assertEqual(mix_colors("#000000", "#ffffff", 4.0), "#ffffff")
        self.assertEqual(mix_colors("not a colour", "#ffffff", 0.0), "#808080")

    def test_every_seat_has_a_place_at_the_table(self):
        from jarvis.ui import SEAT_ANGLES

        self.assertEqual(set(SEAT_ANGLES), {seat.key for seat in COUNCIL_SEATS})
        self.assertEqual(SEAT_ANGLES[CHAIR_KEY], -90.0)
        angles = sorted(SEAT_ANGLES.values())
        gaps = [b - a for a, b in zip(angles, angles[1:])]
        self.assertTrue(all(gap >= 20.0 for gap in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
