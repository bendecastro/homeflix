# homeflix — Deployment

Updated: 2026-08-14
Decisions: [ADR-0002](../decisions/adr-0002-host-minipc-debian-docker.md) (host),
[ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md) (storage layout).
Source: prior private design package (see `references/source-research.md`).

Host OS, runtime, how the stack is defined/started, monitoring, and backups.

## Host + runtime

- Debian on the mini-PC host; Docker + Docker Compose. See `project/hardware.md`.
- One `docker-compose.yml` defines all services on the `traefik-network` bridge, plus
  the Gluetun netns for the three VPN services.

## The source stack

**Written 2026-08-04:** `homeflix/docker-compose.yml` + `.env.example` + `.gitignore` now
exist at the homeflix root, rebuilt from the prior private design package onto the
ADR-0008 single `/data` root with every path parameterised. Validates with
`docker compose config`. **Not yet deployed or run against real services.**

Three correctness fixes were made versus the prior package's compose:
1. **Traefik could not route to the VPN'd services.** qBittorrent/NZBGet/Prowlarr use
   `network_mode: container:gluetun`, so they have no IP of their own and Traefik's Docker
   provider cannot discover them. Their routers are now declared on the **gluetun**
   container, which is on `traefik-network`.
2. **The gluetun healthcheck was broken three ways** — it probed `:8888/v1/openvpn/status`;
   the control server is on `:8000`, the route is `/v1/vpn/status`, and it now requires
   auth by default. Removed in favour of gluetun's own built-in tunnel healthcheck.
3. **Bazarr's media mount must stay writable** (it writes subtitle files alongside the
   media); only Jellyfin gets `:ro`.

Also: Overseerr dropped (ADR-0004 chose Jellyseerr); `version:` and the empty `volumes: {}`
removed (obsolete in Compose v2); images moved to `lscr.io/linuxserver/*` with tags
parameterised so they can be pinned.

homeflix is intended to become a **public, replicable repo**, so paths must be
parameterised rather than hardcoded to this box. Any given deployment is then just a `.env`.

> Target layout:
> ```
> homeflix/ (public repo)
> ├── README.md               # architecture + quickstart
> ├── docker-compose.yml      # fully parameterised, single /data root
> ├── .env.example            # every var documented; real .env never committed
> ├── docs/                   # for replicators
> ├── scripts/                # bootstrap.sh, backup.sh, monitor-disk.sh
> └── .agent/                 # this wiki (the build record)
> ```
> Host runtime dirs, created on the box (never committed):
> ```
> ${CONFIG_ROOT}  ${CACHE_ROOT}          (SSD)
> ${DATA_ROOT}/{torrents,usenet,media}   (HDD, one filesystem)
> ```

**Still open:** repo placement + how the code reaches the host (git clone on the host
is the leading option; Syncthing into a live compose dir risks `.sync-conflict-*` files).
Also whether the wiki moves into the repo. See `tasks/active.md`.

## Monitoring & lifecycle

- **Glances** (`glances.${DOMAIN}`) — host CPU/mem/process via `pid: host`. No
  Docker socket (per-container stats dropped).
- **deunhealth** — restarts unhealthy containers **only** where a `healthcheck` and
  `deunhealth.restart.on.unhealthy=true` are both present. This compose does not yet
  attach that pair to the VPN'd services.
- **Watchtower** — auto-update + cleanup, daily at 02:00.
- All services `restart: unless-stopped`.
- `depends_on: condition: service_healthy` applies to `docker compose up`, **not** to
  daemon restarts on boot. Do not add a second supervisor for boot order.
- ⚠️ **Tension:** every image is pinned to `:latest` AND Watchtower auto-updates on a
  family-critical box → a bad upstream image can silently break things overnight.
  Decide: pin real version tags + notify-only, or accept auto-update. Record as a
  follow-up. See `references/gotchas.md`.

## Backups

`scripts/backup-config.sh` snapshots `${CONFIG_ROOT}` (not media, not the checkout
`.env`) to `BACKUP_DEST` and keeps `BACKUP_KEEP` dated archives. SQLite files are
replaced with `sqlite3 .backup` copies so a live database is not torn. Destination
lives only in `.env`.

The prior same-disk `backup.sh` stays retired. A backup that has never been restored
is not evidence — use `scripts/restore-config.sh --to` a scratch directory.

### Restore runbook (dead host, you did not write this)

1. `./scripts/restore-config.sh --list`
2. `./scripts/restore-config.sh --to /tmp/homeflix-restore-test`
3. Spot-check: `sqlite3 /tmp/homeflix-restore-test/radarr/radarr.db 'SELECT COUNT(*) FROM Movies;'`
4. Stop the stack if anything is running: `docker compose down`
5. Copy the scratch tree over the new `${CONFIG_ROOT}` (permissions: `PUID`/`PGID`)
6. Restore the checkout `.env` separately (0600), then `docker compose up -d`
7. If this was a drill, delete the scratch directory

## Secrets at deploy time

Real secrets (`PROTONVPN_*`, indexer keys, usenet creds) live in the host `.env`
(gitignored) — never in this wiki, never committed. See `conventions/secrets.md`.

## Links
- [Hardware](hardware.md) · [Storage](storage.md) · [Networking](networking-remote-access.md)
- [Commands](../references/commands.md) · [Gotchas](../references/gotchas.md) · [Secrets](../conventions/secrets.md)
