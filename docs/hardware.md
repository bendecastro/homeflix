# Hardware

What you actually need, and what the reference build implies.

## Minimum

| Component | Requirement |
|---|---|
| CPU | Any always-on x86-64. An Intel chip with QuickSync if you want hardware transcoding |
| RAM | 4GB workable, 8GB comfortable for the full ~13-container stack |
| Storage | One disk large enough for your library, plus room for downloads |
| Network | Wired ethernet strongly preferred |
| OS | Any Linux with Docker and the Compose v2 plugin |

ARM (Raspberry Pi 5, ARM mini-PCs) works for the *arr apps and direct-play Jellyfin, but
transcoding is weak and some images have thinner ARM support. Fine for a small library
where clients play files natively.

## Reference build

homeflix was developed against a deliberately modest machine, so the architecture never
assumes headroom:

- Low-power x86-64 mini-PC, Celeron-class with integrated graphics
- 8GB RAM
- 1TB internal SATA SSD (Debian + configs + cache)
- 4TB external HDD over USB 3.0, ext4 (the library)
- Wired gigabit ethernet

The most important consequence: **that CPU cannot transcode much.** The design therefore
prefers direct play — keeping the library in formats clients play natively — rather than
promising several simultaneous transcoded streams. If your hardware is stronger, you have
more freedom than this documentation assumes; nothing here prevents you from using it.

## Storage layout

Two independent variables, so both topologies work:

**One drive** — point `CONFIG_ROOT`, `CACHE_ROOT` and `DATA_ROOT` at paths on it:

```
/opt/homeflix/appdata     ← CONFIG_ROOT
/opt/homeflix/cache       ← CACHE_ROOT
/srv/data                 ← DATA_ROOT   (torrents/ usenet/ media/)
```

**Two drives** — configs and cache on the fast disk, library on the big one:

```
SSD:  /opt/homeflix/appdata     ← CONFIG_ROOT
      /opt/homeflix/cache       ← CACHE_ROOT
HDD:  /mnt/library/data         ← DATA_ROOT   (torrents/ usenet/ media/)
```

The SSD earns its place here: the *arr and Jellyfin SQLite databases are random-I/O
heavy, and transcode scratch is write-heavy and short-lived — keeping both off the
library disk stops them competing with playback reads.

**The one hard requirement is that `DATA_ROOT` is internally a single filesystem.** Do
not split `torrents/` and `media/` across drives; that breaks hardlinking, which is the
foundation of the whole storage design
([ADR-0008](../.agent/decisions/adr-0008-single-filesystem-data-root-hardlinks.md)).

Avoid **exFAT and NTFS** for `DATA_ROOT` — they don't support hardlinks properly. ext4,
XFS and Btrfs are all fine.

## Sizing

- **Library** — the dominant cost. Roughly 4–15GB per movie at 1080p, 20–60GB at 4K, and
  2–5GB per TV episode.
- **Downloads** — they stay in `torrents/` while seeding, but hardlinks mean a seeding
  file and its library copy share one set of blocks. Seeding costs **no extra space**.
- **Configs** — 2–20GB. Jellyfin's metadata and artwork dominate. This is the part to
  back up.
- **Cache** — 10–50GB, transient.

## Hardware transcoding

If your CPU has Intel QuickSync, uncomment the device passthrough in
`docker-compose.yml`:

```yaml
devices:
  - /dev/dri:/dev/dri
```

Verify the device exists first:

```bash
ls -l /dev/dri        # expect renderD128
```

It's commented out by default because passing through a device that doesn't exist stops
the container from starting.

**Passing the device through does not enable hardware transcoding.** It only makes the GPU
*available*; Jellyfin still defaults to `Hardware acceleration: None` and will transcode on
the CPU, silently and slowly. You must also set it under *Dashboard → Playback →
Transcoding*: choose **Intel QuickSync (QSV)**, set the device to `/dev/dri/renderD128`, and
enable decoding for the codecs you actually have — **HEVC** and **HEVC 10-bit** matter most,
because x265 releases are common and browsers cannot decode HEVC, so those titles always
transcode.

Confirm the GPU is genuinely in use rather than trusting the setting. Play an HEVC title on
a client that must transcode, then inspect the newest transcode log:

```bash
docker exec jellyfin sh -c 'ls -t /config/log/FFmpeg.Transcode-*.log | head -1 | xargs grep -m1 -o "h264_qsv\|h264_vaapi\|libx264"'
```

`h264_qsv` or `h264_vaapi` means hardware encoding; `libx264` means it is still on the CPU.
The same log's `speed=` values show the margin — comfortably above `1x` is what you want.

If the render device is present but Jellyfin cannot use it, check that the container user is
in the group owning `/dev/dri/renderD128` and that the driver loads:

```bash
docker exec jellyfin /usr/lib/jellyfin-ffmpeg/vainfo --display drm --device /dev/dri/renderD128
```

## USB-attached drives

Common for the library disk, and workable, but:

- Mount **by UUID with `nofail`** in `/etc/fstab`, so a missing drive doesn't hang boot.
- Prefer USB 3.0+ and a powered enclosure. Bus-powered 3.5" drives cause resets under
  sustained load.
- Because downloads live on this disk too, it stays active most of the time — sustained
  reliability matters more than spin-down tuning.

## Backups

Config is small and irreplaceable; media is large and re-acquirable. Back up
`CONFIG_ROOT` **off the box** with `scripts/backup-config.sh` — a backup on the same
physical disk as the library is not a backup, since one failure loses both. Set
`BACKUP_DEST` in `.env`, install the daily cron, and prove a restore into a scratch
directory with `scripts/restore-config.sh` before you rely on it. See
[configuration](configuration.md#config-backups). The checkout `.env` is a separate
secret and is not inside these archives.
