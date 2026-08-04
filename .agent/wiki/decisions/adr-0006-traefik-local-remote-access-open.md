# ADR 0006: Traefik reverse proxy with `*.local` hostnames — remote access still OPEN

Date: 2026-06-14

## Status
Proposed

> Source: prior private design package (see `references/source-research.md`).
> `traefik-gluetun-integration.md`. The proxy part is decided; the **remote-access
> part is an unresolved gap** and is the main reason this ADR is Proposed, not Accepted.

## Context
Services are exposed on the LAN via Traefik v3 with `Host(`<svc>.local`)` rules
(jellyfin.local, radarr.local, etc.). This is LAN-only. homeflix is for *family* — who
are very likely off-LAN at least some of the time — yet there is currently **no remote
access design and no TLS** (Traefik runs `--api.insecure=true`, HTTP only).

## Decision (partial)
- **Accepted now:** Traefik v3 as the single reverse proxy on `traefik-network`,
  Docker provider, `exposedbydefault=false`, per-service `Host(`*.local`)` routers.
- **Deferred (the open fork):** how off-LAN family reach homeflix. Candidates:
  - **Tailscale / WireGuard** mesh VPN (nothing public; needs client per device —
    check family TV support).
  - **Cloudflare Tunnel** (public hostname, no open ports; check streaming ToS).
  - Reverse-proxy + port-forward + real TLS (largest public surface; least preferred).
- Until decided, homeflix is **LAN-only**.

## Consequences / must-fix before "family-ready"
- `--api.insecure=true` exposes the Traefik dashboard without auth — fine on a trusted
  LAN, **not** acceptable if anything is reachable remotely. Harden before exposure.
- `*.local` names need LAN DNS resolution (router entries / Pi-hole / `/etc/hosts`) —
  document the mechanism in `references/paths.md`.
- No valid TLS today — any remote path needs real certs.
- This ADR moves to **Accepted** once the remote-access method is chosen (likely its
  own ADR-0007) and the dashboard is secured.

## Links
- `project/networking-remote-access.md`, `references/paths.md`, `project/overview.md`
