---
name: network-engineering
description: Systematic network troubleshooting, packet analysis, architecture, segmentation, routing, performance, and safe change planning.
version: 1.0.0
---
# Network Engineering

## When to use

Use for LAN/WAN, routing, switching, wireless, DNS, DHCP, VPN, firewall, load-balancing, packet-capture, capacity, and network-security work.

## Workflow

1. Define the exact source, destination, direction, protocol, address/port, expected behavior, scope, onset, and last known good state.
2. Trace end to end: endpoint and name resolution; link, VLAN, and gateway; routing and symmetry; NAT/stateful policy or VPN; proxy/load balancer; destination listener and return path.
3. Localize the failure domain with minimally disruptive tests. Change one variable at a time and preserve before/after evidence.
4. For packet analysis, correlate flags, sequence/acknowledgment behavior, retransmissions, resets, latency, MTU/MSS, fragmentation, and capture vantage point.
5. For design, evaluate addressing overlap, convergence, loops, broadcast/failure domains, redundancy, capacity, QoS, observability, management-plane security, and blast radius.
6. Before a change, capture configuration and health, define maintenance sequencing and rollback, and identify monitoring thresholds.
7. After a change, verify data plane, control plane, failover, performance, telemetry, and the original user-visible symptom.

## Accuracy rules

Separate vendor-neutral intent from platform syntax. Confirm vendor, platform, software version, topology, and current configuration before exact commands. Prefer current IETF/IEEE standards and vendor documentation.

## Verification

State the evidence that would prove or disprove each leading hypothesis. Finish with expected results, rollback triggers, and residual uncertainty.
