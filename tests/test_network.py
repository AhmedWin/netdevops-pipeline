"""Tests for the network itself.

These do not test the config files. They test the running network:
sessions that must be up, routes that must be learned, traffic that must
flow, and a path that must survive a link failure.

Every expectation is derived from intent/network.yml - nothing is
hard-coded here, so adding a device to the intent adds its tests too.
"""

import itertools

import pytest

from netlab import (
    DEVICES,
    INTENT_DATA,
    ip_only,
    bgp_peers,
    nexthops_for,
    ping,
    routes_for,
    set_interface,
    wait_for_convergence,
    wait_until,
)

# ---------------------------------------------------------------------
# Expectations derived from the source of truth
# ---------------------------------------------------------------------

SESSIONS = [
    (device, neighbor["peer"], neighbor["address"], neighbor["asn"])
    for device, model in sorted(DEVICES.items())
    for neighbor in model["neighbors"]
]
SESSION_IDS = [f"{d}-to-{p}" for d, p, _, _ in SESSIONS]

PAIRS = sorted(itertools.permutations(sorted(DEVICES), 2))
PAIR_IDS = [f"{a}-to-{b}" for a, b in PAIRS]


def direct_neighbor(device, target):
    """The next-hop address of the direct link from device to target."""
    for neighbor in DEVICES[device]["neighbors"]:
        if neighbor["peer"] == target:
            return neighbor["address"]
    return None


# ---------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------

@pytest.mark.parametrize("device,peer,address,remote_asn", SESSIONS, ids=SESSION_IDS)
def test_bgp_session_is_established(converged, device, peer, address, remote_asn):
    """Every peering declared in the intent is actually up."""
    peers = bgp_peers(device)
    assert address in peers, (
        f"{device} has no BGP neighbor {address} ({peer}). "
        f"Configured neighbors: {sorted(peers)}"
    )
    info = peers[address]
    assert info.get("state") == "Established", (
        f"{device} -> {peer} ({address}) is '{info.get('state')}', not Established"
    )
    assert info.get("remoteAs") == remote_asn, (
        f"{device} -> {peer}: remote AS is {info.get('remoteAs')}, expected {remote_asn}"
    )


@pytest.mark.parametrize("device,target", PAIRS, ids=PAIR_IDS)
def test_loopback_is_learned_via_bgp(converged, device, target):
    """Each device learns every other device's loopback, through BGP."""
    prefix = DEVICES[target]["loopback"]
    entries = routes_for(device, prefix)
    assert entries, f"{device} has no route at all to {target}'s loopback {prefix}"

    protocols = {entry.get("protocol") for entry in entries}
    assert "bgp" in protocols, (
        f"{device} has a route to {prefix} but not via BGP (saw: {sorted(protocols)})"
    )


@pytest.mark.parametrize("device,target", PAIRS, ids=PAIR_IDS)
def test_direct_link_is_the_preferred_path(converged, device, target):
    """In this topology every pair is directly connected, so the direct
    link should be the chosen next hop - not a detour through the third
    router. This is the test that catches a change which technically
    'works' but silently moves traffic somewhere unintended."""
    expected = direct_neighbor(device, target)
    if expected is None:
        pytest.skip(f"{device} and {target} are not directly connected")

    prefix = DEVICES[target]["loopback"]
    hops = nexthops_for(device, prefix)
    assert hops, f"{device} has no next hop for {prefix}"
    assert expected in hops, (
        f"{device} reaches {target} via {sorted(hops)}, "
        f"not over the direct link ({expected})"
    )


# ---------------------------------------------------------------------
# Data plane
# ---------------------------------------------------------------------

@pytest.mark.parametrize("device,target", PAIRS, ids=PAIR_IDS)
def test_loopback_is_reachable(converged, device, target):
    """Routes in the table are not proof of anything. Send real packets."""
    source = ip_only(DEVICES[device]["loopback"])
    destination = ip_only(DEVICES[target]["loopback"])
    ok, output = ping(device, source, destination)
    assert ok, f"{device} ({source}) cannot reach {target} ({destination}):\n{output}"


# ---------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------

def test_traffic_reroutes_when_a_link_fails(converged):
    """Fail one link and prove the network heals.

    A passing 'everything is up' suite tells you nothing about what
    happens at 3am. This shuts a real interface, waits for BGP to
    withdraw the direct path, checks traffic still flows the long way
    round, and then puts the link back.
    """
    link = INTENT_DATA["links"][0]
    near, far = link["a"]["device"], link["b"]["device"]
    interface = link["a"]["interface"]

    source = ip_only(DEVICES[near]["loopback"])
    destination = ip_only(DEVICES[far]["loopback"])
    direct = direct_neighbor(near, far)
    prefix = DEVICES[far]["loopback"]

    try:
        set_interface(near, interface, up=False)

        wait_until(
            lambda: direct not in nexthops_for(near, prefix),
            what=f"{near} to withdraw the direct path to {far} via {direct}",
            timeout=60,
        )

        detour = nexthops_for(near, prefix)
        assert detour, (
            f"{near} lost every route to {far} when {interface} went down - "
            f"the backup path did not take over"
        )

        ok, output = ping(near, source, destination)
        assert ok, (
            f"{near} cannot reach {far} after failover. "
            f"Route now points at {sorted(detour)}\n{output}"
        )
    finally:
        set_interface(near, interface, up=True)
        wait_for_convergence()
