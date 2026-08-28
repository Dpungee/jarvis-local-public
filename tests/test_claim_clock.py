import unittest

from jarvis.claim_clock import (
    DEFAULT_HAZARD_PER_DAY,
    effective_confidence,
    estimate_hazard,
    protected_predicate,
    source_key,
)


class ClaimClockTests(unittest.TestCase):
    def test_hazard_fit_distinguishes_stable_and_volatile_predicates(self):
        stable = [(30.0, True, 0.9, 0.9) for _index in range(12)]
        volatile = [(1.0, False, 0.9, 0.9) for _index in range(12)]

        stable_hazard, stable_pairs = estimate_hazard(
            stable, vocabulary_size=8
        )
        volatile_hazard, volatile_pairs = estimate_hazard(
            volatile, vocabulary_size=8
        )

        self.assertEqual((stable_pairs, volatile_pairs), (12, 12))
        self.assertLess(stable_hazard, volatile_hazard)

    def test_sparse_fit_uses_conservative_prior(self):
        hazard, pairs = estimate_hazard(
            [(10.0, True, 0.9, 0.9)] * 5,
            vocabulary_size=4,
        )
        self.assertEqual(hazard, DEFAULT_HAZARD_PER_DAY)
        self.assertEqual(pairs, 5)

    def test_read_time_decay_moves_toward_ignorance_without_mutating_input(self):
        fresh = effective_confidence(
            0.95, hazard_per_day=0.1, elapsed_days=0, vocabulary_size=4
        )
        stale = effective_confidence(
            0.95, hazard_per_day=0.1, elapsed_days=100, vocabulary_size=4
        )
        protected = effective_confidence(
            0.95,
            hazard_per_day=0.1,
            elapsed_days=100,
            vocabulary_size=4,
            immutable=True,
        )
        self.assertEqual(fresh, 0.95)
        self.assertAlmostEqual(stale, 0.25, places=3)
        self.assertEqual(protected, 0.95)

    def test_source_identity_is_stable_non_reversible_and_predicates_are_exact(self):
        first = source_key("external", "https://status.example")
        self.assertEqual(first, source_key("external", "https://status.example"))
        self.assertNotEqual(first, source_key("external", "https://other.example"))
        self.assertNotIn("status.example", first)
        self.assertTrue(protected_predicate("preference:answer_style"))
        self.assertTrue(protected_predicate(" SAFETY:external publishing "))
        self.assertFalse(protected_predicate("employer"))


if __name__ == "__main__":
    unittest.main()
