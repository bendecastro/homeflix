# References — Gotchas

Updated: 2026-08-11

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
- **Bazarr starts empty.** The container is `Up` after `compose up`, but without Radarr/
  Sonarr links, a language profile, and providers it never searches. Run
  `./scripts/configure-bazarr.sh` or follow [`docs/bazarr.md`](../../docs/bazarr.md).
- **Bazarr must not use `localhost` for Radarr/Sonarr.** From inside the container that is
  Bazarr itself. Use the compose service names (`radarr`, `sonarr`) on `traefik-network`.
- **No path mappings under stock Homeflix mounts.** Radarr/Sonarr use `/data/media/...`;
  Bazarr mounts `${DATA_ROOT}/media` at `/data/media`. Adding identity mappings or wrong
  pairs causes “file not found” on search. Only map when the roots truly differ.
- **Bazarr’s media mount is writable on purpose.** It writes `.srt` next to video files.
  Only Jellyfin should mount media read-only.
- **Forced vs full English.** “Forced” = foreign-language lines only; full English is the
  complete track. The recommended profile asks for both so players can offer
  `English - Forced` without forcing always-on captions.

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
  `LAN_SUBNET` is now required with no compose fallback and discovered from the host's
  default route, the Compose network is pinned via a separate `PROXY_SUBNET` chosen to
  avoid every non-default IPv4 route already present on the host, so neither value has to
  be widened to cover the other, and
  `tests/test_compose.py::VpnFirewallTests` fails if any shipped allowed subnet covers a
  known provider gateway.
- **[2026-08-10] Don't infer a LAN subnet from just any interface address.** A host can
  carry a Tailscale `/32`, several Docker bridges, and the real LAN on one machine. Use the
  lowest-metric IPv4 **default route** and its preferred source address (or match its
  gateway when no preferred source is reported). Only RFC1918 and CGNAT ranges may enter
  the VPN bypass; public, `/32`, loopback, link-local and multicast addresses fail closed.
  Proxy subnet selection must separately avoid all existing IPv4 routes across routing
  tables, including Docker bridges, VLANs and secondary LANs, while recognizing and
  preserving Homeflix's own existing proxy network on reruns.
- **[2026-08-11] The *arr "Update Library" connection cannot discover a title Jellyfin has
  never seen.** Before requesting a refresh, Radarr/Sonarr ask Jellyfin where the movie or
  series already lives. For a brand-new title that lookup returns an empty set, so the
  follow-up update targets nothing and Jellyfin answers `204`. No error is logged anywhere,
  and the connection **Test** still passes because it only proves reachability. The same is
  true of `POST /Library/Series/Updated` with an unknown TVDB id. Jellyfin's real-time
  `LibraryMonitor` compounds it: it starts no filesystem watcher for a library folder that
  contains no items, so the very first item added to an empty library has no safety net
  either. The fix is a second **Webhook** connection posting to `/Library/Refresh`, which
  scans unconditionally; keep the targeted connection for titles that already exist.
- **[2026-08-11] Passing `/dev/dri` into Jellyfin does not enable hardware transcoding.**
  The device mapping only makes the GPU available. Jellyfin still defaults to
  `HardwareAccelerationType: none` and transcodes on the CPU, silently. Hardware
  acceleration must also be selected in **Dashboard → Playback → Transcoding**, with HEVC
  and HEVC 10-bit decoding enabled. Verify from the transcode log (`h264_qsv`/`h264_vaapi`
  versus `libx264`) rather than from the setting.
- **[2026-08-11] Jellyfin's media mount is read-only, so libraries must not save metadata
  beside the media.** Library defaults that write artwork, NFO or trickplay images into the
  library folder produce a burst of `IOException saving to /data/media/...` on every scan;
  artwork silently falls back to the config directory and the rest just fails. Core setup
  now sets `SaveLocalMetadata=false`, `MetadataSavers=[]` and `SaveTrickplayWithMedia=false`
  on each managed library.
- **[2026-08-11] Sonarr applies `addOptions.monitor` asynchronously, so season monitoring
  written immediately after an add is silently reverted.** The refresh task that applies the
  add options finishes *after* the `POST` returns, so a corrective `PUT` of
  `seasons[].monitored` is overwritten a moment later, leaving the series unmonitored. The
  read taken straight after the `PUT` returns exactly what was written, so the write looks
  successful; nothing logs an error, and the regression is only visible later. A single read
  after a write is not evidence here. `LibraryClient` re-asserts monitoring and requires the
  desired state to hold across **two reads separated by a delay** before issuing any search,
  failing with `monitoring_unstable` rather than leaving a half-configured series.
- **[2026-08-11] `series/lookup` can rank an unrelated exact-title match above the intended
  show.** A same-named series can outrank the one being sought, including when the intended
  series carries a disambiguating suffix in its own title. Resolving a name to an id by
  string equality picks the wrong series. Titles must be pinned to a TVDB/TMDB id and the
  lookup response checked to carry the id that was requested.
- **[2026-08-11] A Sonarr season-pack download produces one queue record per episode.**
  Identical release names repeated across queue rows are episode records sharing a single
  `downloadId`, not duplicate grabs. Group by `downloadId` before diagnosing duplication.
- **[2026-08-11] Radarr and Sonarr are not published on the host.** Only the proxy, Jellyfin
  and Jellyseerr are bound, so host-loopback `curl` against an *arr port returns nothing —
  which reads like a hung service rather than a closed port. Reach them with
  `docker exec <service> curl http://localhost:<port>/...`. The LinuxServer images ship
  BusyBox `grep`, which has no `-P`, so shell extraction of `<ApiKey>` needs `sed`. Send the
  key as an `X-Api-Key` header; `?apikey=` writes a credential into logs and shell history.
