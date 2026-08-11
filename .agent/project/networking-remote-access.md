# homeflix — Networking, VPN & Remote Access

Updated: 2026-06-14
Decisions: [ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md) (VPN),
[ADR-0006](../decisions/adr-0006-traefik-local-remote-access-open.md) (proxy + the open
remote-access gap). Source: prior private design package (see `references/source-research.md`).

## Reverse proxy — Traefik v3 (decided)

- Image `traefik:v3.0`, network `traefik-network`, Docker provider with
  `exposedbydefault=false`. Per-service routers via `Host(`<svc>.local`)` labels.
- Entrypoints `:80` / `:443`; dashboard on `:8080`.
- ⚠️ Runs `--api.insecure=true` → dashboard has **no auth**. OK on a trusted LAN;
  **must be hardened before any remote exposure**.

## VPN — Gluetun + ProtonVPN (decided)

- Image `qmcgaw/gluetun:latest`, `cap_add: NET_ADMIN`, `/dev/net/tun`.
- ProtonVPN, OpenVPN, `SERVER_COUNTRIES=Netherlands`.
- `FIREWALL=on` (kill switch), `FIREWALL_OUTBOUND_SUBNETS` composed from two **required**
  variables — `LAN_SUBNET` (an RFC1918/CGNAT network discovered from the lowest-metric
  default route and its preferred source or gateway) and `PROXY_SUBNET` (selected away
  from existing host routes, then pinned onto the Compose network; an existing
  Homeflix-owned network is preserved on reruns). Kept separate so neither must be widened to cover the
  other; a whole private block would cover the provider's VPN gateway and break NAT-PMP.
  See `references/gotchas.md`. Also `FIREWALL_INPUT_PORTS=6881,6969,6789,9696`,
  DNS 1.1.1.1 / 1.0.0.1.
- Owns the netns for qBittorrent + NZBGet + Prowlarr and publishes their ports.
- Gluetun's built-in healthcheck probes tunnel health; the three VPN services
  `depends_on: gluetun: condition: service_healthy`. The inherited control-server probe
  was removed because its port, route and unauthenticated-access assumptions were stale.
- Creds (`PROTONVPN_USERNAME/PASSWORD`) come from the host `.env` — never here.

## Local DNS

`*.local` names must resolve on the LAN. **Mechanism not yet documented** — router DNS
entries / Pi-hole / AdGuard / `/etc/hosts`. Decide and record in `references/paths.md`.

## Remote access for off-LAN family — drafted in ADR-0007 (gated on devices)

Today: **LAN-only, no TLS**. The plan is [ADR-0007](../decisions/adr-0007-remote-access.md):
- **Primary: Tailscale** (node-sharing, free) — install on the host, `tailscale serve`
  exposes Jellyfin over HTTPS on the tailnet; admin tools tailnet-only; **zero public
  surface**. Covers phones/tablets/laptops/Apple TV/Android-Fire TV.
- **Locked-down TVs only (Roku/Samsung/LG)** — Tailscale has no client there. Preferred
  fix: add a cheap Google/Fire TV stick (stay all-Tailscale). Fallback:
  **Cloudflare Tunnel** for those + Jellyseerr behind Cloudflare Access (ToS caveat on
  heavy video).
- Reverse-proxy + port-forward + public TLS — **rejected** (too much exposure).

**Decision gate:** confirm the family device inventory (`project/overview.md`). All
app-capable devices → Tailscale only, done. Any no-client TV → pick stick-swap vs
Cloudflare, then Accept ADR-0007 (and ADR-0006). Harden the Traefik dashboard before any
Cloudflare-exposed path.

## Service URL / port map

Full table in `references/paths.md`.

## Links
- [Acquisition](acquisition-stack.md) · [Deployment](deployment.md) · [Paths](../references/paths.md)
- [Secrets](../conventions/secrets.md) · [Overview](overview.md)
