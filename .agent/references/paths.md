# References — Paths, Mounts, URLs & Ports

Updated: 2026-08-04
Storage layout per [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md).
No secrets here.

## Host & deploy

| Thing | Value |
|---|---|
| Reference host | Low-power x86-64 mini-PC, Debian |
| This wiki | `.agent/` in the homeflix repo |
| Provenance | prior private package (unpublished — see `source-research.md`) |
| Deploy method | public git repo, cloned on the host; `git pull && docker compose up -d` |

## Storage mounts

| Variable | Device / mount | FS | Purpose |
|---|---|---|---|
| `${CONFIG_ROOT}` = `${CONFIG_ROOT}` | internal 1TB SSD | host | per-service config + DBs (~2–20GB) |
| `${CACHE_ROOT}` = `${CACHE_ROOT}` | internal 1TB SSD | host | transcode scratch (~10–50GB) |
| `${DATA_ROOT}` = `${DATA_ROOT}` | external 4TB HDD (USB3) | ext4 | `torrents/` + `usenet/` + `media/` — **one filesystem** |

⚠️ Not under `/home` — TRaSH warns restrictive home-dir permissions break containers.
| `${BACKUP_ROOT}` = `${BACKUP_ROOT}` | same HDD | ext4 | config tarballs (⚠️ not off-box) |

### Container mounts (hardlink-safe)

| Service(s) | Bind mount |
|---|---|
| Radarr / Sonarr / Lidarr | `${DATA_ROOT}:/data` — **single root, never split** |
| qBittorrent | `${DATA_ROOT}/torrents:/data/torrents` |
| NZBGet | `${DATA_ROOT}/usenet:/data/usenet` |
| Jellyfin | `${DATA_ROOT}/media:/data/media:ro` + `${CACHE_ROOT}:/cache` |
| all | `${CONFIG_ROOT}/<service>:/config` |

Splitting `/data` into separate `torrents` + `media` mounts for an *arr app breaks
hardlinks silently — see `gotchas.md`.

## Service URLs / ports

| Service | Host rule | Port | VPN | Notes |
|---|---|---|---|---|
| Traefik dashboard | traefik.homeflix | 8080 | – | `api.insecure=true` (harden!) |
| Jellyfin | jellyfin.homeflix | 8096 | – | also published on host:8096 |
| Jellyseerr | jellyseerr.homeflix | 5055 | – | family requests |
| Overseerr | overseerr.homeflix | 5000 | – | alt — drop one |
| Radarr | radarr.homeflix | 7878 | – | |
| Sonarr | sonarr.homeflix | 8989 | – | |
| Lidarr | lidarr.homeflix | 8686 | – | |
| Bazarr | bazarr.homeflix | 6767 | – | |
| Prowlarr | prowlarr.homeflix | 9696 | **VPN** | via gluetun |
| qBittorrent | qbittorrent.homeflix | 6969 (WebUI), 6881 t/u | **VPN** | via gluetun |
| NZBGet | nzbget.homeflix | 6789 | **VPN** | via gluetun |
| Glances | glances.homeflix | 61208 | – | |
| Gluetun control | (internal) | 8888 | – | healthcheck |

*arr → download/indexer host is **`gluetun`** (e.g. `gluetun:6969`), not localhost.

## External / remote

| Thing | Value |
|---|---|
| LAN DNS for `*.homeflix` | router / Pi-hole / `/etc/hosts` (serve locally; not a reserved TLD) |
| Remote access | **OPEN** — none yet (ADR-0006 → future ADR-0007) |
| VPN egress | ProtonVPN, Netherlands (Gluetun) |
