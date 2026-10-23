# Seerr migration plan

Status: Proposed
Updated: 2026-10-23

## Goal
Replace the archived Jellyseerr 2.7.3 request portal with its maintained successor,
Seerr, and rename every Homeflix identifier from `jellyseerr` to `seerr`. Done means:
the repo ships the `seerr` service, the setup CLI configures and verifies it, all
tests pass, docs and wiki say `seerr`, and the owner's deployment runs Seerr with its
migrated requests, users and settings intact.

## Current state
Verified 2026-10-23:

- `docker-compose.yml` runs `fallenbagel/jellyseerr:${JELLYSEERR_TAG:-latest}`, service
  and container `jellyseerr`, `${CONFIG_ROOT}/jellyseerr:/app/config`, Traefik router
  `Host(jellyseerr.${DOMAIN})`, port 5055, no `init`, no healthcheck.
- Docker Hub marks `fallenbagel/jellyseerr` archived ("DO NOT USE… use seerr/seerr");
  its `latest` tag is frozen at 2.7.3. Seerr is the merged Jellyseerr + Overseerr
  project (`ghcr.io/seerr-team/seerr`, v3.5.1 on 2026-10-21). 2.7.3 lacks fixes for
  GHSA-mc6w-69r3-62h8 (avatarproxy path traversal, high) and four medium data-leak
  advisories. Source: researcher report, sources linked in
  [ADR-0012](../decisions/adr-0012-seerr-request-portal.md).
- Seerr runs as fixed UID/GID 1000 (`node`), needs `init: true`, ignores PUID/PGID, and
  migrates `/app/config` in place on first start. No documented reverse migration.
- Setup CLI calls only `/api/v1` status, auth and settings endpoints
  (`scripts/homeflix_setup/api/jellyseerr.py`); none were renamed in Seerr 3.x
  (`blacklist` → `blocklist` is unused). Not yet tested against a live Seerr.
- `api/securepath.py` accepts config owned by root or the operator `PUID` only;
  `core.py` passes `PUID` as the expected owner for the request-portal settings file.
- Owner deployment: `PUID=1000`; config dir owned by 1000 but `db/db.sqlite3` and
  `settings.json` owned by root; 4.6 MB; `main.applicationUrl` empty; Radarr and Sonarr
  have no notification connection to the portal.

## Approach
Swap the image to `ghcr.io/seerr-team/seerr:${SEERR_TAG:-latest}` and rename the
service, container, router, hostname (`seerr.${DOMAIN}`), config directory, tag
variable, CLI identifiers, tests, fixtures and docs in one change. The CLI accepts
UID 1000 as the owner of the Seerr config only; other services keep the `PUID` rule.
Owner decisions 2026-10-23: full rename, `latest` tag, UID 1000 accepted for Seerr only.

## Steps
Repository (one writer; fixture tests gate each step):
- [ ] 1. Compose and env: rename service/container/router/labels to `seerr`, image
      `ghcr.io/seerr-team/seerr:${SEERR_TAG:-latest}`, add `init: true`, volume
      `${CONFIG_ROOT}/seerr:/app/config`; core phase list; `.env.example`
      `JELLYSEERR_TAG` → `SEERR_TAG`.
- [ ] 2. CLI rename: `api/jellyseerr.py` → `api/seerr.py`, `JellyseerrClient` →
      `SeerrClient`, settings path `seerr/settings.json`, hostname `seerr.${DOMAIN}`,
      result/transport keys, allowlists, CLI output (`core.py`, `compose.py`,
      `discover.py`, `cli.py`, `api/__init__.py`).
- [ ] 3. Ownership: Seerr settings reader accepts owner in `{0, 1000}` regardless of
      `PUID`; CLI-created `seerr` config dir is made writable by 1000. Tests for
      PUID=1000 and PUID≠1000.
- [ ] 4. Tests and fixtures: rename `test_api_jellyseerr.py`, `jellyseerr-*.json`
      fixtures, discovery fixtures, and assertions in `test_compose.py`,
      `test_core*.py`, `test_cli.py`, `test_contract.py`, `test_api_jellyfin.py`.
- [ ] 5. Docs: README, `docs/*` (agent-setup, configuration, quickstart, first-use,
      media-library, specs/verification), with an upgrade note for existing
      Jellyseerr installs (step 7 condensed).
- [ ] 6. Wiki: ADR-0012 → Accepted; ADR-0004 image line points at 0012; service pages
      (`media-server.md`, `overview.md`, `paths.md`, `gotchas.md`, others the rename
      touches); `log.md`. Historical log lines stay as written.

Owner deployment on beelink (after repo steps merge; owner runs `sudo` lines):
- [ ] 7. Pull the change into `~/Sync/Work/homeflix-prod`; set `SEERR_TAG=latest`,
      remove `JELLYSEERR_TAG` in its `.env`.
- [ ] 8. `docker stop jellyseerr`; `tar czf ~/jellyseerr-config-2026-10-23.tgz -C
      /opt/homeflix/appdata jellyseerr`.
- [ ] 9. `mv /opt/homeflix/appdata/jellyseerr /opt/homeflix/appdata/seerr`;
      `sudo chown -R 1000:1000 /opt/homeflix/appdata/seerr`.
- [ ] 10. Add `seerr.homeflix` to LAN DNS wherever `jellyseerr.homeflix` is served.
- [ ] 11. `docker compose up -d --remove-orphans seerr` (removes the old
      `jellyseerr` container); watch logs for the migration finishing.
- [ ] 12. `scripts/homeflix verify core`; log in via Jellyfin; confirm existing requests,
      users, Radarr/Sonarr connections and Discover → Networks.

## Validation
- `python3 -m unittest discover -s tests -v`
- `docker compose --env-file .env.example config --quiet`
- `bash -n scripts/preflight.sh`
- `rg -n -i jellyseerr` returns only historical log lines, ADR-0012's context, and the
  upgrade note.
- Live: step 12 checks. Fixture tests do not prove Seerr API compatibility; step 12 is
  the first live check.

## Risks & rollback
- One-way config migration. Rollback: stop `seerr`, restore the step 8 tarball to
  `/opt/homeflix/appdata/jellyseerr`, check out the previous commit in homeflix-prod,
  `docker compose up -d jellyseerr`.
- Hostname change breaks bookmarks and any device that only knows
  `jellyseerr.homeflix`.
- Root-owned DB files make Seerr fail to write if step 9's `chown` is skipped.
- `latest` tag (owner choice) can pull a breaking major release on any `docker compose
  pull`.

## Open questions / blockers
- Where LAN DNS for `*.homeflix` is served for the owner (router, Pi-hole or hosts);
  a wildcard record would make step 10 a no-op.

## Links
- [ADR-0012: Seerr request portal](../decisions/adr-0012-seerr-request-portal.md)
- [ADR-0004: Jellyfin media server](../decisions/adr-0004-jellyfin-media-server.md)
- [ADR-0010: LAN DNS naming](../decisions/adr-0010-lan-dns-naming.md)
- Seerr migration guide: https://docs.seerr.dev/migration-guide/
