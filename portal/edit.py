"""Apply a proposed change to intent/network.yml as *text*.

The obvious implementation - parse the YAML, change the structure, dump
it back - quietly destroys every comment in the file. intent/network.yml
is written for humans to read, so that is not an acceptable trade.

Instead each supported change is expressed as an insertion into the
existing text, which leaves everything else byte-for-byte intact.
"""

import ipaddress
import re


class EditError(ValueError):
    """The change could not be applied to the file as written."""


NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
INTERFACE = re.compile(r"^[a-z][a-z0-9./_-]{0,30}$", re.IGNORECASE)


def _block_bounds(lines, key):
    """The half-open line range of a top-level `key:` block."""
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            start = i
            break
    if start is None:
        raise EditError(f"Could not find a top-level '{key}:' section in the intent file")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            end = i
            break

    # Do not swallow trailing blank lines that separate the sections.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _check_name(value, what):
    if not NAME.match(str(value or "")):
        raise EditError(f"{what} '{value}' is not a valid name (lowercase letters, digits and dashes)")


def _check_interface(value):
    if not INTERFACE.match(str(value or "")):
        raise EditError(f"Interface '{value}' is not a valid interface name")


def _check_network(value, what, prefixes=None):
    try:
        net = ipaddress.ip_network(str(value))
    except ValueError as exc:
        raise EditError(f"{what} '{value}' is not a valid network ({exc})") from exc
    if prefixes and net.prefixlen not in prefixes:
        wanted = " or ".join(f"/{p}" for p in sorted(prefixes))
        raise EditError(f"{what} '{value}' should be {wanted}")
    return net


def add_device(text: str, name: str, asn, loopback: str) -> str:
    """Insert a device at the end of the devices block."""
    _check_name(name, "Device name")
    _check_network(loopback, "Loopback", prefixes={32})
    try:
        asn = int(asn)
    except (TypeError, ValueError):
        raise EditError(f"AS number '{asn}' is not a number") from None

    lines = text.splitlines()
    if re.search(rf"^\s{{2}}{re.escape(name)}:\s*$", text, re.MULTILINE):
        raise EditError(f"Device '{name}' already exists")

    _, end = _block_bounds(lines, "devices")
    block = [f"  {name}:", f"    asn: {asn}", f"    loopback: {loopback}"]
    return "\n".join(lines[:end] + block + lines[end:]) + "\n"


def add_link(text: str, a_device, a_interface, b_device, b_interface, subnet) -> str:
    """Append a link to the end of the links block."""
    for device in (a_device, b_device):
        _check_name(device, "Device")
    for interface in (a_interface, b_interface):
        _check_interface(interface)
    _check_network(subnet, "Subnet", prefixes={30, 31})

    if a_device == b_device:
        raise EditError("A link needs two different devices")

    lines = text.splitlines()
    _, end = _block_bounds(lines, "links")
    block = [
        "",
        f"  - a: {{device: {a_device}, interface: {a_interface}}}",
        f"    b: {{device: {b_device}, interface: {b_interface}}}",
        f"    subnet: {subnet}",
    ]
    return "\n".join(lines[:end] + block + lines[end:]) + "\n"


def apply_operation(text: str, operation: dict) -> str:
    """Return the intent file text with `operation` applied."""
    kind = (operation or {}).get("kind")

    if kind == "none":
        return text

    if kind == "raw":
        replacement = operation.get("yaml")
        if not isinstance(replacement, str) or not replacement.strip():
            raise EditError("The edited file is empty")
        return replacement if replacement.endswith("\n") else replacement + "\n"

    if kind == "add_device":
        return add_device(
            text,
            operation.get("name"),
            operation.get("asn"),
            operation.get("loopback"),
        )

    if kind == "add_link":
        return add_link(
            text,
            operation.get("a_device"),
            operation.get("a_interface"),
            operation.get("b_device"),
            operation.get("b_interface"),
            operation.get("subnet"),
        )

    raise EditError(f"Unsupported change type: {kind!r}")
