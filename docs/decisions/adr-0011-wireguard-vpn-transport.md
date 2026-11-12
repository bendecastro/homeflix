# ADR 0011: WireGuard as the VPN transport, with port forwarding

Date: 2026-08-17

## Status
Accepted

Amends [ADR-0005](adr-0005-arr-stack-gluetun-protonvpn.md). Only that ADR's
*transport* clause changes; the service split, the ProtonVPN provider choice,
`FIREWALL=on`, and the `network_mode: container:gluetun` topology all stand.

## Context
ADR-0005 specified ProtonVPN over OpenVPN before a live tunnel existed. Two
facts change the balance for any operator who actually wants inbound peers:

- Gluetun supports ProtonVPN over WireGuard with a single
  `WIREGUARD_PRIVATE_KEY`. There is no per-server config and no username/password
  pair to rotate.
- Declaring `VPN_TYPE` and a `+pmp` username hint does not enable forwarding.
  Gluetun's `VPN_PORT_FORWARDING` defaults to off. Without a forwarded port,
  qBittorrent still downloads but inbound peers and seeding degrade — which
  undercuts [ADR-0008](adr-0008-single-filesystem-data-root-hardlinks.md).

`LAN_SUBNET` must stay a real household CIDR. A catch-all such as `10.0.0.0/8`
covers ProtonVPN's WireGuard gateway (`10.2.0.1`) and silently breaks NAT-PMP
while the tunnel still looks healthy. The proxy CIDR is a separate pinned
variable (`PROXY_SUBNET`, with `PROXY_NETWORK_SUBNET` as an alias).

## Decision
- **`VPN_TYPE=wireguard`** is the default. Credentials are
  `VPN_WIREGUARD_PRIVATE_KEY` from a provider-generated config.
- Compose passes both credential sets (`WIREGUARD_PRIVATE_KEY` and
  `OPENVPN_USER`/`OPENVPN_PASSWORD`), each defaulting to empty. Gluetun reads
  only the pair matching `VPN_TYPE`. OpenVPN stays a one-variable switch.
- **Port forwarding is on.** For ProtonVPN, NAT-PMP must be selected when the
  WireGuard config is generated; it cannot be added to an existing key.
- `VPN_PORT_FORWARDING_UP_COMMAND` runs `scripts/gluetun-qbt-port.sh` so the
  current forwarded port is pushed into qBittorrent after every reconnect.
  That requires qBittorrent's localhost authentication bypass, which is
  acceptable only because `localhost` in the shared namespace is Gluetun and
  the VPN'd clients.
- Preflight and `secrets vpn` validate the credential set `VPN_TYPE` actually
  selects.

## Consequences
- One secret instead of two for the default path. The private key is
  provider-wide; it belongs only in the host `.env`.
- ProtonVPN forwards a random port that changes on reconnect, so the listen
  port cannot be fixed in qBittorrent config.
- `PORT_FORWARD_ONLY=on` narrows server selection to P2P-capable servers.
- Operators who need OpenVPN set `VPN_TYPE=openvpn` and fill `VPN_USER` /
  `VPN_PASSWORD` (ProtonVPN: append `+pmp` for forwarding).

## Links
- [Gluetun ProtonVPN setup](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/protonvpn.md)
- [Gluetun VPN server port forwarding](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/vpn-port-forwarding.md)
- `project/acquisition-stack.md`, `conventions/secrets.md`
