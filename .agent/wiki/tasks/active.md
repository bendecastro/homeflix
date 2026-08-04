# Active Tasks — Live Cursor

Updated: 2026-08-04

> First thing the next agent reads after `index.md`. Keep it true.

## In flight

- **Phase:** Roadmap → **Phase 0→1 transition**. Nothing executing on the host yet.
- **Just done (2026-08-04):** storage design reversed. **ADR-0008 supersedes ADR-0003** —
  downloads move off the SSD onto the HDD alongside the library (one filesystem), so
  imports are hardlinks and seeding survives. Wiki updated throughout.
- **Published 2026-08-04:** homeflix is now a standalone **public repo**, with this wiki
  included as the build record. Added README, MIT LICENSE, `docs/` for replicators, and
  `scripts/preflight.sh` (which proves hardlinking works rather than assuming it).
  Operator-private material moved to a private note outside the repo. **Not yet pushed to
  a remote.**
- **Also done (2026-08-04):** first real deliverables written at the homeflix root —
  `docker-compose.yml`, `.env.example`, `.gitignore`. Fully parameterised, on the ADR-0008
  `/data` root, validates via `docker compose config`. **Never run against real services.**
  Fixed three latent bugs inherited from the prior compose (Traefik couldn't route to the VPN'd
  services; gluetun healthcheck wrong port/route/auth; Bazarr needs write access to media).
  Overseerr dropped per ADR-0004.
- **Blockers:** needs Ben's input on (a) family members + devices + off-LAN use → ADR-0007,
  and (b) the open public-repo questions below.

## Next up (priority order)

1. **Deploy-readiness on the host** — the compose is untested. Create the dirs
   (`references/commands.md`), fill `.env`, `docker compose up -d`, then verify in order:
   gluetun healthy + VPN IP + kill switch → Traefik routes resolve → **hardlinks actually
   work** (`ls -li` inode match) → one full request-to-playback loop.
2. ~~**Open public-repo decisions**~~ — all resolved 2026-08-04 (see `log.md`). Still
   worth writing up as **ADR-0009 (public repo, parameterisation, and the
   two-audience docs split)**, since the reasoning currently lives only in the log.
3. **Two open decisions still embedded in the compose as ⚠️ comments:**
   Traefik `--api.insecure=true` (unauthenticated dashboard), and `:latest` + Watchtower
   auto-update on a family box. Both left at prior behaviour deliberately — tags are now
   pinnable via `.env` if the Watchtower call goes the other way.
4. **Confirm family device inventory** → accepts [ADR-0007](../decisions/adr-0007-remote-access.md).
5. **Verify on the host:** Docker present, QuickSync transcode (then uncomment the
   `/dev/dri` passthrough in the compose), library drive fstab auto-mount by UUID.
6. Plan the **off-box backup** fix (Phase 5).

## Decisions recorded so far
ADR-0001 (wiki) · 0002 (host) · ~~0003 (storage)~~ superseded · 0004 (Jellyfin) ·
0005 (*arr+VPN) · 0006 (Traefik; remote access open) · 0007 (remote access — Tailscale
primary; Proposed, gated on device inventory) · **0008 (single `/data` root, hardlinks —
Accepted)**.

## Notes
- Source-of-record for original artifacts: the prior private design package (see
  `references/source-research.md`). Stack designed; **deployment state on the box
  unconfirmed** — verify before trusting.
