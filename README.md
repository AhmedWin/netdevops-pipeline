# NetDevOps Pipeline — Network Changes as Code

**No network change reaches a device unless it passes a pipeline first.**

A working demonstration of applying software delivery practice to network
changes: a source of truth, generated configs, automated validation, and
tests that run against a real (virtual) network on every proposed change.

## Status

- [x] **Block 1** — Lab topology: 3 FRR routers, eBGP triangle, containerlab
- [x] **Block 2** — Source of truth + config generation (Jinja2)
- [ ] Block 3 — Pre-deployment validation
- [ ] Block 4 — Automated network tests
- [ ] Block 5 — GitHub Actions pipeline
- [ ] Block 6 — PR reporting + config drift detection

## The topology

```
          r1 (AS65001)
           10.0.0.1
          /         \
   10.1.12.0/31   10.1.13.0/31
        /             \
r2 (AS65002) ----- r3 (AS65003)
  10.0.0.2   10.1.23.0/31  10.0.0.3
```

Three routers, each in its own autonomous system, each advertising a
loopback. Every pair is an eBGP peering.

## How a change is made

Device configs are **generated, never edited**. The only file a human
touches is the source of truth:

```
intent/network.yml  ->  scripts/render.py  ->  configs/<device>/frr.conf
   (edit this)           (+ templates/)         (generated - do not edit)
```

A link is declared **once**, in one place:

```yaml
- a: {device: r1, interface: eth1}
  b: {device: r2, interface: eth1}
  subnet: 10.1.12.0/31
```

From that single declaration the renderer derives, for both ends: the
interface address out of the /31, the interface description, and the eBGP
neighbor statement pointing at the far side with the correct remote AS.
Adding a device or a link means editing intent, not six config files.

## Usage

```bash
# Install dependencies
pip install -r requirements.txt

# Generate device configs from intent
python3 scripts/render.py

# Fail if configs on disk no longer match intent (used by CI)
python3 scripts/render.py --check

# Bring the lab up / tear it down
sudo containerlab deploy  -t lab/topology.clab.yml
sudo containerlab destroy -t lab/topology.clab.yml

# Verify
docker exec clab-netdevops-r1 vtysh -c "show bgp summary"
docker exec clab-netdevops-r1 ping -c 3 -I 10.0.0.1 10.0.0.3
```

## Layout

| Path | Purpose |
|---|---|
| `intent/network.yml` | Source of truth — the only file edited by hand |
| `templates/frr.conf.j2` | Jinja2 template for FRR device config |
| `scripts/render.py` | Renders intent into device configs |
| `configs/` | **Generated** device configs |
| `lab/topology.clab.yml` | containerlab topology definition |

## Why FRR

[FRR](https://frrouting.org/) is a production open-source routing suite
(it is the routing stack in NVIDIA Cumulus Linux and SONiC). It is used
here because it is freely redistributable, runs as a lightweight
container, and exposes a Cisco-like CLI — which means the whole lab can
run inside a public CI runner, where licensed vendor images cannot.

## Limitations

Stated up front, because they matter:

- FRR models the **control plane** faithfully (BGP state machine, path
  selection, policy) but not hardware behaviour — no ASIC forwarding,
  TCAM limits, or vendor-specific bugs.
- Config syntax here is FRR's. Applying this approach to IOS-XE, NX-OS,
  Junos or VRP means a template per platform.
- `no bgp ebgp-requires-policy` is a lab shortcut. Production eBGP should
  apply explicit import/export policy.
- The hard part of adopting this in an existing network is not the
  tooling — it is establishing a trustworthy source of truth for a
  network that has been running for years.
