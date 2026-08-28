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

    def test_cancellation_requires_explicit_operator_language(self):
        self.assertTrue(is_explicit_task_cancellation("Cancel that task."))
        self.assertTrue(is_explicit_task_cancellation("Actually, don't continue."))
        self.assertTrue(is_explicit_task_cancellation("Cancel the dentist appointment."))
        self.assertTrue(is_explicit_task_cancellation(
            "Actually, don't do anything yet.\nI need more time to review."
        ))
        for ordinary in (
            "thanks", "sounds good", "tell me more", "yes", "stop by the store",
            "cancel culture is interesting",
        ):
            with self.subTest(ordinary=ordinary):
                self.assertFalse(is_explicit_task_cancellation(ordinary))

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
            target="this",
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
                        grounding_texts=["Please make this."],
                    )

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

        redacted = build_task_contract_messages("My API_KEY=sk-proj-secret should stay private")
        self.assertNotIn("sk-proj-secret", redacted[1]["content"])

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
