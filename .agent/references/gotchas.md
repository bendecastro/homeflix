# References — Gotchas

Updated: 2026-08-04

Traps specific to the homeflix design, plus the generic classics. Add real ones as they
bite.

## homeflix-specific (from the design review)

- **Never split `/data` into two bind mounts for an *arr app.** Give Radarr/Sonarr/Lidarr
  the single root `${DATA_ROOT}:/data`. If you instead mount `torrents/` and `media/`
  separately, Docker makes them distinct mountpoints and **hardlinks fail even though the
  host filesystem is the same** — the *arr apps silently fall back to copying. This is the
  #1 way to lose hardlinking. See [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md).
- **Don't put the data or appdata roots under `/home`.** TRaSH warns against it directly:
  home directories carry restrictive default permissions and end up a permissions mess for
  containers running as PUID 1000. Use `/opt/<name>/...` or `/docker/appdata/...`. The
  prior design placed these under the user's home directory — don't copy that.
- **Hardlinked files look like they use double the space.** `du` and file managers count
  a file under both `torrents/` and `media/`, but the blocks exist once. Space frees only
  when both names are deleted. Use `du --count-links=no` / check the link count (`ls -l`
  column 2) before panicking about disk usage.
- **A cross-filesystem Move breaks seeding.** If downloads and library ever end up on
  different filesystems, *arr Import Mode = Move relocates the file while the torrent
  still points at the old path — qBittorrent errors with missing files and seeding stops.
  The *arr apps do **not** re-point the download client. This killed the original
  ADR-0003 design; don't reintroduce it.
- **⚠️ Historical (resolved):** the two prior compose variants disagreed on the
  `/downloads` mount, which would have broken imports. ADR-0008's single `/data` root
  removes the whole class of bug — there is no separate downloads mount to drift.
- **Backups on the same HDD as the library = not a backup.** Prior `backup.sh` writes to
  `${BACKUP_ROOT}`, the same drive as `library/`. One drive failure loses both.
  Needs an off-box target.
- **`:latest` + Watchtower auto-update at 02:00** on a family-critical box → a bad
  upstream image can break things unattended. Consider pinned tags + notify-only.
- **Traefik `--api.insecure=true`** → dashboard with no auth. Fine on trusted LAN,
  unacceptable once anything is remotely reachable.
- **Kill switch = no downloads when VPN is down.** Gluetun `FIREWALL=on` fails safe
  (good) but means qBittorrent/NZBGet/Prowlarr have no network until the VPN recovers.
- **`*.local` needs LAN DNS.** Without router/Pi-hole/`/etc/hosts` entries the friendly
  hostnames won't resolve on family devices.
- **Talk to `gluetun:<port>`, not localhost.** The VPN'd services share Gluetun's netns;
  the *arr apps must reference `gluetun:6969/6789/9696`.

## Generic classics (watch for)

- **Permissions hell** — all services run PUID/PGID 1000; library + downloads must be
  writable by uid 1000 or imports fail.
- **Transcoding without GPU access** — pass the iGPU device + drivers; prefer direct play.

## Hit in practice
> _Add dated, specific gotchas here as they happen, with the fix that worked._

- **[2026-08-10] A catch-all `LAN_SUBNET` silently disables VPN port forwarding.**
  `FIREWALL_OUTBOUND_SUBNETS` was shipped as `192.168.0.0/16,10.0.0.0/8`, but VPN
  providers place their gateway inside private address space — ProtonVPN's WireGuard
  gateway is `10.2.0.1`, inside `10.0.0.0/8`. Gluetun therefore added a route sending the
  provider's own gateway out `eth0`, and NAT-PMP requests to `10.2.0.1:5351` timed out.
  Everything else looked correct: container healthy, tunnel up, expected public IP, no
  leak. The only symptom was a missing `/tmp/gluetun/forwarded_port`, and Gluetun's
  explanatory error only appears after roughly nine silent retries (~2 minutes).
  `LAN_SUBNET` is now required with no compose fallback, and
  `tests/test_compose.py::VpnFirewallTests` fails if any shipped allowed subnet covers a
  known provider gateway.
