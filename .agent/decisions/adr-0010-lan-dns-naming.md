# ADR 0010: LAN DNS naming — ship `homeflix`, not `.local`

Date: 2026-08-13

## Status
Accepted

## Context

Services answer at `<service>.${DOMAIN}`. The historical default was `DOMAIN=local`,
and the docs told people to add `*.local` records on the router. RFC 6762 reserves
`.local` for multicast DNS. Apple and Android clients often never query unicast
records for that TLD, so the documented happy path fails on the devices a household
actually uses.

RFC 8375 reserves `home.arpa` for residential unicast names. That is the leak-safe
choice. This project still ships a branded name so the URLs read as the product:
`jellyfin.homeflix`, `radarr.homeflix`.

`.homeflix` is **not** a reserved special-use domain. A resolver that does not claim
it will forward those queries to the public internet.

## Decision

Ship `DOMAIN=homeflix` as the template and CLI default. Document:

- `*.local` only for real mDNS/Avahi
- `home.arpa` as the RFC 8375 leak-safe alternative
- that operators must serve `*.homeflix` locally (router / Pi-hole / AdGuard / hosts)

`JELLYFIN_PUBLISHED_URL` follows the same suffix.

## Consequences

- New installs advertise `http://jellyfin.homeflix`. Existing deployments that still
  use `.local` must recreate Traefik-labeled containers and re-point clients.
- LAN DNS is required. The host-IP `:8096` escape hatch remains.
- No effect on TLS: neither `homeflix` nor `home.arpa` can get a public certificate.

## Links
- [RFC 6762](https://www.rfc-editor.org/info/rfc6762/) ·
  [RFC 8375](https://datatracker.ietf.org/doc/rfc8375/)
- [quickstart §7](../../docs/quickstart.md) ·
  [configuration](../../docs/configuration.md)
