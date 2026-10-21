# NetDevOps Pipeline — Network Changes as Code

[![network](https://github.com/AhmedWin/netdevops-pipeline/actions/workflows/network.yml/badge.svg)](https://github.com/AhmedWin/netdevops-pipeline/actions/workflows/network.yml)

**No network change reaches a device unless it passes a pipeline first.**

A working demonstration of applying software delivery practice to network
changes: a source of truth, generated configs, automated validation, and
tests that run against a real (virtual) network on every proposed change.

## Status

- [x] **Block 1** — Lab topology: 3 FRR routers, eBGP triangle, containerlab
- [x] **Block 2** — Source of truth + config generation (Jinja2)
- [x] **Block 3** — Automated network tests (pytest, against the running lab)
- [x] **Block 4** — GitHub Actions pipeline (every change verified automatically)
- [x] **Block 5** — Validate the intent before anything is built
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

## How a change gets merged

Nothing reaches a device because someone looked at it and thought it was
fine. Every proposed change opens a pull request, and GitHub Actions runs
these in order — cheapest first, so a bad change fails fast:

1. **Validates the intent** (~0.1s) — addressing, ASNs, interfaces and
   topology consistency, with no network built at all
2. **Checks the configs match the intent** — fails if anyone hand-edited a
   generated file
3. **Builds the whole topology from scratch** on a clean runner
4. **Runs all 25 network tests** against that live network
5. **Tears the lab down** whether it passed or not

If any step fails the pull request is red and cannot be merged. On a
failure the job also dumps BGP and routing state from every device, so a
red X comes with the evidence rather than just a verdict.

The workflow is in [`.github/workflows/network.yml`](.github/workflows/network.yml).

## Usage

```bash
# Install dependencies
pip install -r requirements.txt

# Check the design is sound before building anything
python3 scripts/validate.py

# Generate device configs from intent
python3 scripts/render.py

# Fail if configs on disk no longer match intent (used by CI)
python3 scripts/render.py --check

# Bring the lab up / tear it down
sudo containerlab deploy  -t lab/topology.clab.yml
sudo containerlab destroy -t lab/topology.clab.yml

# Verify by hand
docker exec clab-netdevops-r1 vtysh -c "show bgp summary"
docker exec clab-netdevops-r1 ping -c 3 -I 10.0.0.1 10.0.0.3

# Or verify properly
pip install -r requirements-dev.txt
pytest
```

## What validation catches

`scripts/validate.py` reads `intent/network.yml` and answers one question:
is this design internally consistent? It runs in about a tenth of a second
and builds nothing.

| Check | Catches |
|---|---|
| `references` | A link pointing at a device that does not exist, or a device linked to itself |
| `addressing` | A reused or overlapping link subnet, or a prefix that is not point-to-point |
| `interfaces` | The same interface assigned to two different links |
| `loopbacks` | Duplicate loopbacks, or a loopback that falls inside a link subnet |
| `asns` | A missing, out-of-range, or shared AS number |
| `connectivity` | A device with no links, which would be unreachable |
| `topology` | The lab topology file drifting out of step with the intent |

The duplicate-subnet bug that the full pipeline caught in 3 minutes is
caught here in 0.08 seconds. Same bug, no routers, no BGP, no waiting.
That ordering - cheapest check first - is the point.

## What the tests check

`pytest` runs against the **running lab**, not the config files. Every
expectation is derived from `intent/network.yml`, so adding a device to
the intent adds its tests automatically.

| Test | What it would catch |
|---|---|
| `test_bgp_session_is_established` | A peering that never came up, or came up to the wrong AS |
| `test_loopback_is_learned_via_bgp` | A prefix that is not being advertised or accepted |
| `test_direct_link_is_the_preferred_path` | Traffic silently detouring - the change "works", but not the way it was meant to |
| `test_loopback_is_reachable` | A route in the table that does not actually forward |
| `test_traffic_reroutes_when_a_link_fails` | A topology with no real redundancy |

The last one shuts a real interface, waits for BGP to withdraw the
direct path, proves traffic still flows the long way round, and then
restores the link. A suite that only ever proves "everything is up"
says nothing about what happens at 3am.

## Layout

| Path | Purpose |
|---|---|
| `intent/network.yml` | Source of truth — the only file edited by hand |
| `templates/frr.conf.j2` | Jinja2 template for FRR device config |
| `scripts/validate.py` | Checks the intent is internally consistent |
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
