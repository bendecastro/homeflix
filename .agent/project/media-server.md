# homeflix — Media Server (player layer)

Updated: 2026-06-14
Decision: [ADR-0004](../decisions/adr-0004-jellyfin-media-server.md). Source:
the prior private design package.

**Jellyfin** is the media server; **Jellyseerr** is the family request portal.

## Jellyfin

- Image: `linuxserver/jellyfin:latest` · container `jellyfin` · port **8096**
- URL: `http://jellyfin.local:8096` (also routed via Traefik at `jellyfin.local`)
- Env: PUID=1000, PGID=1000, TZ=UTC, `JELLYFIN_PublishedServerUrl=http://jellyfin.local`
- Volumes:
  - config → `${CONFIG_ROOT}/jellyfin`
  - cache → `${CACHE_ROOT}/jellyfin`
  - `${DATA_ROOT}/media` → `/data/media` (**read-only**)
- Libraries to add in-app: Movies `/data/media/movies`, TV `/data/media/tv`,
  Music `/data/media/music`.
- Paths per [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md).

## Jellyseerr (requests)

- Image: `fallenbagel/jellyseerr:latest` · port **5055** · `jellyseerr.local`
- Config → `${CONFIG_ROOT}/jellyseerr`
- The family-facing "request a movie/show" front door; wires to Jellyfin + Radarr/Sonarr.

> Overseerr is also defined in the compose as an alternative — **pick one**; running
> both is redundant surface (ADR-0004).

## Transcoding

Celeron/8GB host (ADR-0002): prefer **direct play**; verify QuickSync before promising
transcoded streams. Transcode cache is on the SSD.

## Accounts

An admin account plus one per household member; consider per-user libraries / parental
limits for kids. Ties into remote access — `project/networking-remote-access.md`.

## Open / to verify

- [ ] Jellyfin client app exists and works on each family device.
- [ ] Hardware transcode works (QuickSync).
- [ ] Drop Overseerr or Jellyseerr (choose one).
- [ ] Remote access for off-LAN family — currently LAN-only ([ADR-0006](../decisions/adr-0006-traefik-local-remote-access-open.md)).

## Links
- [Overview](overview.md) · [Storage](storage.md) · [Acquisition](acquisition-stack.md)
- [Networking](networking-remote-access.md) · [Media naming](../conventions/media-naming.md)
