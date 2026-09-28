#!/usr/bin/env python3
"""
Render device configs from the source of truth.

    intent/network.yml  ->  configs/<device>/frr.conf

Usage:
    python3 scripts/render.py            # write the configs
    python3 scripts/render.py --check    # fail if configs are out of date
"""

import argparse
import ipaddress
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO = Path(__file__).resolve().parent.parent
INTENT = REPO / "intent" / "network.yml"
TEMPLATES = REPO / "templates"
OUTPUT = REPO / "configs"


def load_intent(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh)


def build_device_models(intent: dict) -> dict:
    """Turn the declarative intent into a per-device view.

    A link is declared once. Both ends are derived from it:
      - each side's interface IP, taken from the /31
      - each side's eBGP neighbor, pointing at the other end
    """
    devices = intent["devices"]

    models = {
        name: {
            "name": name,
            "asn": dev["asn"],
            "loopback": dev["loopback"],
            "interfaces": [],
            "neighbors": [],
        }
        for name, dev in devices.items()
    }

    for link in intent.get("links", []):
        subnet = ipaddress.ip_network(link["subnet"])
        hosts = list(subnet)  # on a /31 both addresses are usable
        if len(hosts) < 2:
            raise ValueError(f"subnet {subnet} is too small for a p2p link")

        ends = [(link["a"], hosts[0]), (link["b"], hosts[1])]

        for i in (0, 1):
            side, addr = ends[i]
            other_side, other_addr = ends[1 - i]

            local = side["device"]
            remote = other_side["device"]

            for who in (local, remote):
                if who not in models:
                    raise ValueError(f"link references unknown device '{who}'")

            models[local]["interfaces"].append(
                {
                    "name": side["interface"],
                    "address": f"{addr}/{subnet.prefixlen}",
                    "peer": remote,
                }
            )
            models[local]["neighbors"].append(
                {
                    "address": str(other_addr),
                    "asn": devices[remote]["asn"],
                    "peer": remote,
                }
            )

    # Stable ordering so regenerating produces identical files
    for model in models.values():
        model["interfaces"].sort(key=lambda i: i["name"])
        model["neighbors"].sort(key=lambda n: ipaddress.ip_address(n["address"]))

    return models


def render(models: dict, defaults: dict) -> dict:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("frr.conf.j2")
    return {
        name: template.render(defaults=defaults, **model)
        for name, model in models.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if any config on disk differs from the intent",
    )
    args = parser.parse_args()

    intent = load_intent(INTENT)
    models = build_device_models(intent)
    rendered = render(models, intent.get("defaults", {}))

    drifted = []
    for name, text in sorted(rendered.items()):
        target = OUTPUT / name / "frr.conf"
        current = target.read_text() if target.exists() else None

        if args.check:
            if current != text:
                drifted.append(name)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        status = "unchanged" if current == text else "written"
        print(f"  {name:<4} {status:>9}  {target.relative_to(REPO)}")

    if args.check:
        if drifted:
            print("Configs do not match intent/network.yml: " + ", ".join(drifted))
            print("Run: python3 scripts/render.py")
            return 1
        print(f"All {len(rendered)} configs match intent/network.yml")
        return 0

    print(f"Rendered {len(rendered)} device configs from {INTENT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
