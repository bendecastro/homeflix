# ADR 0002: Host = low-power x86-64 mini-PC on Debian, runtime = Docker Compose

Date: 2026-06-14

## Status
Accepted

> Source: prior private design package (Jan 2026 — see `references/source-research.md`).
> Treat as **designed; running-state on the box unconfirmed** — verify on the host before
> relying on it.

## Context
homeflix needs an always-on, low-power host. Prior research settled on a low-power x86-64 mini-PC mini-PC already on hand, and on a plain-distro + containers approach (vs an
appliance OS like Unraid/TrueNAS). The Docker tooling research recommended the
"Balanced Stack": Docker Compose + Traefik + Watchtower.

## Decision
- **Host:** low-power x86-64 mini-PC — Intel Celeron-class CPU, 8GB RAM, 1TB internal SSD.
- **OS:** Debian Linux, installed on the internal SSD.
- **Storage tier 2:** 4TB external HDD over USB 3.0, ext4, mounted at `${DATA_ROOT}`
  (auto-mount via `/etc/fstab` UUID + `nofail`). See [ADR-0008](adr-0008-single-filesystem-data-root-hardlinks.md).
- **Container runtime:** Docker + Docker Compose, single `docker-compose.yml`.

## Consequences
- Modest hardware: Celeron + 8GB RAM caps simultaneous transcodes — favor **direct
  play** in Jellyfin and client-friendly formats; don't promise many parallel 1080p+
  transcodes. Verify hardware-transcode (QuickSync) capability before relying on it.
- USB-attached HDD is a reliability/throughput consideration (USB resets, spin-down);
  `nofail` keeps boot from hanging if the drive is absent.
- Single box = single point of failure; no HA. Acceptable for a family setup.
- Rules out appliance-OS conveniences (GUI app store, built-in ZFS UI); we manage via
  compose instead.

## Links
- `project/hardware.md`, `project/deployment.md`, `references/paths.md`
- Source: prior private design package (see `references/source-research.md`).
