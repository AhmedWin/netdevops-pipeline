"""Tests for the portal API.

These run without a lab, without Docker and without GitHub. They cover
the behaviour that matters most: the portal must never let an invalid
change through, and it must agree with the pipeline about what "valid"
means - because it calls the same code.
"""

import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "portal"))
sys.path.insert(0, str(REPO / "scripts"))

fastapi_testclient = pytest.importorskip(
    "fastapi.testclient", reason="portal tests need fastapi installed"
)

import yaml  # noqa: E402

from app import app  # noqa: E402
from render import INTENT, load_intent  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return fastapi_testclient.TestClient(app)


@pytest.fixture(scope="module")
def intent():
    return load_intent(INTENT)


ADD_LINK = {
    "kind": "add_link",
    "a_device": "r1", "a_interface": "eth3",
    "b_device": "r2", "b_interface": "eth3",
    "subnet": "10.1.99.0/31",
}


def raw_of(intent):
    """A raw edit carrying an arbitrary intent, for the negative cases."""
    return {"kind": "raw", "yaml": yaml.safe_dump(intent, sort_keys=False)}


# ---------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------

def test_network_endpoint_describes_the_committed_intent(client, intent):
    data = client.get("/api/network").json()
    assert data["validation"]["ok"], "the committed intent should always be valid"
    assert len(data["summary"]["devices"]) == len(intent["devices"])
    assert len(data["summary"]["links"]) == len(intent["links"])


def test_every_device_reports_its_interfaces_and_neighbors(client):
    for device in client.get("/api/network").json()["summary"]["devices"]:
        assert device["interfaces"], f"{device['name']} has no interfaces"
        assert len(device["interfaces"]) == len(device["neighbors"])


# ---------------------------------------------------------------------
# Validating a proposal
# ---------------------------------------------------------------------

def test_a_sound_proposal_validates(client):
    result = client.post("/api/propose", json={"operation": ADD_LINK}).json()["validation"]
    assert result["ok"], result["errors"]
    assert result["seconds"] < 1.0, "validation is supposed to be the cheap check"


def test_a_duplicate_subnet_is_rejected(client, intent):
    duplicate = {**ADD_LINK, "subnet": intent["links"][0]["subnet"]}

    result = client.post("/api/propose", json={"operation": duplicate}).json()["validation"]
    assert not result["ok"]
    assert any("reuses subnet" in e["message"] for e in result["errors"])


def test_a_device_with_no_links_is_rejected(client):
    orphan = {"kind": "add_device", "name": "r9", "asn": 65009, "loopback": "10.0.0.9/32"}

    result = client.post("/api/propose", json={"operation": orphan}).json()["validation"]
    assert not result["ok"]
    assert any("no links" in e["message"] for e in result["errors"])


def test_malformed_intent_is_reported_not_crashed(client):
    response = client.post("/api/propose", json={"operation": raw_of({"devices": "nonsense"})})
    assert response.status_code == 200, "a bad intent is a validation result, not a 500"
    assert not response.json()["validation"]["ok"]


def test_a_nonsense_edit_is_refused_with_a_reason(client):
    response = client.post(
        "/api/propose",
        json={"operation": {**ADD_LINK, "subnet": "not-a-subnet"}},
    )
    assert response.status_code == 400
    assert "not a valid network" in response.json()["detail"]


# ---------------------------------------------------------------------
# Previewing
# ---------------------------------------------------------------------

def test_preview_shows_both_ends_of_a_new_link(client):
    preview = client.post("/api/propose", json={"operation": ADD_LINK}).json()
    paths = {change["path"] for change in preview["changes"]}

    # Both routers, the intent itself, and the generated lab topology.
    assert "configs/r1/frr.conf" in paths
    assert "configs/r2/frr.conf" in paths
    assert "intent/network.yml" in paths
    assert "lab/topology.clab.yml" in paths


def test_proposing_nothing_changes_nothing(client):
    preview = client.post("/api/propose", json={"operation": {"kind": "none"}}).json()
    assert preview["count"] == 0, (
        "a no-op must produce an empty diff - if the intent file is rewritten "
        "rather than edited, this is where it shows up"
    )


def test_an_edit_preserves_the_comments_in_the_intent_file(client):
    preview = client.post("/api/propose", json={"operation": ADD_LINK}).json()
    change = next(c for c in preview["changes"] if c["path"] == "intent/network.yml")
    removed = [l for l in change["diff"].splitlines()
               if l.startswith("-") and not l.startswith("---")]
    assert not removed, f"editing the intent should only add lines, but removed: {removed}"


def test_generated_config_contains_the_new_peering(client):
    preview = client.post("/api/propose", json={"operation": ADD_LINK}).json()
    r1 = next(c for c in preview["changes"] if c["path"] == "configs/r1/frr.conf")
    assert "neighbor 10.1.99.1 remote-as 65002" in r1["diff"]


# ---------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------

def test_an_invalid_change_is_never_submitted(client, intent):
    duplicate = {**ADD_LINK, "subnet": intent["links"][0]["subnet"]}

    response = client.post("/api/submit", json={"operation": duplicate, "title": "bad"})
    assert response.status_code == 400, (
        "an invalid change must be refused on its own merits, whatever "
        "the GitHub configuration happens to be"
    )
    assert "errors" in response.json()["detail"]


def test_branch_names_are_safe(client):
    from app import branch_name

    name = branch_name("Add link r1 <-> r4 !! (urgent)")
    assert name.startswith("portal/")
    assert all(c.isalnum() or c in "-/" for c in name), name


# ---------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------

def test_a_token_locks_the_portal(monkeypatch):
    """With PORTAL_TOKEN set, nothing is readable until you sign in."""
    import app as portal

    monkeypatch.setattr(portal, "PORTAL_TOKEN", "s3cret")
    monkeypatch.setattr(portal, "_SESSIONS", set())
    locked = fastapi_testclient.TestClient(portal.app)

    assert locked.get("/api/network").status_code == 401
    assert locked.post("/api/propose", json={"operation": ADD_LINK}).status_code == 401

    assert locked.post("/api/login", json={"token": "wrong"}).status_code == 401
    assert locked.post("/api/login", json={"token": "s3cret"}).status_code == 200

    # The client keeps the session cookie from here on.
    assert locked.get("/api/network").status_code == 200
