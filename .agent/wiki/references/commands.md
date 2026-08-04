# References — Commands

Updated: 2026-06-14
Source: prior private design package (see `references/source-research.md`). Verify on the host;
summarize output, never paste secrets.

## Stack lifecycle (from the deploy dir on the host)

```bash
docker compose up -d            # start all
docker compose down             # stop all
docker compose ps               # status
docker compose restart <svc>    # restart one
docker logs -f <svc>            # follow logs
docker compose pull && docker compose up -d   # update
```

## VPN checks (Gluetun)

```bash
docker exec gluetun curl -s https://api.ipify.org    # should show ProtonVPN IP, not home IP
docker logs -f gluetun
curl http://traefik.local:8080/api/http/routers      # Traefik routes
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
grep "^## \[" log.md | tail -5   # last 5 log entries (run from .agent/wiki/)
```
