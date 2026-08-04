# homeflix — Build Roadmap

Status: In progress (design done; deploying)
Updated: 2026-06-14

The spine of the build. Because a full design already exists (the prior private design package),
Phase 0 is largely complete — the focus is reconcile → deploy → verify → close gaps.
`tasks/active.md` always points at the current step.

## Phase 0 — Decide & inventory ✅ mostly done
- [x] Host + OS + runtime → [ADR-0002](../decisions/adr-0002-host-minipc-debian-docker.md)
- [x] Storage strategy → [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md)
      (supersedes ADR-0003)
- [x] Media server → [ADR-0004](../decisions/adr-0004-jellyfin-media-server.md)
- [x] Acquisition + VPN → [ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md)
- [x] Reverse proxy → [ADR-0006](../decisions/adr-0006-traefik-local-remote-access-open.md) (proxy only)
- [ ] Fill `overview.md` with real family members + devices.
- [~] **Remote access → [ADR-0007](../decisions/adr-0007-remote-access.md)** drafted
      (Tailscale primary); Accept once device inventory confirms the TV path.

## Phase 1 — Reconcile & host foundation
- [x] Rewrite `docker-compose.yml` onto the ADR-0008 single `/data` root, with all paths
      parameterised (`${CONFIG_ROOT}`, `${CACHE_ROOT}`, `${DATA_ROOT}`) + `.env.example`.
      **Done 2026-08-04** — validates; not yet run. See `project/deployment.md`.
- [x] Decide repo placement + how code reaches the host — public git repo, cloned on the box.
- [ ] Confirm Debian + Docker on the host; SSH admin.
- [ ] Create `${CONFIG_ROOT}` and `${CACHE_ROOT}`; mount the library drive at `${DATA_ROOT}`
      via fstab UUID + `nofail`; create `{torrents,usenet,media}` + backups; set perms.
- [ ] Verify host transcode capability (QuickSync) before relying on it.
- **Exit:** reboot is clean; HDD auto-mounts; runtime up.

## Phase 2 — Media server up
- [ ] Deploy Traefik + Gluetun + Jellyfin; create admin + one family account.
- [ ] Add libraries (`/data/media/{movies,tv,music}`); play a test file on a real family
      device on LAN. Confirm direct play / transcode.
- **Exit:** a family member plays something on the LAN.

## Phase 3 — Acquisition loop
- [ ] Bring up qBittorrent/NZBGet/Prowlarr (VPN) + Radarr/Sonarr/Lidarr/Bazarr +
      Jellyseerr. Verify VPN IP + kill switch.
- [ ] Enable hardlink imports, set root folders under `/data/media`, categories, download
      client `gluetun:<port>`.
- [ ] Apply naming scheme (`conventions/media-naming.md`).
- [ ] **Verify hardlinking actually works** (`ls -li` inode match — see
      `references/commands.md`); a copy instead of a hardlink means the mounts are wrong.
- [ ] One full request → acquire → imported/renamed → plays in Jellyfin, torrent still seeding.
- [ ] Drop Overseerr or Jellyseerr.
- **Exit:** a non-admin family request becomes a watchable, correctly-named item.

## Phase 4 — Networking & remote access
- [ ] Implement [ADR-0007](../decisions/adr-0007-remote-access.md): Tailscale on the
      the host, `tailscale serve` for Jellyfin, share node to family (+ Cloudflare Tunnel
      only if a locked-down TV needs it).
- [ ] Document LAN DNS for `*.local`.
- [ ] Harden Traefik dashboard (remove `api.insecure` / add auth); real TLS where exposed.
- **Exit:** an off-LAN family member signs in and plays.

## Phase 5 — Resilience & handoff
- [ ] Decide `:latest`+Watchtower vs pinned+notify.
- [ ] **Off-box** config backups + a tested restore.
- [ ] Confirm auto-start on boot; Glances/deunhealth monitoring; family "how to use" doc.
- **Exit:** simulate reboot + restore; the household self-serves without the admin.

## Phase 6 — Polish & v2 wishlist
From `tasks/parking-lot.md`: more libraries, 4K, better metadata, etc.

## Active phase
See `tasks/active.md`.
