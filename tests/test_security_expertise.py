import unittest

from jarvis.specialists import specialist_for_prompt
from jarvis.security_expertise import (
    classify_security_expertise,
    requires_current_security_research,
    security_network_contract,
)


class SecurityExpertiseTests(unittest.TestCase):
    def test_domain_classifier_covers_defensive_security_and_networking(self):
        cases = {
            "Map this incident timeline to MITRE ATT&CK": (True, False),
            "Prioritize this CVE using CISA KEV and asset exposure": (True, False),
            "Investigate a security incident on a compromised endpoint": (True, False),
            "Design BGP policy, VRFs, and redundant IPsec tunnels": (False, True),
            "Investigate packet loss and TCP retransmissions in this PCAP": (False, True),
            "Help me secure my home network and diagnose the WAN latency": (True, True),
            "Review firewall segmentation and diagnose asymmetric routing": (True, True),
            "Provide advanced defensive cybersecurity and analyze VLAN routing policy": (True, True),
            "Improve defensive cybersecurity monitoring for our systems": (True, False),
            "Is there anything suspicious on my network?": (True, True),
            "Has my network been compromised?": (True, True),
            "Review my paired home network security posture": (True, True),
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                match = classify_security_expertise(prompt)
                self.assertEqual(
                    (match.cybersecurity, match.network_engineering),
                    expected,
                )

    def test_classifier_rejects_ambiguous_everyday_language(self):
        for prompt in (
            "Return my security deposit",
            "Create a social network marketing plan",
            "Explain a neural network",
            "Networking at a job fair is awkward",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(classify_security_expertise(prompt).active)
                self.assertEqual(security_network_contract(prompt), "")

    def test_freshness_detection_is_narrow_and_fail_safe(self):
        for prompt in (
            "Assess CVE-2026-12345",
            "Is this zero-day being exploited in the wild?",
            "What is the current ransomware threat campaign?",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(requires_current_security_research(prompt))
        for prompt in (
            "Explain what a zero trust architecture is",
            "Teach me how OSPF areas work",
            "Analyze this static firewall configuration",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(requires_current_security_research(prompt))

    def test_contract_requires_defensive_scope_and_operational_rigor(self):
        contract = security_network_contract(
            "Perform incident response and redesign the VLAN and firewall architecture"
        )
        for requirement in (
            "explicitly authorized testing",
            "unauthorized exploitation/scanning",
            "smallest evidence that would discriminate",
            "exploitability, asset criticality, impact",
            "containment from eradication and recovery",
            "destination service",
            "least disruptive discriminating test",
            "change one variable at a time",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, contract)

    def test_cyber_specialist_can_build_only_when_agent_runtime_unlocks_tools(self):
        specialist = specialist_for_prompt(
            "Build an isolated firewall simulator and adversarially test it."
        )
        self.assertIsNotNone(specialist)
        self.assertEqual(specialist.key, "cybersecurity")
        self.assertTrue({"write_file", "edit_file", "run_process"}.issubset(
            specialist.tool_allowlist
        ))


if __name__ == "__main__":
    unittest.main()
