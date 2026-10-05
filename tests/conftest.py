import pytest

from netlab import (
    DEVICES,
    INTENT_DATA,
    DeviceError,
    container,
    container_running,
    session_states,
    wait_for_convergence,
)


@pytest.fixture(scope="session")
def intent():
    return INTENT_DATA


@pytest.fixture(scope="session")
def devices():
    return DEVICES


@pytest.fixture(scope="session")
def lab_up():
    """Stop early, with a useful message, if the lab is not running."""
    missing = [name for name in DEVICES if not container_running(name)]
    if missing:
        names = ", ".join(container(n) for n in missing)
        pytest.exit(
            f"Lab is not running - missing container(s): {names}\n"
            f"Start it with: sudo containerlab deploy -t lab/topology.clab.yml",
            returncode=2,
        )
    return True


@pytest.fixture(scope="session")
def converged(lab_up):
    """Give the control plane time to settle before anything is asserted.

    This deliberately does NOT fail when convergence times out. If it
    did, one unhealthy session would bury every test under the same
    error. Instead we wait, then let each test report precisely what is
    wrong with the part of the network it cares about.
    """
    try:
        return wait_for_convergence()
    except DeviceError as exc:
        print(f"\n[warning] proceeding without full convergence:\n{exc}\n")
        return session_states()
