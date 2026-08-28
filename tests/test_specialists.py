import unittest

from jarvis.specialists import (
    specialist_for_consultation_prompt,
    specialist_for_prompt,
)


def consultation(*, family: str, purpose: str, operator_task: str) -> str:
    return (
        "JARVIS specialist consultation (read-only; no mutations or process execution).\n"
        f"Assigned family: {family}. Specialist purpose: {purpose}.\n"
        "Work classification: Software code build and application implementation analysis.\n"
        "Independently analyze the operator task and report to JARVIS.\n"
        "<operator_task>\n"
        f"{operator_task}\n"
        "</operator_task>"
    )


class SpecialistRoutingTests(unittest.TestCase):
    def test_code_build_consultation_ignores_stale_network_context(self):
        prompt = consultation(
            family="code_build",
            purpose=(
                "software implementation, debugging, refactoring, and verification only"
            ),
            operator_task=(
                "Recent context: scan the home network and inspect the router.\n"
                "Current request: implement and test the desktop application."
            ),
        )

        selected = specialist_for_prompt(prompt)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.key, "coding")
        self.assertEqual(selected.name, "Forge")

    def test_consultation_metadata_must_match_the_registered_purpose(self):
        prompt = consultation(
            family="code_build",
            purpose="network architecture, diagnostics, and engineering analysis only",
            operator_task="scan the home network",
        )

        self.assertIsNone(specialist_for_consultation_prompt(prompt))
        self.assertEqual(specialist_for_prompt(prompt).key, "network")

    def test_ordinary_network_request_still_routes_to_relay(self):
        selected = specialist_for_prompt(
            "Analyze the OSPF routing design for my authorized home lab network."
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.key, "network")
        self.assertEqual(selected.name, "Relay")


if __name__ == "__main__":
    unittest.main()
