"""Tests for the convergence helper itself.

This exists because the helper got it wrong once, in a way that only
appeared in CI. It waited for BGP sessions to reach Established and
returned - but a session being up does not mean its routes have been
installed. Locally the lab had been running for minutes before anyone
ran the tests, so the gap was invisible. In CI the tests start seconds
after the lab is built, and six route assertions failed for a reason
that had nothing to do with the network being tested.

No containers are needed here: the device layer is replaced with a
small model of a network that comes up in stages.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts"))

import netlab  # noqa: E402


class FakeLab:
    """A lab where sessions come up first and routes arrive later."""

    def __init__(self, routes_after=2):
        self.polls = 0
        self.routes_after = routes_after

    def bgp_peers(self, device):
        model = netlab.DEVICES[device]
        return {
            neighbor["address"]: {"state": "Established", "remoteAs": neighbor["asn"]}
            for neighbor in model["neighbors"]
        }

    def routes_for(self, device, prefix):
        # Routes only show up once the network has been polled a few times.
        if self.polls < self.routes_after:
            return []
        return [{"protocol": "bgp", "nexthops": [{"ip": "10.9.9.9"}]}]


@pytest.fixture
def no_sleep(monkeypatch):
    """Let the poll loop run without actually waiting."""
    monkeypatch.setattr(netlab.time, "sleep", lambda _s: None)


def install(monkeypatch, lab):
    def peers(device):
        return lab.bgp_peers(device)

    def routes(device, prefix):
        return lab.routes_for(device, prefix)

    monkeypatch.setattr(netlab, "bgp_peers", peers)
    monkeypatch.setattr(netlab, "routes_for", routes)


def test_convergence_waits_for_routes_not_just_sessions(monkeypatch, no_sleep):
    """The bug: every session Established, no routes anywhere, returns anyway."""
    lab = FakeLab(routes_after=3)
    install(monkeypatch, lab)

    original = netlab.unlearned_prefixes

    def counting_unlearned(devices=netlab.DEVICES):
        lab.polls += 1
        return original(devices)

    monkeypatch.setattr(netlab, "unlearned_prefixes", counting_unlearned)

    netlab.wait_for_convergence(timeout=30)
    assert lab.polls >= 3, (
        "returned before the routes arrived - sessions being up is not convergence"
    )


def test_convergence_returns_once_routes_are_present(monkeypatch, no_sleep):
    lab = FakeLab(routes_after=0)
    install(monkeypatch, lab)
    states = netlab.wait_for_convergence(timeout=30)
    assert states
    assert all(state == "Established" for state in states.values())


def test_timeout_says_which_routes_never_arrived(monkeypatch, no_sleep):
    """A timeout has to name the problem, or it teaches nobody anything."""
    lab = FakeLab(routes_after=10_000)   # never
    install(monkeypatch, lab)

    with pytest.raises(netlab.DeviceError) as caught:
        netlab.wait_for_convergence(timeout=0)

    message = str(caught.value)
    assert "sessions are up, but these routes never arrived" in message
    assert "no BGP route" in message


def test_a_route_from_the_wrong_protocol_does_not_count(monkeypatch, no_sleep):
    """A connected or static route to a loopback is not the same as
    having learned it through BGP, and must not satisfy convergence."""
    install(monkeypatch, FakeLab(routes_after=0))
    monkeypatch.setattr(
        netlab, "routes_for",
        lambda device, prefix: [{"protocol": "connected", "nexthops": []}],
    )

    missing = netlab.unlearned_prefixes()
    assert missing, "a non-BGP route should not count as converged"
    assert "connected" in next(iter(missing.values()))
