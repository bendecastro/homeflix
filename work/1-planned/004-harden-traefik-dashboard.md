# Harden the Traefik dashboard

Why: `docker-compose.yml` still runs Traefik with `--api.insecure=true`, which exposes an unauthenticated dashboard on :8080; it must be hardened before any exposure.
Done when: the dashboard is removed or behind authentication, real TLS is used wherever the stack is exposed, and the decision is recorded (amending [ADR-0006](../../docs/decisions/adr-0006-traefik-local-remote-access-open.md) if needed).

## Steps

- [ ] Decide between removing the dashboard and adding authentication.
- [ ] Change the compose file and record the decision.

## Outcome

<Filled when the item is finished or dropped.>
