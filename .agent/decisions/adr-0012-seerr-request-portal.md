# ADR 0012: Seerr replaces Jellyseerr as the request portal

Date: 2026-10-23

## Status
Proposed

## Context
[ADR-0004](adr-0004-jellyfin-media-server.md) picked Jellyseerr as the request portal.
Jellyseerr and Overseerr have merged into Seerr; Jellyseerr 2.x is end-of-life:

- Seerr 3.0.0 shipped 2026-02-14; 3.5.1 on 2026-10-21
  ([releases](https://github.com/seerr-team/seerr/releases),
  [announcement](https://docs.seerr.dev/blog/seerr-release/)).
- Docker Hub `fallenbagel/jellyseerr` is archived; `latest` is frozen at 2.7.3.
- 2.7.3 lacks fixes for [GHSA-mc6w-69r3-62h8](https://github.com/seerr-team/seerr/security/advisories/GHSA-mc6w-69r3-62h8)
  (high) and medium advisories GHSA-gx3h-3jg5-q65f, GHSA-f7xw-jcqr-57hp,
  GHSA-xc6j-6p6h-p799, GHSA-8j73-j6rc-q8xh.
- Seerr runs as fixed UID 1000, requires `init: true`, ignores PUID/PGID, and migrates
  config one way ([migration guide](https://docs.seerr.dev/migration-guide/)).

## Decision
- Ship `ghcr.io/seerr-team/seerr:${SEERR_TAG:-latest}` with `init: true`.
- Rename every Homeflix identifier to `seerr`: service, container, Traefik router,
  hostname `seerr.${DOMAIN}`, `${CONFIG_ROOT}/seerr`, `SEERR_TAG`, CLI modules and tests.
- The setup CLI accepts UID 1000 as the Seerr config owner regardless of `PUID`; other
  services keep the `PUID` ownership rule.

## Consequences
- Existing installs must back up, move and `chown` the config directory, update LAN DNS
  and bookmarks, and accept a one-way migration. Steps live in
  [seerr-migration-plan.md](../project/seerr-migration-plan.md) and the user docs.
- `latest` follows Seerr majors automatically; a breaking release can land on pull.
- Discover, Networks/Studios browsing and the request flow are unchanged.
