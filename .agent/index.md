# homeflix Agent Wiki

The local, project-scoped agent wiki for **homeflix** — a self-hosted streaming
service for a household. It is the agent's brain for designing, building, and
running homeflix.

> **Status:** not greenfield. A full stack was already designed in a prior private
> package (mini-PC/Debian/Docker · Jellyfin · *arr + Gluetun/VPN · Traefik). That design
> is folded into these pages as ADRs 0002–0006, one of which (0003) was later reversed by
> ADR-0008. Remaining work = deploy → verify → close gaps (remote access, off-box
> backups, hardening). See [overview](project/overview.md).

**Two doors.** This wiki explains *why* homeflix is built the way it is. If you just want
to run it, you want [`docs/`](../docs/) instead —
[quickstart](../docs/quickstart.md) · [hardware](../docs/hardware.md) ·
[configuration](../docs/configuration.md) · [bazarr](../docs/bazarr.md). Nothing here is
required reading to deploy.

## Start here

- [Agent maintainer instructions](AGENTS.md) — **read this; you maintain this wiki**
- [Home](home.md)
- [Context map](map.md) — pick the smallest context set for your task
- [Project overview](project/overview.md) — what homeflix is, who it's for, success criteria
- [**Build roadmap**](project/roadmap.md) — the ordered path from hardware to a family-ready service
- [Active task context](tasks/active.md) — the live cursor
- [Agent log](log.md) — chronological journal

## Catalog

### Project (durable architecture & plans)
- [overview.md](project/overview.md) — goals, family users, scope, success criteria
- [roadmap.md](project/roadmap.md) — phased build plan (the spine)
- [hardware.md](project/hardware.md) — host machine(s), drives, peripherals
- [storage.md](project/storage.md) — filesystem, pooling/RAID, capacity, layout
- [media-server.md](project/media-server.md) — the player layer (Jellyfin/Plex/Emby — TBD)
- [acquisition-stack.md](project/acquisition-stack.md) — *arr stack, indexers, download clients, requests
- [networking-remote-access.md](project/networking-remote-access.md) — reverse proxy, DNS, VPN, family remote access
- [deployment.md](project/deployment.md) — host OS, container runtime, compose layout, backups

### References
- [source-research.md](references/source-research.md) — provenance: the prior private package this builds on
- [commands.md](references/commands.md) — recurring commands
- [paths.md](references/paths.md) — drives, mounts, URLs, ports
- [gotchas.md](references/gotchas.md) — traps and their fixes
- [external-links.md](references/external-links.md) — TRaSH guides, service docs

### Conventions
- [media-naming.md](conventions/media-naming.md) — library file/folder naming scheme
- [secrets.md](conventions/secrets.md) — how secrets are handled (none in this wiki)
- [git-and-commit-policy.md](conventions/git-and-commit-policy.md)

### Decisions
- [ADR-0001: agent wiki for homeflix](decisions/adr-0001-agent-wiki-for-homeflix.md)
- [ADR-0002: host mini-PC/Debian/Docker](decisions/adr-0002-host-minipc-debian-docker.md) — Accepted
- [ADR-0003: two-tier storage, move-not-hardlink](decisions/adr-0003-two-tier-storage-move-not-hardlink.md) — **Superseded by ADR-0008**
- [ADR-0004: Jellyfin + Jellyseerr](decisions/adr-0004-jellyfin-media-server.md) — Accepted
- [ADR-0005: *arr stack + Gluetun/ProtonVPN](decisions/adr-0005-arr-stack-gluetun-protonvpn.md) — Accepted
- [ADR-0006: Traefik proxy; remote access OPEN](decisions/adr-0006-traefik-local-remote-access-open.md) — Proposed
- [ADR-0007: remote access — Tailscale primary, Cloudflare fallback](decisions/adr-0007-remote-access.md) — Proposed (gated on device inventory)
- [ADR-0008: single-filesystem `/data` root, hardlink imports](decisions/adr-0008-single-filesystem-data-root-hardlinks.md) — Accepted (supersedes 0003)

### Tasks
- [active.md](tasks/active.md) · [parking-lot.md](tasks/parking-lot.md) · [completed.md](tasks/completed.md)

### Templates
- [plan.md](templates/plan.md) · [adr.md](templates/adr.md) · [service.md](templates/service.md)

## Scope

This wiki **may** contain: homeflix architecture and design notes; verified
hardware/OS/network facts; per-service config notes (images, ports, volumes);
project decisions; recurring commands, paths, and gotchas; media-naming conventions.

This wiki **must not** contain: personal (non-homeflix) wiki content; secrets,
credentials, API keys, indexer logins, or real `.env` values; long raw logs;
hardcoded home-directory paths in anything portable.

This wiki is also a standalone Obsidian vault rooted at `.agent/`.

## Maintenance policy

**Read [AGENTS.md](AGENTS.md) — you are this wiki's maintainer and must keep it
live, not just read it.** In short: when you learn something durable, update the
smallest relevant page and append to `log.md`; track in-flight work in
`tasks/active.md`; record forks as ADRs in `decisions/`; keep plan statuses and step
checkboxes current. The full update-trigger table and session protocol live in
[AGENTS.md](AGENTS.md).
