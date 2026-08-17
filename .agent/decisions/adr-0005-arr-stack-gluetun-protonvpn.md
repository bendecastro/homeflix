# ADR 0005: Full *arr acquisition stack; downloaders behind Gluetun + ProtonVPN

Date: 2026-06-14

## Status
Accepted

Amended by [ADR-0011](adr-0011-wireguard-vpn-transport.md) for the VPN transport
and port-forwarding defaults. The service split and Gluetun topology stand.

> Source: prior private design package (see `references/source-research.md`).

## Context
homeflix needs an automated acquire→organize→serve pipeline and a privacy posture for
the parts that touch torrent/usenet networks. Prior analysis split services by actual
risk rather than blanket-VPN'ing everything.

## Decision
- **Stack:** qBittorrent (torrents) + NZBGet (usenet) for downloads; Prowlarr
  (indexers); Radarr (movies), Sonarr (TV), Lidarr (music); Bazarr (subtitles);
  Jellyseerr (requests). All `linuxserver/*` images except Jellyseerr.
- **VPN split (deliberate):** only the three risky services route through the VPN —
  **qBittorrent, NZBGet, Prowlarr** via `network_mode: container:gluetun`. Everything
  else (Radarr/Sonarr/Lidarr/Bazarr/Jellyseerr/Jellyfin/monitoring) runs **direct** on
  the `traefik-network` — they only talk to internal services + legal metadata APIs.
- **VPN:** Gluetun (`qmcgaw/gluetun`) → ProtonVPN. Transport and port forwarding
  are in [ADR-0011](adr-0011-wireguard-vpn-transport.md) (`VPN_TYPE=wireguard` by
  default; OpenVPN remains a one-variable switch). `FIREWALL=on` (kill switch),
  `FIREWALL_OUTBOUND_SUBNETS` for LAN and the pinned proxy network,
  `FIREWALL_INPUT_PORTS` for the published service ports. Gluetun owns the network
  namespace for the three VPN services and publishes their ports.
- The *arr apps reach the download/indexer services at `gluetun:<port>` (6969
  qBittorrent, 6789 NZBGet, 9696 Prowlarr).

## Consequences
- Kill switch means: if the VPN drops, the three download/search services lose network
  (fail safe) — they don't leak the real IP. But it also means **VPN down = no
  downloads** until it recovers (a single dependency for the acquire path).
- Management/streaming layer stays fast (no VPN overhead) and simple (no proxy
  containers) — the explicit tradeoff from `VPN-ANALYSIS.md`.
- ProtonVPN credentials are **secrets** — only in the host `.env`, never in this wiki
  (`conventions/secrets.md`).
- Acquire only content legally entitled to; the VPN is privacy hygiene, not a license.

## Links
- `project/acquisition-stack.md`, `project/networking-remote-access.md`,
  `references/paths.md`, `conventions/secrets.md`
