# Seerr migration plan

Status: Proposed (revised after independent review)
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
- `api/securepath.py` checks every path component from `CONFIG_ROOT` down to the file,
  not only the file. The CLI never creates `${CONFIG_ROOT}/<service>` directories;
  Docker creates a missing bind source as root.
- `compose.py` `DIRECT_PORTS` writes a `jellyseerr:` ports block into the gitignored
  `docker-compose.override.yml` when LAN DNS is unresolved.
- Owner deployment: `PUID=1000`; `appdata/` and `appdata/jellyseerr/` owned by 1000;
  `db/` (755), `db/db.sqlite3{,-wal,-shm}` and `settings.json` (644) owned by root;
  4.6 MB; `main.applicationUrl` empty; Radarr and Sonarr have no notification connection
  to the portal. Its `docker-compose.override.yml` contains a `jellyseerr:` 5055 ports
  block.

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
- [ ] 3. Ownership: path components from `CONFIG_ROOT` down to `seerr/` keep the
      `{0, PUID}` rule; only `seerr/` and files under it also accept 1000. Setup creates
      `${CONFIG_ROOT}/seerr` owned by 1000 before the first `up` (Docker would create it
      as root, which Seerr cannot write). `DIRECT_PORTS` and the override generator use
      `seerr`. Tests for
      PUID=1000 and PUID≠1000.
- [ ] 4. Tests and fixtures: rename `test_api_jellyseerr.py`, `jellyseerr-*.json`
      fixtures, the `jellyseerr` key in `fixtures/api/contracts.json`, discovery
      fixtures, and assertions in `test_discover.py`, `test_compose.py`,
      `test_core*.py`, `test_cli.py`, `test_contract.py`, `test_api_jellyfin.py`.
- [ ] 5. Docs: README, `docs/*` (agent-setup, configuration, quickstart, first-use,
      media-library, specs/verification), with an upgrade note for existing
      Jellyseerr installs (step 7 condensed).
- [ ] 6. Wiki: ADR-0012 → Accepted; ADR-0004 image line points at 0012; service pages
      (`media-server.md`, `overview.md`, `storage.md`, `acquisition-stack.md`,
      `roadmap.md`, `paths.md`, `gotchas.md`, `external-links.md`, `index.md`); `log.md`.
      Historical records stay as written: `log.md` entries, ADR-0004's original choice,
      completed plans.

Owner deployment on beelink (after repo steps merge; owner runs `sudo` lines). Order
matters: the config directory moves before the new compose file is pulled, so no
`compose up` can create an empty `seerr/` first.
- [ ] 7. `docker stop jellyseerr` (flushes the SQLite WAL).
- [ ] 8. `sudo tar czf ~/jellyseerr-config-2026-10-23.tgz -C /opt/homeflix/appdata
      jellyseerr`; `tar tzf` it and confirm `settings.json` and `db/db.sqlite3` are listed.
- [ ] 9. `mv /opt/homeflix/appdata/jellyseerr /opt/homeflix/appdata/seerr`;
      `sudo chown -R 1000:1000 /opt/homeflix/appdata/seerr`.
- [ ] 10. Add `seerr.homeflix` to LAN DNS wherever `jellyseerr.homeflix` is served.
- [ ] 11. In `~/Sync/Work/homeflix-prod`: copy `.env` and `docker-compose.override.yml`
      to `*.pre-seerr`; pull the change; in `.env` replace `JELLYSEERR_TAG` with
      `SEERR_TAG=latest`; in the override rename the `jellyseerr:` block to `seerr:` (or
      re-run `scripts/homeflix configure`, which regenerates it).
- [ ] 12. `docker compose pull seerr && docker compose up -d seerr`; watch logs until
      the migration finishes; then `docker rm jellyseerr`.
- [ ] 13. `scripts/homeflix verify core`; log in via Jellyfin; confirm existing requests,
      users, Radarr/Sonarr connections and Discover → Networks.

## Validation
- `python3 -m unittest discover -s tests -v`
- `docker compose --env-file .env.example config --quiet`
- `bash -n scripts/preflight.sh`
- Docs and wiki: `rg -n -i jellyseerr` is the only check that catches a missed rename
  outside code; unit tests do not read docs, and `compose config` passes even if both
  services exist. Allowed leftovers: `log.md` entries, ADR-0004, ADR-0012, completed
  plans, and the upgrade note. Also assert `docker compose config --services` lists
  `seerr` and not `jellyseerr`.
- Live: step 13 checks. Fixture tests do not prove Seerr API compatibility; step 13 is
  the first live check.

## Risks & rollback
- One-way config migration. Rollback: `docker stop seerr && docker rm seerr`; move
  `appdata/seerr` aside; `sudo tar xzf` the step 8 tarball into `/opt/homeflix/appdata`;
  restore `.env.pre-seerr` and `docker-compose.override.yml.pre-seerr`; check out the
  previous commit in homeflix-prod; `docker compose up -d jellyseerr`.
- Hostname change breaks bookmarks and any device that only knows
  `jellyseerr.homeflix`.
- Root-owned DB files make Seerr fail to write if step 9 `chown` is skipped.
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
