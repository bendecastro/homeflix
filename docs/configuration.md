# Configuration reference

Every setting lives in `.env`. Nothing is hardcoded in `docker-compose.yml`.

## Local deployment artifacts

`.env` is the canonical local configuration and secret file. It is ignored, written atomically
by `scripts/homeflix configure`, and must remain mode 0600. Back it up only to a destination
that provides equivalent secret protection; restoring it with broader permissions causes core
API operations to refuse it. Never commit it or paste its values into JSON, logs, or chat.

`.homeflix/setup.json` is ignored, typed, non-secret checkpoint evidence only. It may record
approved checkpoint names and bounded non-secret host facts, but never environment values,
credentials, API responses, command output, paths, addresses, or tokens. Live inspection is
authoritative when checkpoint evidence and deployed state differ. Backing it up is optional; a
missing file is reconstructed through reconciliation.

`docker-compose.override.yml` is an ignored, deterministic generated adaptation for this host.
It contains no credentials. Reconfiguration writes the same bytes for the same capability
selection, including QuickSync and temporary direct setup ports.

The generated Jellyfin administrator credential remains only in `.env`. Retrieve it explicitly
through an unredirected controlling terminal with `scripts/homeflix secrets reveal jellyfin`.
The reveal command refuses JSON and pipes; do not transcribe the values into setup state or chat.
There is no VPN-secret setup command in the current core slice.

## Storage

| Variable | Default | Notes |
|---|---|---|
| `DATA_ROOT` | — | Downloads **and** media. **Must be one filesystem.** Not under `/home`. |
| `CONFIG_ROOT` | — | Service configs and databases. The thing to back up. |
| `CACHE_ROOT` | — | Jellyfin transcode scratch. Ephemeral. |

Expected structure under `DATA_ROOT`:

```
torrents/{movies,tv,music}
usenet/incomplete
usenet/complete/{movies,tv,music}
media/{movies,tv,music}
```

## Host

| Variable | Default | Notes |
|---|---|---|
| `PUID` / `PGID` | `1000` | Must own `DATA_ROOT` and `CONFIG_ROOT`. Check with `id -u` / `id -g`. |
| `TZ` | `UTC` | IANA name, e.g. `Europe/Lisbon`. Affects logs and schedules. |
| `LOG_LEVEL` | `info` | `debug` when diagnosing. |

## Networking

| Variable | Default | Notes |
|---|---|---|
| `DOMAIN` | `local` | Services answer at `<service>.${DOMAIN}`. Needs LAN DNS. |
| `JELLYFIN_PUBLISHED_URL` | `http://jellyfin.local` | What Jellyfin advertises to clients. |
| `LAN_SUBNET` | — (required) | Your LAN's CIDR, allowed to bypass the VPN tunnel. `homeflix setup` discovers it from the default route. Must **not** cover your provider's VPN gateway (ProtonVPN uses `10.2.0.1`), or NAT-PMP port forwarding fails while the tunnel still looks healthy. |
| `PROXY_SUBNET` | — (required) | CIDR pinned to the Compose network, so Traefik can reach the services behind Gluetun. Pinned rather than left to Docker, which would otherwise reallocate it on recreate and invalidate the allowlist. |

## VPN

qBittorrent, NZBGet and Prowlarr share Gluetun's network namespace. They have no route
that isn't the tunnel, and `FIREWALL=on` fails closed — if the VPN drops they lose
connectivity rather than leaking.

| Variable | Default | Notes |
|---|---|---|
| `VPN_SERVICE_PROVIDER` | `protonvpn` | ~40 supported — see the [Gluetun wiki](https://github.com/qdm12/gluetun-wiki). |
| `VPN_TYPE` | `openvpn` | `openvpn` or `wireguard`. |
| `VPN_USER` / `VPN_PASSWORD` | — | Provider's *OpenVPN* credentials, not your account login. |
| `VPN_SERVER_COUNTRIES` | `Netherlands` | Exit country. |
| `VPN_DNS` | `1.1.1.1` | DNS inside the tunnel. |
| `VPN_HEALTH_TARGET` | `cloudflare.com:443` | What Gluetun's built-in healthcheck probes. |

If the healthcheck fails, Gluetun restarts and `deunhealth` restarts the services behind
it.

## Ports

Published by the **gluetun** container, not by the services themselves.

| Variable | Default | Service |
|---|---|---|
| `QBITTORRENT_PORT` | `6969` | qBittorrent WebUI |
| `TORRENT_PORT` | `6881` | Torrent traffic (TCP+UDP) |
| `NZBGET_PORT` | `6789` | NZBGet |
| `PROWLARR_PORT` | `9696` | Prowlarr |

The *arr apps must reach these as **`gluetun:<port>`**, never `localhost`.

## Image tags

Every image has a `*_TAG` variable, defaulting to `latest`.

Watchtower auto-updates daily at 02:00 by default. That's convenient but means an
upstream regression can break playback unattended. If you'd rather control updates, pin
real versions:

```bash
RADARR_TAG=5.14.0
JELLYFIN_TAG=10.9.11
```

…and either remove the `watchtower` service or reconfigure it to notify only.

`WATCHTOWER_SCHEDULE` is 6-field cron with **seconds first** — `"0 0 2 * * *"` is 02:00
daily. Keep it quoted.

## Hardware transcoding

Passing through a nonexistent device stops the container starting, so the base Compose file has
no device mapping. Agent-assisted configuration adds `/dev/dri:/dev/dri` to the ignored override
only when structured discovery confirms usable Intel QuickSync. Manual operators may add that
same mapping to their own ignored override after verifying the device and permissions.

## Adding a service

Attach it to `traefik-network` and label it:

```yaml
  myservice:
    image: example/myservice:latest
    environment: *common-env
    volumes:
      - ${CONFIG_ROOT}/myservice:/config
    networks:
      - traefik-network
    restart: unless-stopped
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.myservice.rule=Host(`myservice.${DOMAIN}`)"
      - "traefik.http.services.myservice.loadbalancer.server.port=1234"
```

To put it **behind the VPN** instead, use `network_mode: container:gluetun`, drop the
`networks:` key, and declare its Traefik router on the `gluetun` service — a container
sharing another's namespace has no IP of its own, so Traefik can't discover it directly.

If it needs both downloads and media, give it the single root `${DATA_ROOT}:/data`.
Never two separate mounts.

## Security notes

Two known-open items, marked in `docker-compose.yml`:

- **Traefik dashboard is unauthenticated** (`--api.insecure=true`). Acceptable on a
  trusted LAN; harden before exposing anything remotely.
- **No remote access is configured.** The stack is LAN-only by design. See
  [ADR-0007](../.agent/decisions/adr-0007-remote-access.md) for the intended
  approach (Tailscale, with a Cloudflare Tunnel fallback for TVs that can't run it).

Never commit `.env`. If a credential leaks, rotate it — rewriting git history doesn't
un-publish anything already pushed.
