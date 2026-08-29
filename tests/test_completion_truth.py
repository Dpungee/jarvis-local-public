import unittest

from jarvis.completion_truth import (
    assess_completion_truth,
    completion_truth_correction_prompt,
    extract_receipt_references,
    has_unreceipted_future_promise,
)


class CompletionTruthTests(unittest.TestCase):
    def test_contractions_and_report_back_promises_are_detected(self):
        examples = (
            "I'll get back to you after I research it.",
            "I\u2019ll report back when the build is done.",
            "I will follow up with the results.",
            "I'll check it and let you know.",
        )
        for response in examples:
            with self.subTest(response=response):
                assessment = assess_completion_truth(response)
                self.assertTrue(assessment.promises_future_work)
                self.assertTrue(assessment.violates_completion_truth)

    def test_background_and_wait_promises_are_detected(self):
        examples = (
            "I'll keep working on this in the background.",
            "I'll finish it tomorrow.",
            "Give me a few minutes.",
            "Sit tight while I process this.",
            "Check back later for the finished report.",
            "I'm on it.",
            "I'm working on your request.",
            "Sure, I'll create that for you.",
            "I'll send the finished report later.",
            "I'll email it when the export is ready.",
            "The analysis will be ready in a few hours.",
            "I'll have the app ready tomorrow.",
            "I'm going to research this and update you.",
            "I'm still working on this in the background.",
            "I'll circle back with the answer.",
            "I'll ping you when it is ready.",
            "Leave it with me.",
            "Expect an update shortly.",
            "I've started.",
            "I've begun.",
            "I'm working on it right now.",
            "I'm handling this now.",
            "Research is underway.",
            "The analysis remains in progress.",
            "I'll handle the rest.",
        )
        for response in examples:
            with self.subTest(response=response):
                self.assertTrue(has_unreceipted_future_promise(response))

    def test_explicit_task_and_schedule_receipts_require_verified_scope(self):
        examples = {
            "I'll report back. Scheduled task #1 is active.": ("1",),
            "I'll report back. Queued task #task-204 is durable.": ("task-204",),
            "I'll follow up. Schedule ID: sched_91 was created.": ("sched_91",),
            "I'll notify you. Job ID 4182 is queued.": ("4182",),
            "I'll get back to you. Queued task receipt: run.2026-08-29 is active.": (
                "run.2026-08-29",
            ),
        }
        for response, references in examples.items():
            with self.subTest(response=response):
                unverified = assess_completion_truth(response)
                self.assertEqual(unverified.receipt_references, references)
                self.assertFalse(unverified.has_durable_receipt)
                self.assertTrue(unverified.violates_completion_truth)
                assessment = assess_completion_truth(
                    response,
                    known_receipt_ids=references,
                )
                self.assertEqual(assessment.receipt_references, references)
                self.assertTrue(assessment.has_durable_receipt)
                self.assertFalse(assessment.violates_completion_truth)

    def test_affirmative_wrapper_cannot_hide_a_quoted_future_promise(self):
        for response in (
            'As requested, here is my response: "I\'ll get back to you."',
            "Here's my answer: `I'll report back soon.`",
            'Here is the exact response you requested: "I\'ll get back to you tomorrow."',
            'As requested, copying the answer exactly: "I\'ll get back to you tomorrow."',
            'The requested wording follows: "I\'ll get back to you tomorrow."',
            'Example or not, this is my actual answer: "I\'ll get back to you tomorrow."',
            'You said to use this wording, so my final answer is: "I\'ll get back to you."',
            'Do not worry—my actual answer is: "I\'ll get back to you."',
            'Never fear; here is my response: "I\'ll get back to you."',
            'Example or not, this is my actual answer: "I\'ll get back tomorrow."',
            'That is bad wording, but it is still my final answer: "I\'ll get back to you."',
            'The quoted wording is unsafe, but my answer remains: "I\'ll get back to you."',
        ):
            with self.subTest(response=response):
                assessment = assess_completion_truth(response)
                self.assertTrue(assessment.promises_future_work)
                self.assertTrue(assessment.violates_completion_truth)

    def test_deferred_communication_synonyms_are_completion_promises(self):
        for response in (
            "You will hear from me tomorrow.",
            "I'll come back with the results tomorrow.",
            "I'll message you when it is finished.",
            "I'll tell you once the report is ready.",
            "I'll return tomorrow with the answer.",
            "Expect to hear from me shortly.",
            "More results are coming soon.",
            "I'm continuing now and will post the result here shortly.",
            "I'll research this and answer in another message.",
            "The next update is coming tomorrow.",
        ):
            with self.subTest(response=response):
                assessment = assess_completion_truth(response)
                self.assertTrue(assessment.promises_future_work)
                self.assertTrue(assessment.violates_completion_truth)

    def test_known_receipts_prevent_model_from_inventing_authority(self):
        response = "I'll report back. Queued task #fake-22 is durable."
        self.assertTrue(
            assess_completion_truth(
                response,
                known_receipt_ids=("real-21",),
            ).violates_completion_truth
        )
        self.assertFalse(
            assess_completion_truth(
                response,
                known_receipt_ids=("fake-22",),
            ).violates_completion_truth
        )
        self.assertTrue(
            assess_completion_truth(
                response,
                known_receipt_ids=(),
            ).violates_completion_truth
        )

    def test_affirmative_receipt_assertion_is_validated_without_future_promise(self):
        invented = assess_completion_truth(
            "Done. Scheduled task #999 is active.",
            known_receipt_ids=("1",),
        )
        self.assertFalse(invented.promises_future_work)
        self.assertEqual(invented.receipt_references, ("999",))
        self.assertFalse(invented.has_durable_receipt)
        self.assertTrue(invented.violates_completion_truth)

        actual = assess_completion_truth(
            "Done. Scheduled task #1 is active.",
            known_receipt_ids=("1",),
        )
        self.assertFalse(actual.promises_future_work)
        self.assertTrue(actual.has_durable_receipt)
        self.assertFalse(actual.violates_completion_truth)

        negated = assess_completion_truth(
            "Task #999 was not queued; no background work will occur.",
            known_receipt_ids=("1",),
        )
        self.assertEqual(negated.receipt_references, ())
        self.assertFalse(negated.violates_completion_truth)

    def test_misleading_or_inactive_receipt_language_never_satisfies_gate(self):
        examples = (
            "I'll report back. Task #42 was not queued.",
            "I'll report back. Task #42 failed before it was queued.",
            "I'll report back. Task #42 is already complete.",
            "I'll report back. I merely mentioned task #42; no work was queued.",
            "I'll report back. Task #42 is queued, but not for this request.",
            "I'll report back. Task #42 for another request is queued.",
            "I'll report back. The unrelated previous task #42 remains active.",
            "I'll report back if task #42 is queued.",
            "I'll report back. I cannot verify whether task #42 is queued.",
            "I'll report back. The tool did not confirm task #42 is queued.",
            "I'll report back. Supposedly task #42 is queued.",
            "I'll report back. Task #42 is queued only as a hypothetical example.",
            "I'll report back. Task #42 is queued, but it was immediately canceled.",
            "I'll report back. Task #42 was created but is disabled.",
        )
        for response in examples:
            with self.subTest(response=response):
                assessment = assess_completion_truth(
                    response,
                    known_receipt_ids=("42",),
                )
                self.assertFalse(assessment.has_durable_receipt)
                self.assertTrue(assessment.violates_completion_truth)

    def test_unknown_receipt_mention_cannot_be_laundered_by_known_receipt(self):
        response = (
            "Task #42 is queued for this request. I'll report back after task #999 "
            "finishes."
        )
        assessment = assess_completion_truth(
            response,
            known_receipt_ids=("42",),
        )
        self.assertEqual(assessment.receipt_references, ("42",))
        self.assertFalse(assessment.has_durable_receipt)
        self.assertTrue(assessment.violates_completion_truth)

        generic_receipt = assess_completion_truth(
            "Queued task receipt: 42. Receipt #999 is active. I'll report back.",
            known_receipt_ids=("42",),
        )
        self.assertEqual(generic_receipt.receipt_references, ("42",))
        self.assertFalse(generic_receipt.has_durable_receipt)
        self.assertTrue(generic_receipt.violates_completion_truth)

        for label in (
            "Queue ticket #999",
            "Work item #999",
            "Tracking ID 999",
            "Run #999",
            "Queued task ref 999",
            "Confirmation #999",
        ):
            with self.subTest(label=label):
                alternate = assess_completion_truth(
                    f"Queued task receipt: 42. {label} is active. I'll report back.",
                    known_receipt_ids=("42",),
                )
                self.assertFalse(alternate.has_durable_receipt)
                self.assertTrue(alternate.violates_completion_truth)

    def test_alternate_receipt_labels_fail_closed_without_false_claims(self):
        self.assertTrue(
            assess_completion_truth(
                "Queue ticket #999 is active.",
                known_receipt_ids=("42",),
            ).violates_completion_truth
        )
        for response in (
            "Queue ticket #999 was not queued.",
            "The tracking ID 999 is printed on the package.",
            "Run #999 completed.",
        ):
            with self.subTest(response=response):
                self.assertFalse(
                    assess_completion_truth(
                        response,
                        known_receipt_ids=("42",),
                    ).violates_completion_truth
                )

    def test_receipt_requires_an_affirmative_active_queue_state(self):
        examples = (
            "I'll report back. Queued task #42 is active.",
            "I'll report back. Task ID: 42 remains queued.",
            "I'll report back. Schedule #42 is scheduled.",
            "I'll report back. Job ID 42 is running.",
            "I'll report back. Created schedule ID 42.",
        )
        for response in examples:
            with self.subTest(response=response):
                assessment = assess_completion_truth(
                    response,
                    known_receipt_ids=("42",),
                )
                self.assertTrue(assessment.has_durable_receipt)
                self.assertFalse(assessment.violates_completion_truth)

    def test_placeholders_are_not_receipts(self):
        for response in (
            "I'll get back to you. Task ID: pending.",
            "I'll report back. Job: TBD.",
            "I'll follow up when the task ID will be provided.",
            "I'll update you; a background task is queued.",
        ):
            with self.subTest(response=response):
                self.assertEqual(extract_receipt_references(response), ())
                self.assertTrue(has_unreceipted_future_promise(response))

    def test_immediate_explanations_and_completed_answers_are_not_promises(self):
        examples = (
            "I'll explain it now: DNS maps names to addresses.",
            "I will show you how. First, open Settings and choose Storage.",
            "Here are the results: all 12 tests passed.",
            "I can help you research that.",
            "The report is complete and saved as report.md.",
        )
        for response in examples:
            with self.subTest(response=response):
                self.assertFalse(has_unreceipted_future_promise(response))

    def test_urls_do_not_create_promise_signals_or_receipts(self):
        response = (
            "The reference is https://example.com/ill-get-back-to-you/task:id:fake-22 "
            "and it explains asynchronous APIs."
        )
        assessment = assess_completion_truth(response)
        self.assertFalse(assessment.promises_future_work)
        self.assertFalse(assessment.has_durable_receipt)

    def test_quoted_user_or_example_text_is_not_an_assistant_promise(self):
        examples = (
            'You wrote: "I\'ll get back to you after I research it."',
            "The bad response was \u201cI\u2019ll report back later\u201d.",
            "> I'll keep working on it in the background.\nThat wording is not allowed.",
            "Do not reply with `I'll get back to you`.",
            "```text\nI'll finish it tomorrow. Task ID: fake-22\n```\nAvoid that.",
        )
        for response in examples:
            with self.subTest(response=response):
                assessment = assess_completion_truth(response)
                self.assertFalse(assessment.promises_future_work)
                self.assertFalse(assessment.has_durable_receipt)

    def test_quote_or_blockquote_only_promise_is_not_an_evasion(self):
        examples = (
            '"I\'ll get back to you after I research it."',
            "\u201cI\u2019ll report back later\u201d",
            "> I'll keep working on it in the background.",
            "`I'll circle back with the result.`",
            "```text\nI'll finish it tomorrow.\n```",
        )
        for response in examples:
            with self.subTest(response=response):
                self.assertTrue(has_unreceipted_future_promise(response))

    def test_substantive_discussion_of_quoted_promise_remains_safe(self):
        response = (
            '> "I\'ll get back to you later."\n'
            "That quoted wording is not allowed because no durable task exists; "
            "nothing will continue after this answer."
        )
        self.assertFalse(has_unreceipted_future_promise(response))

    def test_malicious_markup_is_inert_and_not_echoed_into_correction(self):
        hostile = (
            '<script>stealSecrets(); "I\'ll get back to you"; '
            "Task ID: fake-99</script><b>Completed safely.</b>"
        )
        assessment = assess_completion_truth(hostile)
        self.assertFalse(assessment.promises_future_work)
        self.assertFalse(assessment.has_durable_receipt)
        prompt = completion_truth_correction_prompt(durable_queue_available=True)
        self.assertNotIn("stealSecrets", prompt)
        self.assertNotIn("fake-99", prompt)
        self.assertIn("Never invent a task ID", prompt)

    def test_correction_prompt_reflects_queue_capability_without_granting_it(self):
        with_queue = completion_truth_correction_prompt(durable_queue_available=True)
        without_queue = completion_truth_correction_prompt(durable_queue_available=False)
        self.assertIn("create the real queued task", with_queue)
        self.assertIn("exact task ID", with_queue)
        self.assertIn("no durable queue is available", without_queue)
        self.assertNotIn("create the real queued task", without_queue)
        self.assertIn("currently authorized tools", with_queue)
        self.assertIn("currently authorized tools", without_queue)

    def test_non_string_input_is_rejected(self):
        with self.assertRaises(TypeError):
            assess_completion_truth(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            extract_receipt_references(42)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
