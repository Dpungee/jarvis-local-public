from __future__ import annotations

import json
import unittest

from jarvis.memory_proposer import (
    build_proposer_messages,
    ground_proposal,
    parse_proposer_response,
    predicate_grounded,
    proposal_grounded,
    proposer_response_schema,
)

STATEMENT = "the Kestrel relay got migrated over to Harrier box"


def _raw(subject, predicate, value, span=STATEMENT):
    return json.dumps(
        {"subject": subject, "predicate": predicate, "value": value, "source_span": span}
    )


class MemoryProposerTests(unittest.TestCase):
    def test_grounded_proposal_passes_and_is_parser_validated(self) -> None:
        proposal = ground_proposal(
            _raw("Kestrel relay", "host", "Harrier box"),
            STATEMENT,
            known_predicates=["deployed on host"],
        )
        self.assertEqual(
            proposal, {"subject": "Kestrel relay", "predicate": "host", "value": "Harrier box"}
        )
        # A fenced answer is tolerated; whitespace and case are normalized.
        fenced = "```json\n" + _raw("kestrel relay", "host", "harrier  box") + "\n```"
        proposal = ground_proposal(fenced, STATEMENT, known_predicates=["deployed on host"])
        assert proposal is not None
        self.assertEqual(proposal["value"], "Harrier box")

    def test_anything_outside_the_operators_words_is_dropped(self) -> None:
        cases = {
            "value invented": _raw("Kestrel relay", "host", "Osprey box"),
            "subject invented": _raw("Osprey relay", "host", "Harrier box"),
            "span invented": _raw("Kestrel relay", "host", "Harrier box", "the relay is on Harrier box"),
            "predicate from nowhere": _raw("Kestrel relay", "datacenter", "Harrier box"),
            "nulls": json.dumps({"subject": None, "predicate": None, "value": None, "source_span": None}),
            "missing key": json.dumps({"subject": "Kestrel relay", "predicate": "host", "value": "Harrier box"}),
            "extra nesting": json.dumps({"subject": ["Kestrel relay"], "predicate": "host", "value": "Harrier box", "source_span": STATEMENT}),
            "not json": "Kestrel relay -> host -> Harrier box",
            "too long": "x" * 5_000,
            # The governed parser still has the last word.
            "secret value": json.dumps({"subject": "Kestrel relay", "predicate": "token", "value": "sk-live-abcdefghijklmnopqrstuvwxyz", "source_span": "the Kestrel relay token is sk-live-abcdefghijklmnopqrstuvwxyz"}),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                statement = STATEMENT
                if label == "secret value":
                    statement = "the Kestrel relay token is sk-live-abcdefghijklmnopqrstuvwxyz"
                self.assertIsNone(
                    ground_proposal(raw, statement, known_predicates=["deployed on host"])
                )

    def test_whole_token_and_whole_phrase_grounding(self) -> None:
        # A partial token never grounds.
        self.assertIsNone(
            parse_proposer_response(_raw("Kestrel relay", "host", "arrier box"), STATEMENT)
        )
        # A one-word subject that is the tail of a longer name never grounds.
        osprey = "the Osprey relay got migrated over to Harrier box"
        self.assertIsNone(
            parse_proposer_response(_raw("relay", "host", "Harrier box", osprey), osprey)
        )
        # ... but a determiner-led one-word subject does.
        bare = "the relay got migrated over to Harrier box"
        fields = parse_proposer_response(_raw("relay", "host", "Harrier box", bare), bare)
        assert fields is not None
        self.assertEqual(fields["subject"], "relay")
        # A value only present in a ruled-out clause never grounds.
        for statement in (
            STATEMENT + ", not Talon box",
            STATEMENT + " instead of Talon box",
            STATEMENT + " because Talon box died",
            STATEMENT + "; the Osprey relay stays on Talon box",
        ):
            with self.subTest(statement=statement):
                self.assertIsNone(
                    parse_proposer_response(
                        _raw("Kestrel relay", "host", "Talon box", statement), statement
                    )
                )
        # The operator's own spelling is kept, not the model's.
        fields = parse_proposer_response(
            _raw("kestrel relay", "host", "HARRIER BOX"), STATEMENT
        )
        assert fields is not None
        self.assertEqual((fields["subject"], fields["value"]), ("Kestrel relay", "Harrier box"))
        # ß and ss stay distinct, as in the governed parser's NFKC view.
        self.assertIsNone(
            parse_proposer_response(
                _raw("Kestrel relay", "host", "Grossbox", "the Kestrel relay moved to Großbox"),
                "the Kestrel relay moved to Großbox",
            )
        )

    def test_parse_step_checks_span_subject_and_value_only(self) -> None:
        fields = parse_proposer_response(_raw("Kestrel relay", "host", "Harrier box"), STATEMENT)
        self.assertEqual(
            fields,
            {
                "subject": "Kestrel relay",
                "predicate": "host",
                "value": "Harrier box",
                "source_span": STATEMENT,
            },
        )
        # An ungrounded predicate survives the parse step; grounding is the
        # caller's job once the subject alias is known.
        self.assertIsNotNone(
            parse_proposer_response(_raw("Kestrel relay", "datacenter", "Harrier box"), STATEMENT)
        )
        self.assertIsNone(
            parse_proposer_response(_raw("Kestrel relay", "host", "Osprey box"), STATEMENT)
        )

    def test_predicate_grounding_uses_statement_and_known_predicates(self) -> None:
        self.assertTrue(predicate_grounded("migrated over to", STATEMENT))
        self.assertFalse(predicate_grounded("host", STATEMENT))
        self.assertTrue(predicate_grounded("host", STATEMENT, ["deployed on host"]))
        self.assertTrue(predicate_grounded("listen port", "the relay listens on port 9191"))
        self.assertFalse(predicate_grounded("", STATEMENT))
        self.assertTrue(
            proposal_grounded(
                {"subject": "Kestrel relay", "predicate": "host", "value": "Harrier box"},
                STATEMENT,
                known_predicates=["deployed on host"],
            )
        )
        self.assertFalse(
            proposal_grounded(
                {"subject": "Kestrel relay", "predicate": "host", "value": "Osprey box"},
                STATEMENT,
                known_predicates=["deployed on host"],
            )
        )

    def test_messages_are_bounded_and_carry_hints(self) -> None:
        messages = build_proposer_messages(
            STATEMENT,
            known_subjects=[f"Subject{i}" for i in range(20)],
            known_predicates=[f"zq{i}" for i in range(20)],
        )
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        user = messages[1]["content"]
        self.assertIn("Sentence: " + STATEMENT, user)
        self.assertEqual(user.count("Subject"), 8)
        self.assertEqual(user.count("zq"), 12)
        schema = proposer_response_schema()
        self.assertEqual(sorted(schema["required"]), ["predicate", "source_span", "subject", "value"])
        schema["required"].clear()
        self.assertEqual(len(proposer_response_schema()["required"]), 4)


if __name__ == "__main__":
    unittest.main()
