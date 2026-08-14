# References — Commands

Updated: 2026-08-14
Source: prior private design package (see `references/source-research.md`). Verify on the host;
summarize output, never paste secrets.

## Stack contract (static; no running containers)

```bash
scripts/homeflix --json verify contract
docker compose --env-file .env.example config --format json
```

Renders Compose once, then checks VPN namespace, *arr `/data` binds, self-heal pair,
proxy route ownership, pinned proxy subnet, and disjoint phase allowlists.

## Core runtime verification (read-only)

```bash
scripts/homeflix --json verify core
```

Inspects live Docker, the static stack contract, core service readiness, DATA_ROOT
`/data` identity, a cleaned-up hardlink probe, and application exactness. Unselected
QuickSync is not-applicable. Any mandatory unknown or skip fails the command. Does
not start, stop, or restart Compose services.

## Stack lifecycle (from the deploy dir on the host)

```bash
docker compose up -d            # start all
docker compose down             # stop all
docker compose ps               # status
docker compose restart <svc>    # restart one
docker logs -f <svc>            # follow logs
docker compose pull && docker compose up -d   # update
```

## CONFIG_ROOT backup (off-box)

```bash
./scripts/backup-config.sh                 # snapshot + copy to BACKUP_DEST
./scripts/backup-config.sh --install-cron  # daily 03:15
./scripts/restore-config.sh --list
./scripts/restore-config.sh --to /tmp/homeflix-restore-test
```

Requires `BACKUP_DEST` in `.env`. Media and the checkout `.env` are not included.
Restore refuses to write over live `CONFIG_ROOT`.

## Acquisition VPN gate (non-disruptive)

```bash
scripts/homeflix secrets vpn                 # controlling tty only; writes key names/status
scripts/homeflix --json vpn verify --dry-run # Gluetun-only plan; no secrets
scripts/homeflix --json vpn verify           # contract + acquisition preflight + Gluetun start
```

Evidence is timestamp/image/config identity plus booleans. It expires after 24h or a
relevant image/config change. Output must not include public IPs or secret values.
Do not start qBittorrent, NZBGet, or Prowlarr from this command.

## VPN checks (Gluetun)

```bash
docker logs -f gluetun
```

## Storage / host

```bash
lsblk                           # find drives
df -h ${CONFIG_ROOT} ${DATA_ROOT}   # tier usage
du -sh ${CONFIG_ROOT}/* | sort -h   # SSD breakdown
du -sh --count-links=no ${DATA_ROOT}/*   # real usage (hardlinks counted once)
find ${DATA_ROOT}/media -links 1 -type f  # files NOT hardlinked → hardlinking broke
sudo blkid /dev/sdX1            # UUID for fstab
sudo mount -a                   # test fstab
```

## Setup helpers (ADR-0008 layout)

```bash
# SSD: config + cache only  (NOT under /home — see gotchas)
sudo mkdir -p ${CONFIG_ROOT} ${CACHE_ROOT}

# HDD: ONE filesystem holding downloads + media (so hardlinks work)
# TRaSH reference layout, all lowercase
sudo mkdir -p ${DATA_ROOT}/{torrents/{movies,tv,music},usenet/{incomplete,complete/{movies,tv,music}},media/{movies,tv,music}}
sudo mkdir -p ${BACKUP_ROOT}/{daily,weekly,monthly}

sudo chown -R $USER:$USER /opt/<name> ${DATA_ROOT}
sudo chmod -R a=,a+rX,u+w,g+w ${DATA_ROOT}     # 775 dirs / 664 files
```

Verify hardlinking works before trusting it (TRaSH's check): after one import, the file
under `data/media/` and the one under `data/torrents/` should share an inode.

```bash
ls -li ${DATA_ROOT}/media/movies/*/*.mkv   # column 1 = inode, column 3 = link count
```

## Greppable wiki log

```bash
grep "^## \[" log.md | tail -5   # last 5 log entries (run from .agent/)
```
