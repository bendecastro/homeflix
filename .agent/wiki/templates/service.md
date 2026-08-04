# Service — <name>

Updated: YYYY-MM-DD
Status: Planned   <!-- Planned | Running | Broken | Retired -->
Role: <one line — what it does in the stack>

## Deployment
- Image: `<repo/image:TAG>` (pin the tag — no `latest`)
- Compose file: `<path at homeflix root>`
- Restart policy: `unless-stopped`

## Networking
- Internal URL/port: `<host:port>`
- Subdomain (via proxy): `<sub.domain>` (or "internal only")
- Exposed remotely? `<no / via VPN / via tunnel>`

## Storage / volumes
- Config volume: `<appdata path>` (**back up**)
- Data mounts: `<shared /data parent or subpaths>` (must match `project/storage.md`)
- PUID/PGID / umask: `<values>`

## Secrets
- Which secrets it needs and *where they live* (never the values) — see
  `conventions/secrets.md`.

## Healthcheck / verify
- How to confirm it's working (URL responds, a test action succeeds).

## Depends on
- `<other services it needs up first>`

## Notes / gotchas
- Anything service-specific; cross-link `references/gotchas.md`.
