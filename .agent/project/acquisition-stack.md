# homeflix — Acquisition Stack

Updated: 2026-08-17
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
- CLI selection is `--clients {torrent,usenet,both}` on the acquisition phase
  (default `torrent`, then last successful selection). Prowlarr always starts.
  Unselected download clients stay stopped and are not required by verify.
  There is no `usenet` phase. NZBGet news servers stay disabled until
  `secrets usenet` on a controlling terminal. Missing indexer or news-server
  credentials is `credentials_required`, not `verified`.
- This selection path is **fixture-accepted only**. It is not live production
  verification and does not prove an authorized request-to-library run.

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

Core setup reconciles two Jellyfin connections in Radarr and Sonarr (path-targeted
`/Library/Media/Updated` plus an unconditional `Library/Refresh` webhook). The
built-in targeted Test is not proof that a new title will appear, and
`/Library/Media/Updated` is not a full-library scan. Fixture-accepted only.

> **Do not** give an *arr app `torrents/` and `media/` as separate bind mounts — Docker
> makes them distinct mountpoints and hardlinks silently fail. See
> `references/gotchas.md`.

## Bazarr (subtitles)

Bazarr is in `docker-compose.yml` and starts with the stack, but an empty config does
**not** download anything until it is wired. Portable first-time setup:

- Human guide: [`docs/bazarr.md`](../../docs/bazarr.md)
- Automation: [`scripts/configure-bazarr.sh`](../../scripts/configure-bazarr.sh)

Recommended defaults encoded there (and in the script):

| Concern | Homeflix default |
|---|---|
| Radarr / Sonarr host | Docker DNS names `radarr` / `sonarr` (never `localhost`) |
| Path mappings | **None** under stock mounts — Bazarr has `${DATA_ROOT}/media` → `/data/media`, matching Radarr/Sonarr media paths |
| Language profile | **English**: forced (foreign parts only) + full dialogue; default for new movies/series |
| Subtitle files | Alongside media, multi-language filenames (`Movie.en.srt`, `Movie.en.forced.srt`) |
| Providers | Free set: yifysubtitles, gestdown, bsplayer, tvsubtitles, supersubtitles, embeddedsubtitles; optional OpenSubtitles.com with a free account |
| Jellyfin | Optional immediate library refresh via a dedicated API key |

Existing titles need a mass-edit profile assignment once; titles added after the defaults
are set inherit them. Wanted search runs on Bazarr’s schedule after sync.

## Indexers / credentials

Indexer logins, API keys, ProtonVPN + usenet provider creds are **secrets** — only in
the host `.env`, never here. Record *which* indexers/providers are configured, not the
keys. See `conventions/secrets.md`.

## Links
- [Bazarr setup (docs)](../../docs/bazarr.md) · [Bazarr wiki](https://wiki.bazarr.media/)
- [Storage](storage.md) · [Media naming](../conventions/media-naming.md) · [Secrets](../conventions/secrets.md)
- [Networking / VPN](networking-remote-access.md) · [Media server](media-server.md) · [Paths](../references/paths.md)
