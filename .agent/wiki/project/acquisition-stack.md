# homeflix — Acquisition Stack

Updated: 2026-06-14
Decision: [ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md). Source:
the prior private design package (see `references/source-research.md`).

The automated pipeline: family request → search → download → rename/move → appears in
Jellyfin. All `linuxserver/*` images except Jellyseerr.

> Acquire only content legally entitled to; the VPN is privacy hygiene, not a license.

## Components

| Role | Service | Port | VPN? | URL |
|---|---|---|---|---|
| Requests (family) | Jellyseerr | 5055 | direct | jellyseerr.local |
| Movies | Radarr | 7878 | direct | radarr.local |
| TV | Sonarr | 8989 | direct | sonarr.local |
| Music | Lidarr | 8686 | direct | lidarr.local |
| Subtitles | Bazarr | 6767 | direct | bazarr.local |
| Indexers | Prowlarr | 9696 | **VPN** | prowlarr.local |
| Torrents | qBittorrent | 6969 (WebUI), 6881 | **VPN** | qbittorrent.local |
| Usenet | NZBGet | 6789 | **VPN** | nzbget.local |

## VPN split (deliberate)

Only the three risky services route through **Gluetun → ProtonVPN** via
`network_mode: container:gluetun`: **qBittorrent, NZBGet, Prowlarr**. Everything else
is direct on `traefik-network`. Rationale + the per-service risk analysis:
[ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md) and
the prior private design package. Gluetun details live in
`project/networking-remote-access.md`.

- The *arr apps talk to the VPN'd services at **`gluetun:<port>`** (not `localhost`):
  qBittorrent `gluetun:6969`, NZBGet `gluetun:6789`, Prowlarr `gluetun:9696`.
- Kill switch on: VPN down ⇒ those three lose network (fail safe) ⇒ **no downloads
  until VPN recovers**.

## The hardlink-import wiring (critical)

Per [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md): in
Radarr/Sonarr/Lidarr set **Completed Download Handling = on** and **"Use Hardlinks
instead of Copy" = enabled** (the default),
Rename = on. The *arr apps mount the **single root** `${DATA_ROOT}:/data`; root folders
are `/data/media/movies`, `/data/media/tv`, `/data/media/music`. Download client host
`gluetun`, with categories movies/tv/music. qBittorrent saves to `/data/torrents/<cat>`,
NZBGet to `/data/usenet/<cat>`.

Because downloads and media share one filesystem, imports are **instant hardlinks** — no
copy, no second copy of the data, and the torrent keeps seeding from `torrents/`
indefinitely.

> **Do not** give an *arr app `torrents/` and `media/` as separate bind mounts — Docker
> makes them distinct mountpoints and hardlinks silently fail. See
> `references/gotchas.md`.

## Indexers / credentials

Indexer logins, API keys, ProtonVPN + usenet provider creds are **secrets** — only in
the host `.env`, never here. Record *which* indexers/providers are configured, not the
keys. See `conventions/secrets.md`.

## Links
- [Storage](storage.md) · [Media naming](../conventions/media-naming.md) · [Secrets](../conventions/secrets.md)
- [Networking / VPN](networking-remote-access.md) · [Media server](media-server.md) · [Paths](../references/paths.md)
