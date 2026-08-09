# Homeflix

A self-hosted streaming service for your household — a private "Netflix" backed by a
media library you own, running on a single low-power mini-PC.

Jellyfin serves the library. Jellyseerr lets the household request titles. The *arr apps
manage acquisition, with the download clients and indexer manager isolated behind a VPN
with a kill switch. Traefik routes everything to friendly `*.local` names.

```bash
git clone https://github.com/bendecastro/homeflix.git
cd homeflix
```

## Agent-assisted setup

Open the checkout with a capable coding agent and paste one of these exact intents:

- `Set up Homeflix core on this Debian or Ubuntu machine using my existing mounted storage.`
- `Resume Homeflix core setup and verify the live deployment.`
- `Show me a dry-run plan for Homeflix core setup without changing this machine.`

The current CLI supports **local Debian/Ubuntu with existing mounted storage**. It runs in the
target checkout and does not provide SSH transport; an agent may use its own SSH capability.
Core includes Traefik, Jellyfin, Jellyseerr, Radarr, and Sonarr. Encrypted storage and acquisition
(VPN/download/indexer services) are planned follow-ups, not shipped setup phases. Automation is
fixture-tested only and has not received disposable real-host Debian/Ubuntu acceptance.

Start with the [agent setup guide](docs/agent-setup.md), which requires capability discovery,
reviewed mutation plans, secure human gates, and live evidence before completion.

```
                        ┌──────────┐
   household ──────────▶│ Traefik  │
                        └────┬─────┘
             ┌───────────────┼────────────────┐
             ▼               ▼                ▼
      ┌────────────┐  ┌────────────┐   ┌──────────────┐
      │  Jellyfin  │  │ Jellyseerr │   │ Radarr/Sonarr│
      │  (play)    │  │ (requests) │   │ Lidarr/Bazarr│
      └─────┬──────┘  └──────┬─────┘   └───────┬──────┘
            │                │                 │
            │                └────────────────▶│
            │                                  ▼
            │                    ┌─────────────────────────┐
            │                    │  Gluetun (VPN + kill    │
            │                    │  switch) — shared netns │
            │                    │  ┌───────────────────┐  │
            │                    │  │ qBittorrent       │  │
            │                    │  │ NZBGet · Prowlarr │  │
            │                    │  └───────────────────┘  │
            │                    └───────────┬─────────────┘
            │                                │
            ▼                                ▼
   ┌──────────────────────────────────────────────────────┐
   │  $DATA_ROOT — ONE filesystem                         │
   │    torrents/   usenet/   media/                      │
   │    imports are hardlinks: instant, no extra disk,    │
   │    and torrents keep seeding afterwards              │
   └──────────────────────────────────────────────────────┘
```

## Manual quickstart fallback

Prefer the agent-assisted path above on its supported hosts. Manual operation requires a Linux
host with Docker and the Compose plugin.

```bash
git clone https://github.com/bendecastro/homeflix.git
cd homeflix
cp .env.example .env
```

Edit `.env` — at minimum `DATA_ROOT`, `CONFIG_ROOT`, `CACHE_ROOT`, and your VPN
credentials. Then create the storage layout:

```bash
sudo mkdir -p "$DATA_ROOT"/{torrents/{movies,tv,music},usenet/{incomplete,complete/{movies,tv,music}},media/{movies,tv,music}}
sudo chown -R $USER:$USER "$DATA_ROOT"
```

Verify the host is correctly set up, then start:

```bash
./scripts/preflight.sh
docker compose up -d
```

**Run `preflight.sh`.** It doesn't just check that files exist — it creates a real
hardlink between `torrents/` and `media/` and confirms they share an inode. That single
check catches the failure mode that quietly ruins most setups of this kind. Full
walkthrough: [`docs/quickstart.md`](docs/quickstart.md).

## What you get

| Service | Purpose | Default host |
|---|---|---|
| Jellyfin | Media server / playback | `jellyfin.local` |
| Jellyseerr | Household request portal | `jellyseerr.local` |
| Radarr · Sonarr · Lidarr | Movie / TV / music management | `radarr.local`, … |
| Bazarr | Subtitles | `bazarr.local` |
| Prowlarr | Indexer manager *(VPN)* | `prowlarr.local` |
| qBittorrent · NZBGet | Download clients *(VPN)* | `qbittorrent.local`, `nzbget.local` |
| Traefik | Reverse proxy | `traefik.local` |
| Glances · deunhealth · Watchtower | Monitoring & lifecycle | `glances.local` |

`*.local` names need LAN DNS — your router, a Pi-hole, or `/etc/hosts` on each device.
Traefik routes them; it doesn't resolve them.

## Why it's built this way

**One filesystem for downloads and media.** `$DATA_ROOT` holds `torrents/`, `usenet/`
and `media/` together, so the *arr apps import by **hardlink** — a second name for the
same data. Imports are instant, cost no extra disk, and the torrent keeps seeding from
its original path. The *arr apps mount the single root `/data`; splitting that into
separate `torrents` and `media` mounts silently breaks hardlinking even on one physical
disk, because Docker presents them as distinct mountpoints.
→ [ADR-0008](.agent/decisions/adr-0008-single-filesystem-data-root-hardlinks.md)

**Download clients share the VPN's network namespace.** qBittorrent, NZBGet and Prowlarr
run with `network_mode: container:gluetun`, so they have no network path that isn't the
tunnel. The kill switch fails closed: if the VPN drops, those three lose connectivity
entirely rather than leaking. They're reachable as `gluetun:<port>`, never localhost.
→ [ADR-0005](.agent/decisions/adr-0005-arr-stack-gluetun-protonvpn.md)

**Everything is an `.env` variable.** No paths, ports, domain, timezone, VPN provider or
image tag is hardcoded. A deployment is a `.env` file.

Full reasoning: [`docs/`](docs/) for running it, the
[ADRs](.agent/decisions/) for why.

## Requirements

Developed against a deliberately modest reference build — a Celeron-class mini-PC with
8GB RAM and a USB3 external drive — so the design never assumes headroom. Notably it
**prefers direct play over transcoding**, because that hardware can't do much of it.

Works on a single drive or two; `CONFIG_ROOT` and `DATA_ROOT` are independent. The only
hard requirement is that `DATA_ROOT` is internally one filesystem.
→ [`docs/hardware.md`](docs/hardware.md)

## How this was built

This repo carries its own engineering record. `.agent/` is a project-scoped wiki
maintained by an AI agent across sessions — architecture pages, a build roadmap, a
gotchas file, dated decision records, and an append-only log.

It's included deliberately, and not as a tidy after-the-fact writeup. The most useful
thing in it is a mistake: [ADR-0003](.agent/decisions/adr-0003-two-tier-storage-move-not-hardlink.md)
specified downloads on the SSD and the library on the HDD, and was accepted.
[ADR-0008](.agent/decisions/adr-0008-single-filesystem-data-root-hardlinks.md)
later reversed it — the original argument turned out to be circular, its stated benefit
didn't survive scrutiny, and the workflow it documented was factually wrong about
seeding surviving a cross-filesystem move. The superseded record is kept, with its
reasoning intact, because the correction is the interesting part.

Start at [`.agent/index.md`](.agent/index.md).

## Legal

Use homeflix only with content you have the right to access, and follow the laws that
apply where you live.

## License

[MIT](LICENSE) © Ben Duarte
