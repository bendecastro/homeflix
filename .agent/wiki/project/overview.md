# homeflix — Project Overview

Updated: 2026-06-14

## What homeflix is

A self-hosted streaming service for a household — a private "Netflix" backed by a media
library you own, running on a single low-power mini-PC. The practical outcome of this
wiki is **a running homeflix the household actually uses**.

## Where it stands (important)

This is **not greenfield.** A complete stack was already designed in prior research at
the prior private design package (Jan 2026): a working `docker-compose.yml` (14 services), storage
layout, VPN analysis, and setup guides. Most foundational decisions are therefore
**already made** (see Decisions below). The remaining work is to **reconcile, deploy,
verify, and close the gaps** — not to re-decide from scratch.

The wiki has folded that research into living pages. The original package is
unpublished; see `references/source-research.md` for what carried forward and what
didn't survive review.

## Who it's for

- **Operator** — owner/admin; the person running the box.
- **Household** — end users; need easy apps on their devices, simple sign-in, and a way
  to request titles (Jellyseerr).

> Each deployment should inventory its own household members, their devices
> (TV/stick/phone), and whether they watch off-LAN. That inventory drives the
> **remote access** decision — see [ADR-0007](../decisions/adr-0007-remote-access.md).

## Decisions already made (ADRs)

- [ADR-0002](../decisions/adr-0002-host-minipc-debian-docker.md) — Host: low-power
  mini-PC, Debian, Docker Compose. **Accepted**
- [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md) — single
  `/data` root on the HDD, hardlink imports; SSD holds config + cache. **Accepted**
  (supersedes ADR-0003, whose SSD-downloads split rested on circular reasoning)
- [ADR-0004](../decisions/adr-0004-jellyfin-media-server.md) — Jellyfin + Jellyseerr.
  **Accepted**
- [ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md) — Full *arr stack;
  qBittorrent/NZBGet/Prowlarr behind Gluetun+ProtonVPN. **Accepted**
- [ADR-0006](../decisions/adr-0006-traefik-local-remote-access-open.md) — Traefik
  `*.local` proxy; **remote access still OPEN**. **Proposed**

## Still open / the real gaps

1. **Remote access for off-LAN family** — none designed; LAN-only today (ADR-0006).
   The single biggest decision left (→ future ADR-0007).
2. **Off-box backups** — current backups sit on the same HDD as the library; not a real
   backup.
3. **Traefik dashboard is unauthenticated** (`--api.insecure=true`) — harden before any
   exposure.
4. **`:latest` + Watchtower auto-update** on a family box — decide pin-vs-auto.
5. ~~**Compose needs rewriting**~~ — **done 2026-08-04**: `docker-compose.yml` +
   `.env.example` written at the repo root on the ADR-0008 `/data` root, fully
   parameterised. Not yet deployed.
6. **Verify on the host** — transcode/QuickSync capability before relying on it.
7. ~~**Overseerr vs Jellyseerr**~~ — resolved: Overseerr dropped from the compose per ADR-0004.

## Success criteria (v1 done = )

- [ ] Stack deployed on the host; all services healthy (`docker compose ps`).
- [ ] Gluetun connected; qBittorrent/NZBGet/Prowlarr use the VPN IP (kill switch verified).
- [ ] A family request in Jellyseerr → acquired → moved+renamed to HDD → plays in Jellyfin.
- [ ] Each family member signs in on their own device and plays (on LAN at minimum).
- [ ] **Remote access** works for ≥1 off-LAN family member (after ADR-0007).
- [ ] Stack restarts cleanly after a host reboot; HDD auto-mounts.
- [ ] Config backed up **off-box** and a restore tested.

## The layers

Hardware → Storage → Media server → Acquisition → Networking/remote → Deployment.
Ordered build path: `roadmap.md`.

## Links
- [Roadmap](roadmap.md) · [Source research](../references/source-research.md)
- [Hardware](hardware.md) · [Storage](storage.md) · [Media server](media-server.md)
- [Acquisition](acquisition-stack.md) · [Networking](networking-remote-access.md) · [Deployment](deployment.md)
