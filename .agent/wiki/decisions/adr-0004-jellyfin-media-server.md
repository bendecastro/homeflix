# ADR 0004: Jellyfin as the media server; Jellyseerr for requests

Date: 2026-06-14

## Status
Accepted

> Source: prior private design package (see `references/source-research.md`).

## Context
The family-facing player layer needed a choice among Jellyfin / Plex / Emby. Prior
design chose Jellyfin (fully free/open, self-hosted, no account dependency or paywall)
and paired it with Jellyseerr for family requests. Overseerr is also present in the
compose as an alternative.

## Decision
- **Media server:** Jellyfin (`linuxserver/jellyfin`), port 8096, served at
  `jellyfin.local` via Traefik. Libraries: `/data/movies`, `/data/tv`, `/data/music`
  bind-mounted read-only from `${DATA_ROOT}/media` (ADR-0008). Transcode cache on SSD
  (`${CACHE_ROOT}/jellyfin-cache`).
- **Requests:** Jellyseerr (`fallenbagel/jellyseerr`), port 5055, `jellyseerr.local`.
  Overseerr stays defined but Jellyseerr is the chosen request portal (it pairs with
  Jellyfin). Decide whether to drop Overseerr to reduce surface.

## Consequences
- No third-party account dependency; full control. Client-app quality varies by
  device — verify the Jellyfin app exists and works on each family device
  (`project/overview.md`).
- On Celeron/8GB (ADR-0002), prefer **direct play**; confirm hardware transcoding
  before promising transcoded remote streams.
- Running both Jellyseerr and Overseerr is redundant — pick one.

## Links
- `project/media-server.md`, `project/overview.md`, `conventions/media-naming.md`
