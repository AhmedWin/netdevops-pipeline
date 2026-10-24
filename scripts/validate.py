#!/usr/bin/env python3
"""
Check the intent before anything is built from it.

Every problem found here is one that would otherwise cost a full lab
build to discover - or worse, not be discovered at all. This runs in
well under a second and needs no containers, no routers, no network.

    python3 scripts/validate.py

Exit code 0 if the intent is sound, 1 if it is not.
"""

import ipaddress
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
INTENT = REPO / "intent" / "network.yml"
TOPOLOGY = REPO / "lab" / "topology.clab.yml"

# Point-to-point links should be /31 (RFC 3021) or /30. Anything wider on
# a p2p link is almost always a mistake, and anything narrower is invalid.
P2P_PREFIXES = {30, 31}


class Report:
    """Collects problems so we can show all of them, not just the first."""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.checks = []

    def error(self, check, message):
        self.errors.append((check, message))

    def warn(self, check, message):
        self.warnings.append((check, message))

    def record(self, check, summary):
        self.checks.append((check, summary))

    @property
    def ok(self):
        return not self.errors

    def to_dict(self):
        """JSON-friendly form, for the portal API."""
        return {
            "ok": self.ok,
            "checks": [{"name": n, "summary": s} for n, s in self.checks],
            "errors": [{"check": n, "message": m} for n, m in self.errors],
            "warnings": [{"check": n, "message": m} for n, m in self.warnings],
        }


# ---------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------

def check_references(intent, report):
    """Every link must point at devices that actually exist."""
    devices = set(intent.get("devices", {}))
    links = intent.get("links", [])

    for i, link in enumerate(links, 1):
        for side in ("a", "b"):
            name = link.get(side, {}).get("device")
            if name not in devices:
                report.error(
                    "references",
                    f"link {i} references unknown device '{name}' "
                    f"(known: {', '.join(sorted(devices))})",
                )
        if link.get("a", {}).get("device") == link.get("b", {}).get("device"):
            report.error("references", f"link {i} connects {link['a']['device']} to itself")

    report.record("references", f"{len(links)} links, {len(devices)} devices")


def check_addressing(intent, report):
    """Link subnets must be valid, the right size, and not reused."""
    seen = {}
    networks = []

    for i, link in enumerate(intent.get("links", []), 1):
        label = f"{link['a']['device']}<->{link['b']['device']}"
        raw = link.get("subnet")

        try:
            net = ipaddress.ip_network(raw)
        except (ValueError, TypeError) as exc:
            report.error("addressing", f"link {label}: invalid subnet '{raw}' ({exc})")
            continue

        if net.prefixlen not in P2P_PREFIXES:
            report.error(
                "addressing",
                f"link {label}: /{net.prefixlen} is not a point-to-point prefix "
                f"(expected /31 or /30)",
            )

        if raw in seen:
            report.error(
                "addressing",
                f"link {label} reuses subnet {raw}, already used by {seen[raw]}",
            )
        else:
            seen[raw] = label

        for other_net, other_label in networks:
            if net.overlaps(other_net):
                report.error(
                    "addressing",
                    f"link {label} subnet {net} overlaps {other_net} on {other_label}",
                )
        networks.append((net, label))

    report.record("addressing", f"{len(seen)} distinct link subnets")


def check_interfaces(intent, report):
    """One interface cannot be on two links."""
    used = defaultdict(list)

    for link in intent.get("links", []):
        for side in ("a", "b"):
            end = link.get(side, {})
            key = f"{end.get('device')}:{end.get('interface')}"
            used[key].append(f"{link['a']['device']}<->{link['b']['device']}")

    for key, links in sorted(used.items()):
        if len(links) > 1:
            report.error(
                "interfaces",
                f"{key} is used by {len(links)} links: {', '.join(links)}",
            )

    report.record("interfaces", f"{len(used)} interface endpoints")


def check_loopbacks(intent, report):
    """Loopbacks must be unique /32s, and must not sit inside a link subnet."""
    devices = intent.get("devices", {})
    link_nets = []
    for link in intent.get("links", []):
        try:
            link_nets.append((ipaddress.ip_network(link["subnet"]), link))
        except (ValueError, KeyError, TypeError):
            pass  # already reported by check_addressing

    seen = {}
    for name, device in sorted(devices.items()):
        raw = device.get("loopback")
        try:
            net = ipaddress.ip_network(raw)
        except (ValueError, TypeError) as exc:
            report.error("loopbacks", f"{name}: invalid loopback '{raw}' ({exc})")
            continue

        if net.prefixlen != 32:
            report.error("loopbacks", f"{name}: loopback {raw} should be a /32")

        if raw in seen:
            report.error("loopbacks", f"{name} and {seen[raw]} share loopback {raw}")
        else:
            seen[raw] = name

        for link_net, link in link_nets:
            if net.overlaps(link_net):
                label = f"{link['a']['device']}<->{link['b']['device']}"
                report.error(
                    "loopbacks",
                    f"{name}: loopback {raw} falls inside link subnet {link_net} ({label})",
                )

    report.record("loopbacks", f"{len(seen)} loopbacks")


def check_asns(intent, report):
    """ASNs must be present, in range, and - for this eBGP design - unique."""
    devices = intent.get("devices", {})
    seen = defaultdict(list)

    for name, device in sorted(devices.items()):
        asn = device.get("asn")
        if not isinstance(asn, int):
            report.error("asns", f"{name}: asn must be a number, got {asn!r}")
            continue
        if not 1 <= asn <= 4294967295:
            report.error("asns", f"{name}: asn {asn} is outside the valid range")
        elif not 64512 <= asn <= 65534:
            report.warn(
                "asns",
                f"{name}: asn {asn} is outside the private range 64512-65534",
            )
        seen[asn].append(name)

    for asn, names in sorted(seen.items()):
        if len(names) > 1:
            report.error(
                "asns",
                f"AS{asn} is used by {', '.join(names)} - this design expects "
                f"eBGP between every pair, so a shared AS would make these "
                f"sessions iBGP and change how routes propagate",
            )

    report.record("asns", f"{len(seen)} autonomous systems")


def check_connectivity(intent, report):
    """No device should be left with nothing attached to it."""
    devices = set(intent.get("devices", {}))
    attached = set()
    for link in intent.get("links", []):
        for side in ("a", "b"):
            attached.add(link.get(side, {}).get("device"))

    for name in sorted(devices - attached):
        report.error("connectivity", f"{name} has no links - it would be unreachable")

    report.record("connectivity", f"{len(devices & attached)} devices attached")


def check_topology_matches_intent(intent, report, topology_text=None):
    """The lab topology is generated from the intent - verify it still matches.

    This catches the case where the intent changed but nobody re-ran the
    renderer, which would otherwise only surface when the lab is built.
    """
    if topology_text is None:
        if not TOPOLOGY.exists():
            report.warn("topology", f"{TOPOLOGY.name} not found - skipping")
            return
        topology_text = TOPOLOGY.read_text()

    topo = yaml.safe_load(topology_text) or {}

    topo_nodes = set((topo.get("topology", {}).get("nodes") or {}))
    intent_nodes = set(intent.get("devices", {}))

    for missing in sorted(intent_nodes - topo_nodes):
        report.error("topology", f"{missing} is in the intent but not in the lab topology")
    for extra in sorted(topo_nodes - intent_nodes):
        report.error("topology", f"{extra} is in the lab topology but not in the intent")

    def pair(a, b):
        return tuple(sorted((a, b)))

    topo_links = set()
    for link in topo.get("topology", {}).get("links") or []:
        endpoints = link.get("endpoints") or []
        if len(endpoints) == 2:
            topo_links.add(pair(*endpoints))

    intent_links = {
        pair(
            f"{l['a']['device']}:{l['a']['interface']}",
            f"{l['b']['device']}:{l['b']['interface']}",
        )
        for l in intent.get("links", [])
        if "a" in l and "b" in l
    }

    for missing in sorted(intent_links - topo_links):
        report.error("topology", f"link {missing[0]} <-> {missing[1]} is missing from the lab topology")
    for extra in sorted(topo_links - intent_links):
        report.error("topology", f"link {extra[0]} <-> {extra[1]} is in the lab topology but not the intent")

    report.record("topology", f"{len(topo_nodes)} nodes, {len(topo_links)} links in lab file")


CHECKS = [
    check_references,
    check_addressing,
    check_interfaces,
    check_loopbacks,
    check_asns,
    check_connectivity,
]


def validate_intent(intent: dict, topology_text: str | None = None) -> Report:
    """Run every check against an intent and return the collected report.

    The portal calls this with a *proposed* intent that has not been
    written anywhere, which is why nothing here touches the filesystem
    except the optional topology comparison.
    """
    report = Report()
    for check in CHECKS:
        check(intent, report)
    check_topology_matches_intent(intent, report, topology_text)
    return report


# ---------------------------------------------------------------------

def main():
    if not INTENT.exists():
        print(f"No intent file at {INTENT}")
        return 1

    with INTENT.open() as fh:
        intent = yaml.safe_load(fh) or {}

    report = validate_intent(intent)

    print(f"Validating {INTENT.relative_to(REPO)}\n")
    for name, summary in report.checks:
        print(f"  {name:<14} {summary}")
    print()

    for name, message in report.warnings:
        print(f"  WARNING  [{name}] {message}")
    for name, message in report.errors:
        print(f"  ERROR    [{name}] {message}")

    if report.errors:
        plural = "s" if len(report.errors) != 1 else ""
        print(f"\nIntent is not valid: {len(report.errors)} error{plural}. Nothing was built.")
        return 1

    if report.warnings:
        print(f"\nIntent is valid ({len(report.warnings)} warning(s)).")
    else:
        print("\nIntent is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
