# References — Provenance

Updated: 2026-08-04

homeflix is **not greenfield**. It was preceded by a private design package (January
2026) that produced a working 14-service `docker-compose.yml`, a storage layout, a VPN
provider analysis, and a set of setup guides.

That package is **not published**: it contains host secrets (a disk-encryption UUID and
a backup recovery key) alongside the design material, and separating them cleanly isn't
worth the risk. Its durable conclusions were folded into this wiki as ADRs 0002–0006,
which is where the reasoning now lives.

## What carried forward

| From the prior package | Now recorded as |
|---|---|
| Host + OS + container runtime choice | [ADR-0002](../decisions/adr-0002-host-minipc-debian-docker.md) |
| Media server choice (Jellyfin over Plex/Emby) | [ADR-0004](../decisions/adr-0004-jellyfin-media-server.md) |
| *arr stack shape + VPN provider analysis | [ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md) |
| Reverse proxy approach | [ADR-0006](../decisions/adr-0006-traefik-local-remote-access-open.md) |
| Media naming scheme | [conventions/media-naming.md](../conventions/media-naming.md) |

## What did not survive review

The prior package's storage design — downloads on the SSD, library on the HDD, *arr
Import Mode = Move — was adopted as [ADR-0003](../decisions/adr-0003-two-tier-storage-move-not-hardlink.md)
and later **reversed** by [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md).

Its central argument was circular (it treated an unexamined choice as an external
constraint), its stated benefit didn't hold (download speed is bounded by the internet
link, not the disk), and its documented workflow was factually wrong (it claimed
seeding survives a cross-filesystem move — it does not). Its accompanying
`HARDLINK-SETUP.md` guide should not be followed.

This is the most useful thing in the provenance: inherited designs deserve the same
scrutiny as new ones, especially when they arrive as confident, well-formatted
documentation.

## Links
- [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md) — the reversal
- [gotchas.md](gotchas.md) · [external-links.md](external-links.md)
