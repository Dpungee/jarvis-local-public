from __future__ import annotations

import re
from dataclasses import dataclass


_CYBERSECURITY_PATTERNS = (
    r"\b(?:cyber[ -]?secur(?:ity|uty)|networks? security|information security|infosec|security operations?|security analyst|SOC analyst)\b",
    r"\b(?:SIEM|SOAR|EDR|XDR|IDS|IPS|WAF|DLP|CASB|SASE)\b",
    r"\b(?:incident response|digital forensics?|threat hunt(?:ing)?|threat intelligence|threat model(?:ing)?|attack surface)\b",
    r"\b(?:security incident|security breach|data breach|compromised?|security alerts?|security posture|security controls?|cyber risk assessment)\b",
    r"\b(?:vulnerability management|vulnerability assessment|penetration test(?:ing)?|security assessment|security audit)\b",
    r"\bCVE-\d{4}-\d{4,}\b|\b(?:zero[- ]day|known exploited vulnerabilit(?:y|ies)|CISA KEV)\b",
    r"\b(?:malware|ransomware|phishing|botnet|rootkit|webshell|data exfiltration|lateral movement)\b",
    r"\b(?:identity and access management|IAM|least privilege|zero trust|secrets management|credential stuffing)\b",
    r"\b(?:authentication|authorization|access control|privilege escalation|security hardening|secure configuration)\b",
    r"\b(?:cryptograph(?:y|ic)|encryption|PKI|certificate authority|TLS|mTLS|certificate pinning)\b",
    r"\b(?:MITRE ATT&CK|TTPs?|indicators? of compromise|IOCs?|YARA|Sigma rule|detection engineering)\b",
    r"\b(?:OWASP|CWE-\d+|CVSS|SAST|DAST|SBOM|DevSecOps|application security|API security|cloud security|container security|endpoint security|email security|supply chain security)\b",
    r"\b(?:log analysis|memory forensics|disk forensics|network forensics)\b",
    r"\b(?:secure|harden|audit|assess)\s+(?:(?:my|the|our|an?)\s+)?(?:home\s+)?network\b",
    r"\b(?:firewalls?|network segmentation|microsegmentation|intrusion detection|intrusion prevention)\b",
)

_NETWORK_ENGINEERING_PATTERNS = (
    r"\b(?:network engineer(?:ing)?|network architecture|network topology|enterprise network)\b",
    r"\b(?:networks? security|network defense|network monitoring|network operations center|NOC)\b",
    r"\b(?:home network|campus network|data cent(?:er|re) network|LAN|WAN)\b",
    r"\b(?:TCP/IP|OSI model|IPv4|IPv6|CIDR|subnetting|supernetting|IPAM)\b",
    r"\b(?:BGP|OSPF|EIGRP|IS-IS|MPLS|VRF|route redistribution|routing table|asymmetric routing)\b",
    r"\b(?:VLAN|VXLAN|STP|RSTP|MSTP|LACP|port channel|switching loop|broadcast storm)\b",
    r"\b(?:DNS|DNSSEC|DHCP|NAT|PAT|ARP|NDP|ICMP)\b",
    r"\b(?:packet capture|PCAP|Wireshark|tcpdump|packet loss|retransmissions?|three-way handshake)\b",
    r"\b(?:VPN|IPsec|WireGuard|GRE tunnel|SD-WAN|load balancer|reverse proxy|forward proxy)\b",
    r"\b(?:QoS|MTU|MSS|latency|jitter|throughput|bandwidth|duplex mismatch)\b",
    r"\b(?:traceroute|tracert|pathping|netstat|nslookup|dig)\b",
    r"\b(?:firewalls?|security groups?|network ACLs?|port connectivity|socket states?)\b",
)

_LOCAL_NETWORK_POSTURE_PATTERNS = (
    r"\b(?:my|our|this)\b[^.!?\r\n]{0,50}\b(?:network|lan|wi[- ]?fi|router)\b"
    r"[^.!?\r\n]{0,100}\b(?:safe|secure|security|suspicious|unusual|unexpected|"
    r"threats?|risks?|anomal(?:y|ies)|compromis(?:e|ed)|protect|defend|posture)\b",
    r"\b(?:safe|secure|security|suspicious|unusual|unexpected|threats?|risks?|"
    r"anomal(?:y|ies)|compromis(?:e|ed)|protect|defend|assess|review)\b"
    r"[^.!?\r\n]{0,100}\b(?:my|our|this)\b[^.!?\r\n]{0,50}"
    r"\b(?:network|lan|wi[- ]?fi|router)\b",
    r"\b(?:do|does|did|have|has)\s+(?:i|we)\b[^.!?\r\n]{0,100}"
    r"\b(?:network|lan|wi[- ]?fi|router)\b[^.!?\r\n]{0,80}"
    r"\b(?:security|threats?|risks?|anomal(?:y|ies)|compromise)\b",
)

_CURRENT_SECURITY_PATTERNS = (
    r"\bCVE-\d{4}-\d{4,}\b",
    r"\b(?:CISA KEV|known exploited vulnerabilit(?:y|ies)|zero[- ]day|0[- ]day)\b",
    r"\b(?:latest|current|currently|today|recent|new|newly|active|in the wild)\b"
    r"[^.!?\r\n]{0,100}\b(?:vulnerabilit(?:y|ies)|exploit(?:ation)?|threat|campaign|malware|ransomware|advisory|patch|IOC|TTP)\b",
    r"\b(?:vulnerabilit(?:y|ies)|exploit(?:ation)?|threat|campaign|malware|ransomware|advisory|patch|IOC|TTP)\b"
    r"[^.!?\r\n]{0,100}\b(?:latest|current|currently|today|recent|new|newly|active|in the wild)\b",
)


@dataclass(frozen=True)
class SecurityExpertise:
    cybersecurity: bool
    network_engineering: bool
    local_network_posture: bool = False

    @property
    def active(self) -> bool:
        return self.cybersecurity or self.network_engineering

    @property
    def label(self) -> str:
        if self.cybersecurity and self.network_engineering:
            return "cybersecurity and network engineering"
        if self.cybersecurity:
            return "cybersecurity"
        if self.network_engineering:
            return "network engineering"
        return "general"


def classify_security_expertise(prompt: str) -> SecurityExpertise:
    text = re.sub(r"\bnetworks\s+security\b", "network security", str(prompt), flags=re.I)
    local_network_posture = any(
        re.search(pattern, text, re.I) for pattern in _LOCAL_NETWORK_POSTURE_PATTERNS
    )
    return SecurityExpertise(
        cybersecurity=local_network_posture or any(
            re.search(pattern, text, re.I) for pattern in _CYBERSECURITY_PATTERNS
        ),
        network_engineering=local_network_posture or any(
            re.search(pattern, text, re.I) for pattern in _NETWORK_ENGINEERING_PATTERNS
        ),
        local_network_posture=local_network_posture,
    )


def requires_current_security_research(prompt: str) -> bool:
    expertise = classify_security_expertise(prompt)
    return expertise.cybersecurity and any(
        re.search(pattern, str(prompt), re.I) for pattern in _CURRENT_SECURITY_PATTERNS
    )


def security_network_contract(prompt: str) -> str:
    expertise = classify_security_expertise(prompt)
    if not expertise.active:
        return ""

    shared = f"""For this {expertise.label} request, operate as a senior defensive specialist.
- Scope and authorization: help with defensive analysis, architecture, hardening, incident response, and explicitly authorized testing. Do not provide credential theft, phishing deployment, persistence, stealth/evasion, destructive payloads, or unauthorized exploitation/scanning. If a request mixes safe and unsafe work, complete the safe defensive portion.
- Evidence discipline: clearly separate observed facts, sourced facts, inferences, assumptions, and unknowns. Never invent packets, logs, topology, CVEs, ATT&CK technique IDs, command output, or successful tests. Ask for or propose the smallest evidence that would discriminate between competing hypotheses.
- Current facts: verify time-sensitive vulnerability, exploitation, advisory, and threat claims with current primary sources. Prefer vendor advisories and authoritative catalogs; treat CVSS as one input rather than the complete risk decision.
- Decisions: rank findings by exposure, exploitability, asset criticality, impact, compensating controls, and confidence. Give concrete remediation, validation, monitoring, rollback, and residual-risk steps rather than a generic checklist.
- Local network posture: when the operator asks about the paired home network, use the network_inventory security/status evidence before drawing conclusions. Treat every inventory anomaly as a hypothesis, state benign alternatives, and never turn reachability, a new address, a randomized MAC, or one model opinion into a compromise verdict or autonomous containment.
- Communication: adapt depth to the audience, state material assumptions up front, and provide an executive conclusion plus technically precise evidence when the task warrants both."""

    cyber = ""
    if expertise.cybersecurity:
        cyber = """
- Cybersecurity method: identify assets, identities, trust boundaries, threat actors, attack paths, controls, telemetry, and failure modes. Use NIST CSF 2.0, NIST SP 800-61 Rev. 3, MITRE ATT&CK, CISA KEV, and zero-trust principles only where they genuinely clarify the work; do not force framework mappings or fabricate identifiers.
- Incident and vulnerability work: preserve evidence and timestamps, build a defensible timeline, distinguish containment from eradication and recovery, prioritize known exploitation and reachable attack paths, and include detection opportunities and lessons learned."""

    network = ""
    if expertise.network_engineering:
        network = """
- Network method: reason end to end through client/endpoint, name resolution, link/VLAN, gateway and routing, NAT/stateful firewall or VPN, load balancer/proxy, and destination service. Track direction, address/port, protocol, state, route symmetry, MTU, and failure domain instead of guessing from one symptom.
- Network changes: check addressing overlap, route loops and convergence, broadcast and failure domains, redundancy, capacity, QoS, observability, blast radius, maintenance sequencing, configuration backup, rollback, and post-change verification. Troubleshoot with the least disruptive discriminating test and change one variable at a time.
- Protocol and configuration accuracy: prefer current IETF/IEEE standards and authoritative vendor documentation. Separate vendor-neutral design intent from platform syntax, confirm the device/OS and version before exact commands, and call out defaults that vary by implementation."""

    return "\n".join(part.strip() for part in (shared, cyber, network) if part.strip())
