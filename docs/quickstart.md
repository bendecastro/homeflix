# Quickstart

From nothing to a running homeflix. Assumes a Linux host you can SSH into.

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker            # or log out and back in
docker compose version   # must print v2.x — the old docker-compose v1 won't work
```

## 2. Decide where things live

Three locations, set as variables so nothing is hardcoded:

| Variable | Holds | Size |
|---|---|---|
| `CONFIG_ROOT` | Service configs and databases | 2–20GB |
| `CACHE_ROOT` | Jellyfin transcode scratch | 10–50GB |
| `DATA_ROOT` | Downloads **and** media library | as big as your library |

**The one rule that matters: `DATA_ROOT` must be a single filesystem.** Everything
inside it — `torrents/`, `usenet/`, `media/` — has to live on the same mount, because
that's what allows hardlink imports. See [why](#why-one-filesystem) below.

**Don't put these under `/home`.** Home directories carry restrictive default
permissions that cause hard-to-diagnose failures in containers running as a fixed
UID/GID. Use `/opt`, `/srv`, or a dedicated mount.

**One drive or two, both fine.** Two drives: put `CONFIG_ROOT`/`CACHE_ROOT` on a fast
SSD and `DATA_ROOT` on a big disk. One drive: point all three at paths on it. The
architecture doesn't care, as long as `DATA_ROOT` is internally one filesystem.

If you're mounting a dedicated library drive, mount it by UUID with `nofail` so a
missing drive can't hang boot:

```bash
sudo blkid /dev/sdX1        # get the UUID
# /etc/fstab:
# UUID=xxxx-xxxx  /mnt/library  ext4  defaults,nofail  0  2
sudo mount -a
```

## 3. Create the layout

```bash
export DATA_ROOT=/mnt/library/data      # adjust to yours

sudo mkdir -p "$DATA_ROOT"/{torrents/{movies,tv,music},usenet/{incomplete,complete/{movies,tv,music}},media/{movies,tv,music}}
sudo mkdir -p /opt/homeflix/{appdata,cache}

sudo chown -R "$USER:$USER" "$DATA_ROOT" /opt/homeflix
sudo chmod -R a=,a+rX,u+w,g+w "$DATA_ROOT"
```

This is the [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/) layout.
It's worth matching exactly — most support answers you'll find online assume it.

## 4. Configure

```bash
git clone https://github.com/bendecastro/homeflix.git
cd homeflix
cp .env.example .env
```

Edit `.env`. Minimum to change:

- `DATA_ROOT`, `CONFIG_ROOT`, `CACHE_ROOT` — from step 2
- `VPN_WIREGUARD_PRIVATE_KEY` (default) or `VPN_USER` / `VPN_PASSWORD` — see below
- `TZ` — your timezone
- `PUID` / `PGID` — run `id -u` and `id -g`
- `LAN_SUBNET` — your narrow RFC1918/CGNAT LAN CIDR; never a whole private block
- `PROXY_SUBNET` — a private `/24` that overlaps neither an existing host/Docker route nor
  the VPN gateway (for ProtonVPN, it must not contain `10.2.0.1`). `PROXY_NETWORK_SUBNET`
  is an alias if an older checkout already uses that name.

**VPN credentials.** Gluetun supports ~40 providers; set `VPN_SERVICE_PROVIDER`
accordingly and check the [Gluetun wiki](https://github.com/qdm12/gluetun-wiki) for what
each expects. The default `VPN_TYPE=wireguard` needs `VPN_WIREGUARD_PRIVATE_KEY` —
for ProtonVPN, enable NAT-PMP when generating the config. With `VPN_TYPE=openvpn`,
use the provider's OpenVPN credentials, not your account login.

The VPN is not optional: qBittorrent, NZBGet and Prowlarr have no network path except
through it, and the kill switch fails closed.

The CLI acquisition path is optional and selected: `scripts/homeflix --json setup
acquisition --clients {torrent,usenet,both}` (default `torrent`) starts only the selected
clients plus Prowlarr after current fail-closed evidence. That path is **fixture-accepted
only**. It is not live request-to-library proof, and it is not the same as
`docker compose up -d`, which starts every service defined in Compose. News-server
credentials, when Usenet is selected, enter only via `scripts/homeflix secrets usenet`.

## 5. Preflight

```bash
./scripts/homeflix preflight
# compatibility adapter (same command): ./scripts/preflight.sh
```

This is `scripts/homeflix preflight`, not a separate checker. The wrapper execs that
CLI and does not source `.env`. The command validates configuration and storage
without starting containers. Most importantly it **creates an actual hardlink**
between `torrents/` and `media/` and confirms both names share one inode.

Other CLI capabilities, used when needed rather than as one required sequence:

- Acquisition: `scripts/homeflix --json setup acquisition --clients {torrent,usenet,both}`
- Verification: `scripts/homeflix --json verify {contract,core,vpn,acquisition}`
  and the explicit `verify vpn --disrupt` fail-closed exception
- Backup: `scripts/homeflix --json backup create` (also `list` / `retrieve` / `prune` /
  scratch `restore`)

Fix anything it reports before continuing. A failure here is much cheaper than
discovering the same problem after you've built a library.

## 6. Start

```bash
docker compose up -d
docker compose ps          # everything should be Up; gluetun should be healthy
docker compose logs -f gluetun
```

## 7. LAN DNS

Services answer on `*.homeflix` names, but something has to resolve them. Pick one:

- **Router** — add local DNS entries pointing each name at the host's IP
- **Pi-hole / AdGuard** — add local DNS records
- **Per device** — add lines to `/etc/hosts` (fine for testing, tedious for a household)

Or skip it: Jellyfin is also published directly on `http://<host-ip>:8096`.

## 8. Wire it up

1. **Prowlarr** (`prowlarr.homeflix`) — add your indexers, then add Radarr/Sonarr/Lidarr
   under *Settings → Apps* so indexers sync automatically.
2. **qBittorrent** (`qbittorrent.homeflix`) — default login `admin`; print only the current
   temporary password with
   `docker compose logs qbittorrent 2>&1 | sed -n 's/.*session: //p' | tail -1`.
   Change it, then set the save path to `/data/torrents`.
3. **Radarr / Sonarr / Lidarr** —
   - *Media Management* → root folder `/data/media/movies` (resp. `tv`, `music`)
   - *Media Management* → enable **Rename**, and confirm **Use Hardlinks instead of
     Copy** is on (it's the default)
   - *Download Clients* → add qBittorrent at host **`gluetun`**, port `6969`. Not
     `localhost` — the VPN'd services share Gluetun's network namespace.
4. **Jellyfin** (`jellyfin.homeflix`) — create the admin account, add libraries pointing at
   `/data/media/movies`, `/data/media/tv`, `/data/media/music`.
5. **Bazarr** (`bazarr.homeflix`) — compose starts the container, but it still needs one-time
   wiring to Radarr/Sonarr, a language profile, and subtitle providers. After Radarr (and
   ideally Jellyfin) are up:

   ```bash
   ./scripts/configure-bazarr.sh
   ```

   That installs an **English forced + English full** profile as the default, enables free
   providers, and connects the *arr apps over the Docker network. Optional
   OpenSubtitles.com account and full manual steps: [bazarr.md](bazarr.md).
6. **Radarr / Sonarr → Jellyfin** — create a dedicated Jellyfin API key, then add and test
   an **Emby / Jellyfin** connection in each *arr app so imports trigger library refreshes.
   Use internal host `jellyfin`, port `8096`; see the
   [first-use settings](first-use.md#4-make-imports-appear-promptly).
7. **Jellyseerr** (`jellyseerr.homeflix`) — connect it to Jellyfin, then to Radarr/Sonarr.
   This is the only URL most of your household needs.

Next, follow the [first-use guide](first-use.md) to create household accounts, connect a
Jellyfin client, and trace a released test request through the stack.

## 9. Verify hardlinks end to end

After the first successful import:

```bash
ls -li "$DATA_ROOT"/media/movies/*/*.mkv
```

Column 1 is the inode, column 3 is the link count. A properly imported file shows a
link count of **2** — one name under `torrents/`, one under `media/`. If it's 1, the
file was copied and something is misconfigured:

```bash
find "$DATA_ROOT/media" -links 1 -type f     # anything listed was NOT hardlinked
```

## Why one filesystem

A file is data blocks plus a name pointing at them. A hardlink is a second name for the
same blocks — created instantly, costing no extra space. That's how the *arr apps import:
`media/Dune (2021)/Dune (2021).mkv` and `torrents/Dune.2021.2160p.mkv` become two names
for one 30GB file.

This means the torrent keeps seeding after import, Jellyfin sees a cleanly-named library,
and deleting either name is safe — the data survives until both are gone.

Hardlinks only work within a single filesystem. Put downloads and media on different
drives and imports become full copies: slow, double the disk, and the torrent breaks
because the file it was seeding has moved.

Full reasoning:
[ADR-0008](../.agent/decisions/adr-0008-single-filesystem-data-root-hardlinks.md).

## Troubleshooting

**Imports fail with permission errors** — `DATA_ROOT` must be owned by `PUID:PGID`.
Re-run the `chown` from step 3.

**Files copy instead of hardlinking** — the *arr app has `torrents/` and `media/` as
separate mounts. It needs the single root `${DATA_ROOT}:/data`.

**Downloads never start** — check `docker compose logs gluetun`. If the tunnel is down,
the kill switch is doing its job and those services have no network by design.

**`*.homeflix` doesn't resolve** — step 7. Traefik routes; it doesn't do DNS. `.homeflix` is not reserved — serve it locally (router / Pi-hole / `/etc/hosts`) so queries do not leak.

**No subtitles appear** — Bazarr is running but unconfigured until step 8.5 (or
[docs/bazarr.md](bazarr.md)). Confirm Radarr is reachable as host `radarr`, not
`localhost`, and that a language profile is assigned to the title.

More: [`.agent/references/gotchas.md`](../.agent/references/gotchas.md).
