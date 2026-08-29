import json
import unittest
from dataclasses import replace

from jarvis.task_contract import (
    MAX_RESOLVER_CONTEXT_CHARS,
    TASK_CONTRACT_RESPONSE_SCHEMA,
    MissingInput,
    TaskContract,
    TaskContractError,
    bind_provided_material_continuation,
    build_task_contract_messages,
    grounding_texts_for_resolution,
    is_explicit_task_cancellation,
    normalize_task_contract_response,
    parse_task_contract,
    reconcile_task_contract_continuation,
    task_contract_response_schema,
)


def payload(**changes):
    value = {
        "version": 1,
        "relation": "new",
        "lane": "research",
        "artifact_kind": "none",
        "evidence_source": "public_web",
        "requested_effect": "read",
        "goal": "compare current battery systems",
        "target": "battery systems",
        "constraint_quotes": ["current", "with official sources"],
        "missing_inputs": [],
        "acceptance": ["sources"],
    }
    value.update(changes)
    return value


GROUNDING = [
    "Please compare current battery systems with official sources and give me the tradeoffs."
]


class TaskContractTests(unittest.TestCase):
    def test_model_response_normalizer_canonicalizes_only_lane_derived_fields(self):
        research = payload(artifact_kind="document")
        normalized = normalize_task_contract_response(research)
        self.assertEqual(normalized["lane"], "research")
        self.assertEqual(normalized["artifact_kind"], "none")
        self.assertEqual(normalized["requested_effect"], "read")
        self.assertEqual(
            parse_task_contract(normalized, grounding_texts=GROUNDING).lane,
            "research",
        )

        external = payload(
            lane="external_action",
            artifact_kind="document",
            evidence_source="none",
            requested_effect="none",
            acceptance=["external_receipt"],
        )
        normalized = normalize_task_contract_response(external)
        self.assertEqual(normalized["artifact_kind"], "none")
        self.assertEqual(normalized["requested_effect"], "external")
        self.assertEqual(
            parse_task_contract(normalized, grounding_texts=GROUNDING).lane,
            "external_action",
        )

    def test_model_response_normalizer_recovers_structural_chat_only_creation(self):
        response_only = payload(
            lane="creation",
            artifact_kind="none",
            evidence_source="none",
            requested_effect="none",
            target=None,
            acceptance=["answer"],
        )
        normalized = normalize_task_contract_response(response_only)
        self.assertEqual(normalized["lane"], "dialogue")
        self.assertEqual(normalized["artifact_kind"], "none")
        self.assertEqual(
            parse_task_contract(normalized, grounding_texts=GROUNDING).lane,
            "dialogue",
        )

        persistent = payload(
            lane="creation",
            artifact_kind="document",
            evidence_source="none",
            requested_effect="write",
            target=None,
            acceptance=["artifact"],
        )
        self.assertEqual(
            normalize_task_contract_response(persistent)["lane"],
            "creation",
        )

    def test_persistent_document_may_use_public_web_evidence(self):
        prompt = (
            "Research conference-room display systems and put the findings into a Word document."
        )
        contract = parse_task_contract(
            payload(
                lane="creation",
                artifact_kind="document",
                evidence_source="public_web",
                requested_effect="write",
                goal=prompt,
                target="conference-room display systems",
                constraint_quotes=["Word document"],
                acceptance=["sources", "artifact"],
            ),
            grounding_texts=[prompt],
        )

        self.assertEqual(contract.lane, "creation")
        self.assertEqual(contract.evidence_source, "public_web")
        self.assertEqual(contract.requested_effect, "write")
        self.assertEqual(contract.acceptance, ("sources", "artifact"))

    def test_model_response_normalizer_grounds_goal_and_narrows_optional_target(self):
        ungrounded = payload(
            goal="a provider paraphrase",
            target="another provider paraphrase",
        )
        normalized = normalize_task_contract_response(
            ungrounded,
            grounding_texts=GROUNDING,
            canonical_goal=GROUNDING[0],
        )
        self.assertEqual(normalized["goal"], GROUNDING[0])
        self.assertIsNone(normalized["target"])
        self.assertEqual(
            parse_task_contract(normalized, grounding_texts=GROUNDING).lane,
            "research",
        )

        operational = payload(
            lane="external_action",
            artifact_kind="none",
            evidence_source="none",
            requested_effect="external",
            goal=GROUNDING[0],
            target="invented@example.com",
            acceptance=["external_receipt"],
        )
        normalized = normalize_task_contract_response(
            operational,
            grounding_texts=GROUNDING,
            canonical_goal=GROUNDING[0],
        )
        with self.assertRaisesRegex(TaskContractError, "target is not"):
            parse_task_contract(normalized, grounding_texts=GROUNDING)

    def test_model_response_normalizer_preserves_pending_goal_on_continuation(self):
        continued = payload(relation="continue")
        normalized = normalize_task_contract_response(
            continued,
            grounding_texts=[*GROUNDING, "Here are the figures: 1, 2, and 3."],
            canonical_goal="Here are the figures: 1, 2, and 3.",
            continued_goal="compare current battery systems",
        )
        self.assertEqual(normalized["goal"], "compare current battery systems")

    def test_normalizer_preserves_validated_pending_schema_on_continuation(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        current = "Keep going, and make the answer concise."
        normalized = normalize_task_contract_response(
            payload(
                relation="continue",
                lane="dialogue",
                artifact_kind="document",
                evidence_source="none",
                requested_effect="none",
                goal=current,
                target=None,
                constraint_quotes=["concise"],
                acceptance=["answer"],
            ),
            grounding_texts=[*GROUNDING, current],
            canonical_goal=current,
            continued_goal=pending.goal,
            operator_turn=current,
            pending_contract=pending,
        )

        self.assertEqual(normalized["lane"], pending.lane)
        self.assertEqual(normalized["requested_effect"], pending.requested_effect)
        self.assertEqual(normalized["evidence_source"], pending.evidence_source)
        self.assertEqual(normalized["target"], pending.target)
        self.assertEqual(
            normalized["constraint_quotes"],
            [*pending.constraint_quotes, "concise"],
        )
        self.assertEqual(normalized["acceptance"], ["sources"])

    def test_normalizer_canonicalizes_complete_acceptance_by_schema(self):
        cases = (
            (
                payload(
                    lane="dialogue",
                    evidence_source="provided",
                    requested_effect="none",
                    acceptance=["sources", "artifact"],
                ),
                ["answer"],
            ),
            (
                payload(acceptance=["answer", "artifact", "sources"]),
                ["sources"],
            ),
            (
                payload(
                    lane="external_action",
                    evidence_source="provided",
                    requested_effect="external",
                    acceptance=["answer", "external_receipt"],
                ),
                ["external_receipt"],
            ),
            (
                payload(
                    lane="creation",
                    artifact_kind="software",
                    evidence_source="workspace",
                    requested_effect="execute",
                    acceptance=["answer", "tests", "launch"],
                ),
                ["tests", "launch", "artifact"],
            ),
        )
        for raw, expected in cases:
            with self.subTest(lane=raw["lane"]):
                self.assertEqual(
                    normalize_task_contract_response(raw)["acceptance"],
                    expected,
                )

    def test_normalizer_marks_only_context_free_short_referents_incomplete(self):
        ambiguous = "Inspect that file."
        normalized = normalize_task_contract_response(
            payload(
                lane="inspection",
                evidence_source="workspace",
                goal=ambiguous,
                target="that file",
                constraint_quotes=[],
                acceptance=["answer"],
            ),
            grounding_texts=[ambiguous],
            canonical_goal=ambiguous,
            operator_turn=ambiguous,
        )
        self.assertEqual(normalized["missing_inputs"], [{"key": "target"}])

        grounded = "Inspect that file."
        with_context = normalize_task_contract_response(
            payload(
                lane="inspection",
                evidence_source="workspace",
                goal=grounded,
                target="that file",
                constraint_quotes=[],
                acceptance=["answer"],
            ),
            grounding_texts=["The build log is reports/build.log.", grounded],
            canonical_goal=grounded,
            operator_turn=grounded,
        )
        self.assertEqual(with_context["missing_inputs"], [])

        self.assertEqual(
            normalize_task_contract_response(
                payload(
                    lane="dialogue",
                    evidence_source="none",
                    requested_effect="none",
                    goal="Help me choose.",
                    target=None,
                    constraint_quotes=[],
                    acceptance=["answer"],
                ),
                grounding_texts=["Help me choose."],
                canonical_goal="Help me choose.",
                operator_turn="Help me choose.",
            )["missing_inputs"],
            [{"key": "target"}],
        )

        complete_dialogue = "I love it when plans are simple."
        self.assertEqual(
            normalize_task_contract_response(
                payload(
                    lane="dialogue",
                    evidence_source="none",
                    requested_effect="none",
                    goal=complete_dialogue,
                    target=None,
                    constraint_quotes=[],
                    acceptance=["answer"],
                ),
                grounding_texts=[complete_dialogue],
                canonical_goal=complete_dialogue,
                operator_turn=complete_dialogue,
            )["missing_inputs"],
            [],
        )

    def test_prompt_defines_generic_minimal_constraint_audit(self):
        system = build_task_contract_messages(
            "Create three concise examples by Friday."
        )[0]["content"]
        self.assertIn("complete minimal verification checklist", system)
        self.assertIn("quantity, time or recency", system)
        self.assertIn("shortest exact spans", system)
        self.assertIn("silently audit", system)

    def test_model_response_normalizer_canonicalizes_explicit_pending_cancellation(self):
        normalized = normalize_task_contract_response(
            payload(relation="continue"),
            grounding_texts=["nah drop it", *GROUNDING],
            canonical_goal="nah drop it",
            continued_goal="compare current battery systems",
            operator_turn="nah drop it",
        )
        contract = parse_task_contract(
            normalized,
            grounding_texts=["nah drop it", *GROUNDING],
            has_pending_goal=True,
        )
        self.assertEqual(contract.relation, "cancel")
        self.assertEqual(contract.lane, "dialogue")
        self.assertEqual(contract.requested_effect, "none")
        self.assertEqual(contract.acceptance, ("answer",))

    def test_model_response_normalizer_canonicalizes_generic_current_public_lookup(self):
        prompt = "yo whats the weather lookin like in 10001 today"
        normalized = normalize_task_contract_response(
            payload(
                lane="research",
                artifact_kind="none",
                evidence_source="public_web",
                requested_effect="none",
                goal=prompt,
                target=None,
                constraint_quotes=["10001", "today"],
                acceptance=[],
            ),
            grounding_texts=[prompt],
            canonical_goal=prompt,
            operator_turn=prompt,
        )
        contract = parse_task_contract(normalized, grounding_texts=[prompt])
        self.assertEqual(contract.lane, "research")
        self.assertEqual(contract.evidence_source, "public_web")
        self.assertEqual(contract.requested_effect, "read")
        self.assertIn("sources", contract.acceptance)

    def test_current_shape_does_not_override_nonpublic_inspection_evidence(self):
        prompt = "What is the current version of my private workspace app?"
        normalized = normalize_task_contract_response(
            payload(
                lane="inspection",
                artifact_kind="none",
                evidence_source="workspace",
                requested_effect="read",
                goal=prompt,
                target="my private workspace app",
                constraint_quotes=[],
                acceptance=["answer"],
            ),
            grounding_texts=[prompt],
            canonical_goal=prompt,
            operator_turn=prompt,
        )
        contract = parse_task_contract(normalized, grounding_texts=[prompt])
        self.assertEqual(contract.lane, "inspection")
        self.assertEqual(contract.evidence_source, "workspace")

    def test_current_shape_does_not_upgrade_workspace_research_to_public_web(self):
        prompt = "What is the latest version recorded in my private workspace notes?"
        normalized = normalize_task_contract_response(
            payload(
                lane="research",
                artifact_kind="none",
                evidence_source="workspace",
                requested_effect="read",
                goal=prompt,
                target="my private workspace notes",
                constraint_quotes=[],
                acceptance=["answer"],
            ),
            grounding_texts=[prompt],
            canonical_goal=prompt,
            operator_turn=prompt,
        )
        contract = parse_task_contract(normalized, grounding_texts=[prompt])
        self.assertEqual(contract.lane, "research")
        self.assertEqual(contract.evidence_source, "workspace")
        self.assertNotIn("sources", contract.acceptance)

    def test_private_local_target_rejects_model_claimed_public_web_evidence(self):
        prompts = (
            r"What is the latest version recorded in C:\Users\example\Private Roadmap.txt?",
            "What is the latest version recorded in private/roadmap.md?",
            'Summarize this excerpt: "What is the latest news today?"',
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                normalized = normalize_task_contract_response(
                    payload(
                        lane="research",
                        artifact_kind="none",
                        evidence_source="public_web",
                        requested_effect="read",
                        goal=prompt,
                        target=None,
                        constraint_quotes=[],
                        acceptance=["sources"],
                    ),
                    grounding_texts=[prompt],
                    canonical_goal=prompt,
                    operator_turn=prompt,
                )
                with self.assertRaisesRegex(TaskContractError, "public-web evidence"):
                    parse_task_contract(
                        normalized,
                        grounding_texts=[prompt],
                        current_operator_turn=prompt,
                    )

    def test_model_response_normalizer_uses_declared_provided_read_as_research(self):
        prompt = "Using only the pasted policy excerpts, compare their retention rules."
        normalized = normalize_task_contract_response(
            payload(
                lane="dialogue",
                artifact_kind="document",
                evidence_source="provided",
                requested_effect="read",
                goal=prompt,
                target=None,
                constraint_quotes=["only the pasted policy excerpts"],
                acceptance=[],
            ),
            grounding_texts=[prompt],
            canonical_goal=prompt,
            operator_turn=prompt,
        )
        contract = parse_task_contract(normalized, grounding_texts=[prompt])
        self.assertEqual(contract.lane, "research")
        self.assertEqual(contract.requested_effect, "read")
        self.assertEqual(contract.acceptance, ("answer",))

    def test_model_response_normalizer_drops_paraphrased_configuration_target(self):
        prompt = "Disable Public Presence and keep private Presence online."
        normalized = normalize_task_contract_response(
            payload(
                lane="configuration",
                artifact_kind="none",
                evidence_source="none",
                requested_effect="write",
                goal=prompt,
                target="Public Presence and private Presence",
                constraint_quotes=["Public Presence", "keep private Presence online"],
                acceptance=[],
            ),
            grounding_texts=[prompt],
            canonical_goal=prompt,
            operator_turn=prompt,
        )
        contract = parse_task_contract(normalized, grounding_texts=[prompt])
        self.assertIsNone(contract.target)
        self.assertEqual(contract.lane, "configuration")
        self.assertIn("answer", contract.acceptance)

    def test_model_response_normalizer_leaves_independent_invalid_fields_strict(self):
        malformed = payload(evidence_source="none")
        with self.assertRaisesRegex(TaskContractError, "research requires"):
            parse_task_contract(
                normalize_task_contract_response(malformed),
                grounding_texts=GROUNDING,
            )

    def test_valid_contract_is_frozen_and_clarification_is_derived(self):
        contract = parse_task_contract(payload(), grounding_texts=GROUNDING)

        self.assertEqual(contract.lane, "research")
        self.assertFalse(contract.needs_clarification)
        self.assertIsNone(contract.clarification_question)
        with self.assertRaises(AttributeError):
            contract.lane = "dialogue"

    def test_configuration_lane_is_bounded_to_catalog_state_reads_and_writes(self):
        prompt = "Show which optional capabilities are configured."
        status = payload(
            lane="configuration",
            artifact_kind="none",
            evidence_source="none",
            requested_effect="read",
            goal=prompt,
            target="optional capabilities",
            constraint_quotes=[],
            acceptance=["answer"],
        )
        self.assertEqual(
            parse_task_contract(status, grounding_texts=[prompt]).lane,
            "configuration",
        )

        decision_prompt = "Disable automatic paired-Bluetooth checks."
        decision = {
            **status,
            "requested_effect": "write",
            "goal": decision_prompt,
            "target": "automatic paired-Bluetooth checks",
        }
        self.assertEqual(
            parse_task_contract(decision, grounding_texts=[decision_prompt]).lane,
            "configuration",
        )

        for changes, expected in (
            ({"evidence_source": "computer"}, "evidence source"),
            ({"requested_effect": "execute"}, "reading or changing"),
            ({"requested_effect": "external"}, "reading or changing"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(TaskContractError, expected):
                    parse_task_contract(
                        {**status, **changes},
                        grounding_texts=[prompt],
                    )

    def test_resolver_prompt_defines_configuration_semantically_not_by_tool_name(self):
        messages = build_task_contract_messages(
            "Set up the optional capability I selected."
        )
        system = messages[0]["content"]

        self.assertIn("Configuration is only for viewing the state or setup plan", system)
        self.assertNotIn("feature_setup_status", system)
        self.assertNotIn("feature_setup_decide", system)

    def test_schema_is_strict_and_returned_as_a_defensive_copy(self):
        first = task_contract_response_schema()
        first["properties"]["lane"]["enum"].append("unbounded")
        second = task_contract_response_schema()

        self.assertFalse(TASK_CONTRACT_RESPONSE_SCHEMA["additionalProperties"])
        self.assertNotIn("unbounded", second["properties"]["lane"]["enum"])
        self.assertEqual(
            set(second["required"]), set(TASK_CONTRACT_RESPONSE_SCHEMA["required"])
        )
        for forbidden in ("tools", "tool_names", "permissions", "approval", "model"):
            self.assertNotIn(forbidden, second["properties"])

    def test_rejects_extra_missing_and_wrong_version_fields(self):
        with self.assertRaisesRegex(TaskContractError, "extra fields"):
            parse_task_contract(payload(permission=True), grounding_texts=GROUNDING)
        missing = payload()
        missing.pop("lane")
        with self.assertRaisesRegex(TaskContractError, "missing fields"):
            parse_task_contract(missing, grounding_texts=GROUNDING)
        with self.assertRaisesRegex(TaskContractError, "version"):
            parse_task_contract(payload(version=True), grounding_texts=GROUNDING)

    def test_goal_target_and_constraints_require_exact_user_quotes(self):
        for change, message in (
            ({"goal": "investigate lithium prices"}, "goal"),
            ({"target": "secret prototype"}, "target"),
            ({"constraint_quotes": ["under $500"]}, "constraint"),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(TaskContractError, message):
                    parse_task_contract(payload(**change), grounding_texts=GROUNDING)

    def test_new_task_rejects_constraint_from_an_unrelated_prior_turn(self):
        prior = "What do you think of community gardens? Keep that answer concise and friendly."
        current = "Which programs are currently open?"
        grounding = grounding_texts_for_resolution(
            current,
            recent_user_turns=[prior],
        )
        raw = payload(
            lane="inspection",
            artifact_kind="none",
            evidence_source="computer",
            requested_effect="read",
            goal=current,
            target="programs",
            constraint_quotes=["Keep that answer concise and friendly."],
            acceptance=["answer"],
        )

        with self.assertRaisesRegex(
            TaskContractError,
            "new-task constraint quote is not grounded in the current operator turn",
        ):
            parse_task_contract(raw, grounding_texts=grounding)

    def test_new_task_accepts_constraint_from_the_current_turn(self):
        prior = "Write the result as a long formal report."
        current = "Which programs are currently open? Keep the answer concise."
        grounding = grounding_texts_for_resolution(
            current,
            recent_user_turns=[prior],
        )
        contract = parse_task_contract(
            payload(
                lane="inspection",
                artifact_kind="none",
                evidence_source="computer",
                requested_effect="read",
                goal=current,
                target="programs",
                constraint_quotes=["Keep the answer concise."],
                acceptance=["answer"],
            ),
            grounding_texts=grounding,
        )

        self.assertEqual(contract.constraint_quotes, ("Keep the answer concise.",))

    def test_replace_target_and_constraints_require_current_turn_grounding(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        prior = "Inspect the archived telemetry and keep the answer exhaustive."
        current = "Inspect the live capture instead. Keep the answer brief."
        grounding = grounding_texts_for_resolution(
            current,
            pending_contract=pending,
            recent_user_turns=[prior],
        )
        base = payload(
            relation="replace",
            goal=current,
            target="live capture",
            constraint_quotes=["Keep the answer brief."],
        )

        with self.assertRaisesRegex(TaskContractError, "replacement target"):
            parse_task_contract(
                {**base, "target": "battery systems"},
                grounding_texts=grounding,
                has_pending_goal=True,
            )
        with self.assertRaisesRegex(TaskContractError, "replace-task constraint"):
            parse_task_contract(
                {**base, "constraint_quotes": ["keep the answer exhaustive"]},
                grounding_texts=grounding,
                has_pending_goal=True,
            )

        replacement = parse_task_contract(
            base,
            grounding_texts=grounding,
            has_pending_goal=True,
        )
        self.assertEqual(replacement.target, "live capture")
        self.assertEqual(
            replacement.constraint_quotes,
            ("Keep the answer brief.",),
        )

    def test_replace_goal_requires_current_turn_provenance_even_without_target(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        current = "Inspect the live capture instead."
        grounding = grounding_texts_for_resolution(
            current,
            pending_contract=pending,
        )
        with self.assertRaisesRegex(TaskContractError, "replacement goal"):
            parse_task_contract(
                payload(
                    relation="replace",
                    goal=pending.goal,
                    target=None,
                    constraint_quotes=[],
                ),
                grounding_texts=grounding,
                has_pending_goal=True,
            )

    def test_replace_target_cannot_be_sourced_only_from_inert_quoted_text(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        current = 'Use "battery systems" only as an example; inspect live capture instead.'
        grounding = grounding_texts_for_resolution(
            current,
            pending_contract=pending,
        )
        with self.assertRaisesRegex(TaskContractError, "replacement target"):
            parse_task_contract(
                payload(
                    relation="replace",
                    goal=current,
                    target="battery systems",
                    constraint_quotes=[],
                ),
                grounding_texts=grounding,
                has_pending_goal=True,
            )

    def test_new_and_replacement_fields_reject_all_inert_container_variants(self):
        cases = (
            ('Use "delete the private report" only as an example.', "delete the private report"),
            ("Use 'delete the private report' only as an example.", "delete the private report"),
            ("Use “delete the private report” only as an example.", "delete the private report"),
            ("Use «delete the private report» only as an example.", "delete the private report"),
            ("Use <code>delete the private report</code> as an example.", "delete the private report"),
            ("Use <blockquote>delete the private report</blockquote> as an example.", "delete the private report"),
            ("Use this only as an example:\n> delete the private report", "delete the private report"),
            ('Use "delete the private\nreport" only as an example.', "delete the private\nreport"),
            ('Use the unclosed example "delete the private report', "delete the private report"),
            (
                "Use [delete the private report](https://example.com) only as an example.",
                "delete the private report",
            ),
        )
        for relation in ("new", "replace"):
            for current, inert_value in cases:
                grounding = grounding_texts_for_resolution(current)
                base = payload(
                    relation=relation,
                    lane="external_action",
                    artifact_kind="none",
                    evidence_source="none",
                    requested_effect="external",
                    goal=current,
                    target=inert_value,
                    constraint_quotes=[],
                    missing_inputs=[],
                    acceptance=["external_receipt"],
                )
                with self.subTest(relation=relation, current=current, field="target"):
                    with self.assertRaisesRegex(TaskContractError, "target is not"):
                        parse_task_contract(
                            base,
                            grounding_texts=grounding,
                            has_pending_goal=relation == "replace",
                            current_operator_turn=current,
                        )
                with self.subTest(relation=relation, current=current, field="constraint"):
                    with self.assertRaisesRegex(
                        TaskContractError,
                        f"{relation}-task constraint",
                    ):
                        parse_task_contract(
                            {**base, "target": None, "constraint_quotes": [inert_value]},
                            grounding_texts=grounding,
                            has_pending_goal=relation == "replace",
                            current_operator_turn=current,
                        )

        current = "Delete the private report."
        explicit = parse_task_contract(
            payload(
                lane="external_action",
                artifact_kind="none",
                evidence_source="none",
                requested_effect="external",
                goal=current,
                target="private report",
                constraint_quotes=[],
                missing_inputs=[],
                acceptance=["external_receipt"],
            ),
            grounding_texts=grounding_texts_for_resolution(current),
            current_operator_turn=current,
        )
        self.assertEqual(explicit.target, "private report")

    def test_genuine_continuation_preserves_pending_and_current_constraints(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        current = "Do that again, but concise."
        grounding = grounding_texts_for_resolution(
            current,
            pending_contract=pending,
            recent_user_turns=[GROUNDING[0]],
        )
        continued = parse_task_contract(
            payload(
                relation="continue",
                constraint_quotes=["current", "with official sources", "concise"],
            ),
            grounding_texts=grounding,
            has_pending_goal=True,
        )

        reconciled = reconcile_task_contract_continuation(
            continued,
            pending_contract=pending,
            operator_turn=current,
        )
        self.assertEqual(
            reconciled.constraint_quotes,
            ("current", "with official sources", "concise"),
        )

    def test_direct_continuation_cannot_change_pending_goal(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        forged = replace(
            pending,
            relation="continue",
            goal="switch to an unrelated goal",
        )
        with self.assertRaisesRegex(TaskContractError, "pending goal"):
            reconcile_task_contract_continuation(
                forged,
                pending_contract=pending,
                operator_turn="switch to an unrelated goal",
            )

    def test_grounding_is_case_insensitive_but_not_fuzzy(self):
        result = parse_task_contract(
            payload(goal="Compare Current Battery Systems", target="BATTERY SYSTEMS"),
            grounding_texts=GROUNDING,
        )
        self.assertEqual(result.target, "BATTERY SYSTEMS")
        with self.assertRaisesRegex(TaskContractError, "exact quote"):
            parse_task_contract(
                payload(goal="compare the current battery systems"),
                grounding_texts=GROUNDING,
            )

    def test_material_missing_inputs_derive_one_bounded_question(self):
        raw = payload(
            lane="creation",
            artifact_kind="document",
            evidence_source="provided",
            requested_effect="write",
            goal="make a report",
            target="report",
            constraint_quotes=[],
            missing_inputs=[{"key": "source_material"}],
            acceptance=["artifact"],
        )
        contract = parse_task_contract(raw, grounding_texts=["Please make a report."])

        self.assertTrue(contract.needs_clarification)
        self.assertEqual(
            contract.clarification_question,
            "What should I use for the missing source material?",
        )
        self.assertEqual(contract.missing_inputs[0].key, "source_material")

    def test_missing_provided_source_is_inferred_from_contract_structure(self):
        for lane, artifact_kind, effect, acceptance in (
            ("creation", "document", "write", ["artifact"]),
            ("inspection", "none", "read", ["answer"]),
        ):
            with self.subTest(lane=lane):
                raw = payload(
                    lane=lane,
                    artifact_kind=artifact_kind,
                    evidence_source="provided",
                    requested_effect=effect,
                    goal="shape these notes",
                    target=None,
                    constraint_quotes=[],
                    missing_inputs=[],
                    acceptance=acceptance,
                )
                contract = parse_task_contract(
                    raw,
                    grounding_texts=["Shape these notes."],
                )

                self.assertTrue(contract.needs_clarification)
                self.assertEqual(
                    contract.missing_inputs,
                    (MissingInput("source_material"),),
                )
                self.assertEqual(
                    contract.clarification_question,
                    "What should I use for the missing source material?",
                )

    def test_deictic_provided_target_without_material_requires_source(self):
        for target in (
            "this",
            "this file",
            "that document",
            "attached file",
            "the attached file",
            "the upload",
            "the above",
            "what I attached",
            "these notes",
            "those images",
            "here",
        ):
            prompt = f"Make {target} into a polished PDF."
            with self.subTest(target=target):
                contract = parse_task_contract(
                    payload(
                        lane="creation",
                        artifact_kind="document",
                        evidence_source="provided",
                        requested_effect="write",
                        goal=prompt,
                        target=target,
                        constraint_quotes=[],
                        missing_inputs=[],
                        acceptance=["artifact"],
                    ),
                    grounding_texts=grounding_texts_for_resolution(prompt),
                    current_operator_turn=prompt,
                )
                self.assertEqual(
                    contract.missing_inputs,
                    (MissingInput("source_material"),),
                )

        supplied = "Make this file into a polished PDF:\nQuarterly sales rose 8%."
        contract = parse_task_contract(
            payload(
                lane="creation",
                artifact_kind="document",
                evidence_source="provided",
                requested_effect="write",
                goal=supplied,
                target="this file",
                constraint_quotes=[],
                missing_inputs=[],
                acceptance=["artifact"],
            ),
            grounding_texts=grounding_texts_for_resolution(supplied),
            current_operator_turn=supplied,
        )
        self.assertFalse(contract.missing_inputs)

    def test_unrelated_missing_key_cannot_stand_in_for_provided_source(self):
        contract = parse_task_contract(
            payload(
                lane="creation",
                artifact_kind="document",
                evidence_source="provided",
                requested_effect="write",
                goal="shape this into a guide",
                target=None,
                constraint_quotes=[],
                missing_inputs=[{"key": "format"}],
                acceptance=["artifact"],
            ),
            grounding_texts=["Shape this into a guide."],
        )

        self.assertEqual(
            contract.missing_inputs,
            (MissingInput("source_material"), MissingInput("format")),
        )

    def test_framed_continuation_satisfies_only_pending_source_material(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="creation",
            artifact_kind="document",
            evidence_source="provided",
            requested_effect="write",
            goal="shape these notes",
            target=None,
            constraint_quotes=(),
            missing_inputs=(MissingInput("source_material"),),
            acceptance=("artifact",),
        )
        continued = TaskContract(
            version=1,
            relation="continue",
            lane="creation",
            artifact_kind="document",
            evidence_source="none",
            requested_effect="write",
            goal="shape these notes",
            target=None,
            constraint_quotes=(),
            missing_inputs=(MissingInput("source_material"), MissingInput("format")),
            acceptance=("artifact",),
        )

        bound = bind_provided_material_continuation(
            continued,
            pending_contract=pending,
            operator_turn=(
                "Use these notes: verify inputs; preserve last-known-good data."
            ),
        )

        self.assertEqual(
            bound.target,
            "verify inputs; preserve last-known-good data.",
        )
        self.assertEqual(bound.missing_inputs, (MissingInput("format"),))
        self.assertEqual(bound.lane, continued.lane)
        self.assertEqual(bound.evidence_source, "provided")
        self.assertEqual(bound.requested_effect, continued.requested_effect)

    def test_unframed_or_noncontinuation_text_cannot_clear_missing_source(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="inspection",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="read",
            goal="inspect the sample",
            target=None,
            constraint_quotes=(),
            missing_inputs=(MissingInput("source_material"),),
            acceptance=("answer",),
        )
        continued = replace(pending, relation="continue")
        for relation, turn in (
            ("continue", "I do not have it yet."),
            ("replace", "Use this: replacement payload"),
            ("new", "Use this: initial payload"),
        ):
            with self.subTest(relation=relation, turn=turn):
                candidate = replace(continued, relation=relation)
                self.assertEqual(
                    bind_provided_material_continuation(
                        candidate,
                        pending_contract=pending,
                        operator_turn=turn,
                    ),
                    candidate,
                )

    def test_source_binding_revalidates_final_acceptance_and_rejects_control_text(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="creation",
            artifact_kind="document",
            evidence_source="provided",
            requested_effect="write",
            goal="shape these notes",
            target=None,
            constraint_quotes=(),
            missing_inputs=(MissingInput("source_material"),),
            acceptance=(),
        )
        continued = replace(pending, relation="continue")
        with self.assertRaisesRegex(TaskContractError, "artifact"):
            bind_provided_material_continuation(
                continued,
                pending_contract=pending,
                operator_turn="Use these notes: validate every input boundary.",
            )
        self.assertEqual(
            bind_provided_material_continuation(
                continued,
                pending_contract=pending,
                operator_turn="Actually: don't do anything yet.",
            ),
            continued,
        )
        self.assertEqual(
            bind_provided_material_continuation(
                continued,
                pending_contract=pending,
                operator_turn=(
                    "Actually, don't do anything yet.\nI need more time to review."
                ),
            ),
            continued,
        )

    def test_continuation_cannot_drop_missing_inputs_or_change_authority(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="inspection",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="read",
            goal="assess the telemetry",
            target="telemetry",
            constraint_quotes=(),
            missing_inputs=(MissingInput("capture"),),
            acceptance=("answer",),
        )
        omitted = replace(pending, relation="continue", missing_inputs=())
        with self.assertRaisesRegex(TaskContractError, "did not ground"):
            reconcile_task_contract_continuation(
                omitted,
                pending_contract=pending,
                operator_turn="thanks",
            )
        unrelated = replace(
            omitted,
            target="I was thinking about dogs.",
            constraint_quotes=("I was thinking about dogs.",),
        )
        with self.assertRaisesRegex(TaskContractError, "did not ground"):
            reconcile_task_contract_continuation(
                unrelated,
                pending_contract=pending,
                operator_turn="I was thinking about dogs.",
            )

        natural = replace(
            omitted,
            target="midnight capture",
            constraint_quotes=("midnight capture",),
        )
        natural_result = reconcile_task_contract_continuation(
            natural,
            pending_contract=pending,
            operator_turn="midnight capture",
        )
        self.assertFalse(natural_result.needs_clarification)
        self.assertEqual(natural_result.target, "midnight capture")

        for control in ("yes", "thanks"):
            with self.subTest(control=control):
                control_contract = replace(
                    omitted,
                    target=control,
                    constraint_quotes=(control,),
                )
                with self.assertRaisesRegex(TaskContractError, "did not ground"):
                    reconcile_task_contract_continuation(
                        control_contract,
                        pending_contract=pending,
                        operator_turn=control,
                    )

        for nonanswer in (
            "I do not know the capture",
            "capture: not sure",
            "capture: unknown",
            'capture: "midnight capture"',
            "capture: `midnight capture`",
        ):
            with self.subTest(nonanswer=nonanswer):
                nonanswer_contract = replace(
                    omitted,
                    target=("not sure" if "not sure" in nonanswer else nonanswer),
                    constraint_quotes=(
                        "not sure" if "not sure" in nonanswer else nonanswer,
                    ),
                )
                with self.assertRaisesRegex(TaskContractError, "did not ground"):
                    reconcile_task_contract_continuation(
                        nonanswer_contract,
                        pending_contract=pending,
                        operator_turn=nonanswer,
                    )

        for control in ("cancel it", "never mind"):
            with self.subTest(control=control):
                cancelled = reconcile_task_contract_continuation(
                    omitted,
                    pending_contract=pending,
                    operator_turn=control,
                )
                self.assertEqual(cancelled.relation, "cancel")
                self.assertEqual(cancelled.lane, "dialogue")
                self.assertFalse(cancelled.missing_inputs)

        grounded = replace(
            omitted,
            target="midnight capture",
            constraint_quotes=("midnight capture",),
        )
        result = reconcile_task_contract_continuation(
            grounded,
            pending_contract=pending,
            operator_turn="capture: midnight capture",
        )
        self.assertFalse(result.needs_clarification)
        self.assertEqual(result.target, "midnight capture")

        for changed in (
            replace(omitted, lane="dialogue", requested_effect="none"),
            replace(omitted, requested_effect="external"),
            replace(omitted, artifact_kind="document"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(TaskContractError, "continuation may not"):
                    reconcile_task_contract_continuation(
                        changed,
                        pending_contract=pending,
                        operator_turn="Use the midnight capture.",
                    )

        two_missing = replace(
            pending,
            missing_inputs=(MissingInput("capture"), MissingInput("format")),
        )
        both_omitted = replace(
            grounded,
            missing_inputs=(),
        )
        with self.assertRaisesRegex(TaskContractError, "did not ground"):
            reconcile_task_contract_continuation(
                both_omitted,
                pending_contract=two_missing,
                operator_turn="Use the midnight capture.",
            )
        keyed = replace(
            both_omitted,
            constraint_quotes=("midnight capture", "plain text"),
        )
        resolved = reconcile_task_contract_continuation(
            keyed,
            pending_contract=two_missing,
            operator_turn="capture: midnight capture\nformat: plain text",
        )
        self.assertFalse(resolved.needs_clarification)

    def test_missing_input_keys_reject_common_high_risk_pii_aliases(self):
        for key in (
            "social_security_no",
            "taxpayer_id",
            "passport_number",
            "drivers_license",
            "date_of_birth",
            "bank_iban",
            "credit_card_pan",
            "medical_record_number",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                TaskContractError,
                "secrets or credentials",
            ):
                parse_task_contract(
                    payload(missing_inputs=[{"key": key}], acceptance=[]),
                    grounding_texts=GROUNDING,
                )

    def test_single_missing_input_rejects_unrelated_short_grounded_phrases(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="inspection",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="read",
            goal="assess the telemetry",
            target="telemetry",
            constraint_quotes=(),
            missing_inputs=(MissingInput("capture"),),
            acceptance=("answer",),
        )
        for phrase in (
            "dogs",
            "blue sky",
            "midnight",
            "the other one",
            "yard work",
            "looks good",
        ):
            with self.subTest(phrase=phrase):
                forged = replace(
                    pending,
                    relation="continue",
                    target=phrase,
                    constraint_quotes=(phrase,),
                    missing_inputs=(),
                )
                with self.assertRaisesRegex(TaskContractError, "did not ground"):
                    reconcile_task_contract_continuation(
                        forged,
                        pending_contract=pending,
                        operator_turn=phrase,
                    )

    def test_single_missing_input_accepts_keyed_or_typed_exact_answers_only(self):
        capture_pending = TaskContract(
            version=1,
            relation="new",
            lane="inspection",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="read",
            goal="assess the telemetry",
            target="telemetry",
            constraint_quotes=(),
            missing_inputs=(MissingInput("capture"),),
            acceptance=("answer",),
        )
        for turn, value in (
            ("capture: midnight capture", "midnight capture"),
            ("the capture is midnight capture", "midnight capture"),
            ("midnight capture for the capture", "midnight capture"),
            ("telemetry.pcap", "telemetry.pcap"),
        ):
            with self.subTest(turn=turn):
                continued = replace(
                    capture_pending,
                    relation="continue",
                    target=value,
                    constraint_quotes=(value,),
                    missing_inputs=(),
                )
                resolved = reconcile_task_contract_continuation(
                    continued,
                    pending_contract=capture_pending,
                    operator_turn=turn,
                )
                self.assertFalse(resolved.needs_clarification)

        format_pending = TaskContract(
            version=1,
            relation="new",
            lane="creation",
            artifact_kind="document",
            evidence_source="provided",
            requested_effect="write",
            goal="shape the supplied notes",
            target="supplied notes",
            constraint_quotes=(),
            missing_inputs=(MissingInput("format"),),
            acceptance=("artifact",),
        )
        for turn in ("PDF", "plain text", "format: markdown"):
            with self.subTest(turn=turn):
                value = turn.partition(":")[2].strip() or turn
                continued = replace(
                    format_pending,
                    relation="continue",
                    constraint_quotes=(value,),
                    missing_inputs=(),
                )
                resolved = reconcile_task_contract_continuation(
                    continued,
                    pending_contract=format_pending,
                    operator_turn=turn,
                )
                self.assertFalse(resolved.needs_clarification)

        count_pending = replace(
            format_pending,
            missing_inputs=(MissingInput("item_count"),),
        )
        count_continued = replace(
            count_pending,
            relation="continue",
            constraint_quotes=("3",),
            missing_inputs=(),
        )
        for turn in ("item count: 3", "item_count=3", "3"):
            with self.subTest(turn=turn):
                resolved = reconcile_task_contract_continuation(
                    count_continued,
                    pending_contract=count_pending,
                    operator_turn=turn,
                )
                self.assertFalse(resolved.needs_clarification)

    def test_cancellation_requires_explicit_operator_language(self):
        self.assertTrue(is_explicit_task_cancellation("Cancel that task."))
        self.assertTrue(is_explicit_task_cancellation("Actually, don't continue."))
        self.assertTrue(is_explicit_task_cancellation("Cancel the dentist appointment."))
        self.assertTrue(is_explicit_task_cancellation("Stop working on the task."))
        self.assertTrue(is_explicit_task_cancellation(
            "Actually, don't do anything yet.\nI need more time to review."
        ))
        for ordinary in (
            "thanks", "sounds good", "tell me more", "yes", "stop by the store",
            "cancel culture is interesting",
            "Don't do the destructive step, continue with the read-only audit.",
            "Do not proceed without tests, but keep working.",
            "Don't continue using that API; use the local fixture.",
            "Don't do this in production; explain the safe approach instead.",
        ):
            with self.subTest(ordinary=ordinary):
                self.assertFalse(is_explicit_task_cancellation(ordinary))

    def test_cancellation_is_turn_wide_but_quoted_and_code_examples_are_inert(self):
        for explicit in (
            "Keep the intro unchanged.\nCancel that task.",
            "One more thing: actually, don't continue.",
            "Review the note first; please cancel that task.",
            "The deadline is Friday.\nNever mind.\nWe can discuss it later.",
            "Status update.\n- Stop that task.\nThanks.",
        ):
            with self.subTest(explicit=explicit):
                self.assertTrue(is_explicit_task_cancellation(explicit))

        for inert in (
            'Explain why the phrase "cancel that task" is unambiguous.',
            "Explain why the phrase “cancel that task” is unambiguous.",
            "Write 'never mind' in the heading.",
            "Use `cancel that task` as the button label.",
            "Example:\n```text\ncancel that task\n```\nKeep working.",
            "Example:\n    cancel that task\nKeep working.",
            "Use <code>cancel that task</code> as the example.",
            "> cancel that task\nExplain the quoted instruction.",
            "The words cancel culture are not an instruction.",
        ):
            with self.subTest(inert=inert):
                self.assertFalse(is_explicit_task_cancellation(inert))

    def test_explicit_cancellation_dominates_relation_and_line_order(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="inspection",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="read",
            goal="assess the telemetry",
            target="telemetry",
            constraint_quotes=(),
            missing_inputs=(MissingInput("capture"),),
            acceptance=("answer",),
        )
        model_continuation = replace(pending, relation="continue", missing_inputs=())
        model_replacement = replace(
            pending,
            relation="replace",
            missing_inputs=(),
        )
        for turn in (
            "Cancel that task.\nThe capture would have been midnight.",
            "The capture would have been midnight.\nCancel that task.",
            "The capture would have been midnight.\nCancel that task.\nThanks.",
        ):
            with self.subTest(turn=turn):
                cancelled = reconcile_task_contract_continuation(
                    model_continuation,
                    pending_contract=pending,
                    operator_turn=turn,
                )
                self.assertEqual(cancelled.relation, "cancel")
                self.assertEqual(cancelled.lane, "dialogue")
                self.assertFalse(cancelled.missing_inputs)

        replaced_by_cancel = reconcile_task_contract_continuation(
            model_replacement,
            pending_contract=pending,
            operator_turn="This would otherwise be a replacement. Cancel that task.",
        )
        self.assertEqual(replaced_by_cancel.relation, "cancel")

        with self.assertRaisesRegex(TaskContractError, "did not ground"):
            reconcile_task_contract_continuation(
                model_continuation,
                pending_contract=pending,
                operator_turn='The example says "cancel that task."',
            )

        forged_cancel = TaskContract(
            version=1,
            relation="cancel",
            lane="dialogue",
            artifact_kind="none",
            evidence_source="none",
            requested_effect="none",
            goal=pending.goal,
            target=None,
            constraint_quotes=(),
            missing_inputs=(),
            acceptance=("answer",),
        )
        with self.assertRaisesRegex(TaskContractError, "explicit operator"):
            reconcile_task_contract_continuation(
                forged_cancel,
                pending_contract=pending,
                operator_turn='The documentation says "cancel that task."',
            )

    def test_multiple_missing_inputs_are_combined_without_losing_fields(self):
        raw = payload(
            lane="external_action",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="external",
            goal="schedule the review",
            target="review",
            constraint_quotes=[],
            missing_inputs=[
                {"key": "attendees"},
                {"key": "time"},
            ],
            acceptance=["external_receipt"],
        )
        contract = parse_task_contract(raw, grounding_texts=["Schedule the review."])

        self.assertEqual(
            contract.clarification_question,
            "What should I use for the missing attendees and time?",
        )

    def test_clarification_keys_are_bounded_unique_and_non_sensitive(self):
        base = payload(
            lane="creation",
            artifact_kind="other",
            evidence_source="provided",
            requested_effect="write",
            goal="make this",
            target="provided sample",
            constraint_quotes=[],
            acceptance=["artifact"],
        )
        for missing, expected in (
            ([{"key": "Bad-Key"}], "lowercase"),
            ([
                {"key": "target"},
                {"key": "target"},
            ], "unique"),
            ([{"key": "password"}], "secrets or credentials"),
            ([{"key": "api_key"}], "secrets or credentials"),
            ([{"key": "target", "question": "Which one?"}], "semantic key"),
        ):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(TaskContractError, expected):
                    parse_task_contract(
                        {**base, "missing_inputs": missing},
                        grounding_texts=["Please make this from the provided sample."],
                    )

    def test_collapsed_credential_keys_are_rejected_without_benign_key_false_positives(self):
        base = payload(
            lane="creation",
            artifact_kind="other",
            evidence_source="provided",
            requested_effect="write",
            goal="make this",
            target="provided sample",
            constraint_quotes=[],
            acceptance=["artifact"],
        )
        sensitive = (
            "apikey",
            "authcode",
            "clientsecret",
            "client_secret",
            "secretkey",
            "signingkey",
            "signing_key",
            "sshkey",
            "ssh_key",
            "credentialfile",
            "accesskey",
            "privatekey",
            "oauthtoken",
            "oauth_token",
            "access_token",
            "password_hint",
            "recoverycode",
            "recovery_code",
            "walletseed",
            "wallet_seed",
            "seedphrase",
            "mnemonic",
            "mnemonic_phrase",
            "pin",
            "otp",
            "totp_code",
            "mfa_code",
            "verification_code",
            "account_pin",
            "credit_card_number",
            "credit_card_expiry",
            "login_code",
            "one_time_code",
            "bank_account_number",
            "social_security_number",
            "unlock_code",
            "device_passcode",
            "cvv",
            "cc_number",
            "card_no",
            "acct_number",
            "account_no",
            "routing_no",
            "tax_id",
            "gov_id",
            "birth_day",
            "login_otp",
            "mfa_pin",
            "two_factor_pin",
            "cvv2",
            "cvc2",
            "debit_pin",
            "unlock_pattern",
            "routing_num",
            "govt_id",
            "federal_id",
            "license_no",
            "dl_number",
            "health_id",
            "patient_id",
            "medicare_number",
            "medicaid_id",
            "insurance_policy_number",
            "twofa_code",
            "second_factor_code",
            "login_mfa",
            "mfa_otp",
            "verification_otp",
            "security_answer",
            "challenge_answer",
            "mother_maiden_name",
            "backup_phrase",
            "seed_words",
            "wallet_words",
            "card_exp",
            "expiry",
            "bank_account_id",
            "account_identifier",
            "social_id",
            "authentication_code",
            "authentication_pin",
            "authn_code",
            "two_step_code",
            "session_id",
            "session_identifier",
            "bearer_value",
            "transit_number",
            "sort_code",
            "licence_no",
            "healthcare_id",
            "policy_no",
            "sec_answer",
            "mothers_maiden",
            "banking_account_num",
            "account_reference",
        )
        for key in sensitive:
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    TaskContractError,
                    "secrets or credentials",
                ):
                    parse_task_contract(
                        {**base, "missing_inputs": [{"key": key}]},
                        grounding_texts=["Please make this from the provided sample."],
                    )

        for key in (
            "keyboard_layout",
            "keynote_theme",
            "client_name",
            "signing_algorithm",
            "ssh_host",
            "wallet_address",
            "random_seed",
            "pin_location",
            "pin_size",
            "otp_delivery_method",
            "verification_method",
            "security_level",
            "backup_schedule",
            "account_name",
            "login_method",
            "device_name",
            "time_zone",
            "bank_name",
            "card_theme",
            "unlock_method",
            "secretary_name",
            "email",
            "location",
            "mfa_delivery_channel",
            "otp_provider",
            "client_id",
            "project_id",
            "delivery_pin",
            "map_pin",
            "two_step_verification",
        ):
            with self.subTest(key=key):
                contract = parse_task_contract(
                    {**base, "missing_inputs": [{"key": key}]},
                    grounding_texts=["Please make this from the provided sample."],
                )
                self.assertEqual(contract.missing_inputs, (MissingInput(key),))

    def test_resolver_schema_cannot_return_user_facing_prose(self):
        dialogue = payload(
            lane="dialogue",
            artifact_kind="none",
            evidence_source="none",
            requested_effect="none",
            goal="what do you think of community gardens",
            target="community gardens",
            constraint_quotes=[],
            acceptance=["answer"],
        )
        contract = parse_task_contract(
            dialogue, grounding_texts=["What do you think of community gardens?"]
        )
        self.assertFalse(hasattr(contract, "direct_response"))
        schema = task_contract_response_schema()
        self.assertNotIn("direct_response", schema["properties"])

        with self.assertRaisesRegex(TaskContractError, "extra fields"):
            parse_task_contract(
                {**dialogue, "direct_response": "Model-authored answer."},
                grounding_texts=["What do you think of community gardens?"],
            )
        with self.assertRaisesRegex(TaskContractError, "semantic key"):
            parse_task_contract(
                {**dialogue, "missing_inputs": [{
                    "key": "topic",
                    "question": "What is your password?",
                }]},
                grounding_texts=["What do you think of community gardens?"],
            )

    def test_relation_is_bounded_to_same_conversation_pending_state(self):
        continuation = payload(relation="continue")
        with self.assertRaisesRegex(TaskContractError, "require a pending goal"):
            parse_task_contract(continuation, grounding_texts=GROUNDING)
        result = parse_task_contract(
            continuation, grounding_texts=GROUNDING, has_pending_goal=True
        )
        self.assertEqual(result.relation, "continue")
        with self.assertRaisesRegex(TaskContractError, "replace or continue"):
            parse_task_contract(payload(), grounding_texts=GROUNDING, has_pending_goal=True)

    def test_consistency_rules_reject_lane_effect_and_evidence_confusion(self):
        cases = (
            (payload(lane="dialogue"), "dialogue"),
            (payload(lane="inspection"), "inspection"),
            (payload(lane="creation", artifact_kind="none"), "artifact kind"),
            (payload(artifact_kind="document"), "only creation"),
            (payload(lane="external_action"), "external effect"),
            (payload(requested_effect="external"), "external-action"),
            (payload(lane="research", evidence_source="none"), "research requires"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(TaskContractError, expected):
                    parse_task_contract(raw, grounding_texts=GROUNDING)

        # The contract is descriptive only. Exact resources are still selected
        # and approval-gated at tool execution, so a broad external goal need
        # not invent a target merely to survive semantic classification.
        broad_external = parse_task_contract(
            payload(
                lane="external_action",
                artifact_kind="none",
                evidence_source="none",
                requested_effect="external",
                target=None,
                acceptance=["external_receipt"],
            ),
            grounding_texts=GROUNDING,
        )
        self.assertIsNone(broad_external.target)

    def test_acceptance_minimums_can_only_strengthen_runtime_evidence(self):
        with self.assertRaisesRegex(TaskContractError, "sources"):
            parse_task_contract(payload(acceptance=["answer"]), grounding_texts=GROUNDING)
        creation = payload(
            lane="creation",
            artifact_kind="software",
            evidence_source="provided",
            requested_effect="execute",
            goal="build a calculator",
            target="calculator",
            constraint_quotes=[],
            acceptance=["tests"],
        )
        with self.assertRaisesRegex(TaskContractError, "artifact"):
            parse_task_contract(creation, grounding_texts=["Build a calculator."])

    def test_incomplete_contract_defers_source_and_final_evidence_until_ready(self):
        research = payload(
            evidence_source="none",
            acceptance=["answer"],
            missing_inputs=[{"key": "research_topic"}],
        )
        contract = parse_task_contract(research, grounding_texts=GROUNDING)
        self.assertTrue(contract.needs_clarification)

        inspection = payload(
            lane="inspection",
            evidence_source="none",
            requested_effect="read",
            acceptance=["answer"],
            missing_inputs=[{"key": "target"}],
        )
        contract = parse_task_contract(inspection, grounding_texts=GROUNDING)
        self.assertTrue(contract.needs_clarification)

        with self.assertRaisesRegex(TaskContractError, "research requires"):
            parse_task_contract(
                {**research, "missing_inputs": []},
                grounding_texts=GROUNDING,
            )
        with self.assertRaisesRegex(TaskContractError, "inspection is limited"):
            parse_task_contract(
                {**inspection, "missing_inputs": []},
                grounding_texts=GROUNDING,
            )

    def test_rejects_malformed_oversized_and_duplicate_arrays(self):
        for raw, expected in (
            ("not-json", "valid JSON"),
            (json.dumps([1, 2, 3]), "object"),
            (payload(constraint_quotes=["current", "CURRENT"]), "duplicates"),
            (payload(acceptance=["sources"] * 5), "exceeds 4"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(TaskContractError, expected):
                    parse_task_contract(raw, grounding_texts=GROUNDING)
        with self.assertRaisesRegex(TaskContractError, "20,000"):
            parse_task_contract("{" + " " * 20_001, grounding_texts=GROUNDING)

    def test_prompt_builder_is_tool_free_bounded_redacted_and_domain_general(self):
        messages = build_task_contract_messages(
            "Please analyze the unfamiliar object and decide what is materially missing.",
            recent_user_turns=["older", "middle", "newest"],
            latest_assistant_context="Earlier I described object sapphire-17.",
        )

        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertLessEqual(
            len(messages[1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0]),
            MAX_RESOLVER_CONTEXT_CHARS,
        )
        self.assertIn("no tools", messages[0]["content"])
        self.assertIn("broad lane", messages[0]["content"])
        self.assertNotIn("shopping", messages[0]["content"].casefold())
        context = json.loads(messages[1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertEqual(context["recent_user_turns"], ["middle", "newest"])
        self.assertEqual(
            context["latest_assistant_context"],
            "Earlier I described object sapphire-17.",
        )
        self.assertIn("never operator grounding", messages[0]["content"])

        redacted = build_task_contract_messages(
            "My API_KEY=EXAMPLE_NOT_A_REAL_KEY should stay private"
        )
        self.assertNotIn("EXAMPLE_NOT_A_REAL_KEY", redacted[1]["content"])

    def test_prompt_builder_preserves_pending_contract_but_drops_old_turns_first(self):
        pending = TaskContract(
            version=1,
            relation="new",
            lane="creation",
            artifact_kind="document",
            evidence_source="provided",
            requested_effect="write",
            goal="create the report",
            target="report",
            constraint_quotes=("PDF",),
            missing_inputs=(MissingInput("source"),),
            acceptance=("artifact",),
        )
        messages = build_task_contract_messages(
            "Use the attached figures.",
            pending_contract=pending,
            recent_user_turns=["x" * 800, "y" * 800],
        )
        context = json.loads(messages[1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0])

        self.assertEqual(context["pending_task_contract"]["target"], "report")
        self.assertLessEqual(
            len(messages[1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0]),
            MAX_RESOLVER_CONTEXT_CHARS,
        )

    def test_grounding_helper_retains_only_user_authored_contract_quotes(self):
        pending = parse_task_contract(payload(), grounding_texts=GROUNDING)
        values = grounding_texts_for_resolution(
            "Add a comparison table.",
            pending_contract=pending,
            recent_user_turns=["Earlier user constraint."],
        )

        self.assertIn("compare current battery systems", values)
        self.assertIn("battery systems", values)
        self.assertIn("current", values)
        self.assertIn("Add a comparison table.", values)
        self.assertNotIn("Assistant-only invented target.", values)


if __name__ == "__main__":
    unittest.main()
