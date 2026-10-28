"""Helpers for talking to the running containerlab topology.

Everything the tests know about the *intended* network comes from
intent/network.yml (via scripts/render.py), so the tests and the configs
can never disagree about what the network is supposed to look like.
Everything they know about the *actual* network comes from the devices.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from render import INTENT, build_device_models, load_intent  # noqa: E402

LAB_PREFIX = os.environ.get("CLAB_PREFIX", "clab-netdevops-")
CONVERGENCE_TIMEOUT = int(os.environ.get("CONVERGENCE_TIMEOUT", "90"))
DOCKER = os.environ.get("DOCKER_BIN", "docker")

# The intended network, loaded once.
INTENT_DATA = load_intent(INTENT)
DEVICES = build_device_models(INTENT_DATA)


class DeviceError(RuntimeError):
    """A device could not be reached, or answered with something unusable."""


def container(device: str) -> str:
    return f"{LAB_PREFIX}{device}"


def ip_only(address: str) -> str:
    """'10.0.0.1/32' -> '10.0.0.1'"""
    return address.split("/")[0]


def _run(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def container_running(device: str) -> bool:
    proc = _run([DOCKER, "inspect", "-f", "{{.State.Running}}", container(device)])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def vtysh(device: str, *commands: str, timeout: int = 30) -> str:
    """Run one or more vtysh commands on a device and return stdout."""
    args = [DOCKER, "exec", container(device), "vtysh"]
    for command in commands:
        args += ["-c", command]
    proc = _run(args, timeout=timeout)
    if proc.returncode != 0:
        raise DeviceError(
            f"{device}: {commands!r} failed (exit {proc.returncode})\n{proc.stderr.strip()}"
        )
    return proc.stdout


def vtysh_json(device: str, command: str, timeout: int = 30):
    """Run a show command in JSON mode and parse the result.

    FRR sometimes prints warnings before the JSON body, so we start
    parsing at the first brace rather than trusting the whole stream.
    """
    raw = vtysh(device, f"{command} json", timeout=timeout)
    start = raw.find("{")
    if start == -1:
        raise DeviceError(f"{device}: '{command} json' returned no JSON:\n{raw}")
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise DeviceError(f"{device}: '{command} json' returned invalid JSON:\n{raw}") from exc


def bgp_peers(device: str) -> dict:
    """{neighbor_ip: peer_info} from 'show bgp summary'."""
    data = vtysh_json(device, "show bgp summary")
    return data.get("ipv4Unicast", {}).get("peers", {}) or {}


def routes_for(device: str, prefix: str) -> list:
    """Route entries for a prefix, or [] if the device has none."""
    data = vtysh_json(device, f"show ip route {prefix}")
    return data.get(prefix, []) or []


def nexthops_for(device: str, prefix: str) -> set:
    """The set of next-hop IPs a device would use to reach a prefix."""
    return {
        hop.get("ip")
        for route in routes_for(device, prefix)
        for hop in route.get("nexthops", [])
        if hop.get("ip")
    }


def ping(device: str, source: str, destination: str, count: int = 3, wait: int = 2):
    """Ping from one of the device's own addresses. Returns (ok, output)."""
    proc = _run(
        [
            DOCKER, "exec", container(device),
            "ping", "-c", str(count), "-W", str(wait), "-I", source, destination,
        ],
        timeout=count * wait + 20,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def set_interface(device: str, interface: str, up: bool):
    """Administratively enable or disable an interface."""
    vtysh(
        device,
        "configure terminal",
        f"interface {interface}",
        "no shutdown" if up else "shutdown",
    )


def session_states(devices: dict = DEVICES) -> dict:
    """{'r1 -> r2 (10.1.12.1)': 'Established', ...} for every intended session."""
    states = {}
    for name, model in devices.items():
        peers = bgp_peers(name)
        for neighbor in model["neighbors"]:
            label = f"{name} -> {neighbor['peer']} ({neighbor['address']})"
            states[label] = peers.get(neighbor["address"], {}).get("state", "no such neighbor")
    return states


def unlearned_prefixes(devices: dict = DEVICES) -> dict:
    """Which loopbacks each device has NOT yet learned through BGP.

    'Established' only means the session came up. It says nothing about
    whether the UPDATE messages have been processed and the routes
    installed, and there is a real window between the two. Tests that
    run inside that window fail for a reason that has nothing to do with
    the change being tested.
    """
    missing = {}
    for name in devices:
        for other, model in devices.items():
            if other == name:
                continue
            prefix = model["loopback"]
            try:
                entries = routes_for(name, prefix)
            except DeviceError as exc:
                missing[f"{name} -> {other} ({prefix})"] = f"lookup failed: {exc}"
                continue
            if not any(entry.get("protocol") == "bgp" for entry in entries):
                seen = sorted({entry.get("protocol") for entry in entries}) or ["nothing"]
                missing[f"{name} -> {other} ({prefix})"] = f"no BGP route (saw: {', '.join(map(str, seen))})"
    return missing


def wait_for_convergence(devices: dict = DEVICES, timeout: int = CONVERGENCE_TIMEOUT,
                         interval: float = 2.0) -> dict:
    """Block until the network has actually converged.

    That means two things, not one: every intended session is
    Established, AND every device has installed a BGP route to every
    other device's loopback. Waiting only on session state returns too
    early - the sessions come up a moment before the routes land, which
    is invisible when you deploy a lab and run the tests minutes later,
    and perfectly visible in CI where they run seconds apart.
    """
    deadline = time.monotonic() + timeout
    states, missing = {}, {}
    while True:
        states = session_states(devices)
        sessions_up = all(state == "Established" for state in states.values())

        missing = unlearned_prefixes(devices) if sessions_up else {}
        if sessions_up and not missing:
            return states

        if time.monotonic() >= deadline:
            detail = "\n".join(f"    {k}: {v}" for k, v in sorted(states.items()))
            if missing:
                detail += "\n  sessions are up, but these routes never arrived:\n"
                detail += "\n".join(f"    {k}: {v}" for k, v in sorted(missing.items()))
            raise DeviceError(f"Network did not converge within {timeout}s:\n{detail}")
        time.sleep(interval)


def wait_until(predicate, what: str, timeout: int = 60, interval: float = 2.0):
    """Poll until predicate() is true, or fail with a readable message."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise DeviceError(f"Timed out after {timeout}s waiting for {what}")
        time.sleep(interval)
