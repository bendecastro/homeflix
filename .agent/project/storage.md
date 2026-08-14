# homeflix — Storage

Updated: 2026-08-14
Decision: [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md)
(supersedes ADR-0003).

**One filesystem for downloads + library on the HDD** (so hardlinks work), with the SSD
holding only what genuinely benefits from fast random I/O: config and transcode cache.

## Path variables

Paths are parameterised so the stack is replicable on any hardware. Example
values are shown for reference; they belong in `.env`, not in committed files.

| Variable | Tier | Example | Holds |
|---|---|---|---|
| `${CONFIG_ROOT}` | SSD | `${CONFIG_ROOT}` | per-service config + SQLite DBs |
| `${CACHE_ROOT}` | SSD | `${CACHE_ROOT}` | Jellyfin transcode scratch, thumbnails |
| `${DATA_ROOT}` | HDD | `${DATA_ROOT}` | downloads **and** media — one filesystem |
| `${BACKUP_DEST}` | off-box | rsync/scp dest in `.env` | dated `CONFIG_ROOT` archives — not the library disk |

## SSD — config + cache (~50–100GB is plenty)

```
${CONFIG_ROOT}/          (~2–20GB) the thing to BACK UP
└── {qbittorrent,radarr,sonarr,lidarr,bazarr,jellyseerr,
      jellyfin,prowlarr,nzbget,glances,traefik,...}/
${CACHE_ROOT}/           (~10–50GB) transcode scratch, ephemeral
```

> ⚠️ **Do not put `${CONFIG_ROOT}` or `${DATA_ROOT}` under `/home`.** TRaSH explicitly
> warns against it: home directories carry restrictive default permissions, which turns
> into a permissions mess for containers running as PUID 1000. Use `/opt/<name>/...`
> (or `/docker/appdata/...`, TRaSH's own convention). The prior design placed these under
> the user's home directory — don't copy that.

The SSD earns its place here: the *arr and Jellyfin SQLite databases are random-I/O
heavy, and transcode scratch is write-heavy and short-lived — keeping both off the HDD
stops them contending with playback reads. It does **not** hold downloads (ADR-0008).

## HDD — `${DATA_ROOT}`, a single filesystem

Exactly the TRaSH reference layout ([Docker](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/)),
all lowercase (Linux is case-sensitive):

```
${DATA_ROOT}/
├── torrents/{movies,tv,music}          active + seeding (files stay here permanently)
├── usenet/
│   ├── incomplete/
│   └── complete/{movies,tv,music}
└── media/{movies,tv,music}             the library Jellyfin serves
```

Create with:
```bash
mkdir -p "${DATA_ROOT}"/{torrents/{movies,tv,music},usenet/{incomplete,complete/{movies,tv,music}},media/{movies,tv,music}}
```

Naming scheme detail: `conventions/media-naming.md`.

## Why one filesystem — hardlinks

Downloads and library sit on the **same** filesystem, so the *arr apps import by
**hardlink**: instant, free, and no second copy of the data. The torrent keeps its file
in `torrents/` while `media/` holds another name for the same blocks, so **seeding
continues indefinitely**. Deleting either name is safe; space is reclaimed only when
both are gone.

This is the standard TRaSH layout — see
[ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md) for the full
rationale and for why the prior SSD-downloads design was retired.

## Mounting rules (these are load-bearing)

- The *arr apps get the **single root**: `${DATA_ROOT}:/data`.
- **Never** give an *arr app `torrents/` and `media/` as two separate bind mounts.
  Docker makes those distinct mountpoints and hardlinks across them fail *even though
  the host filesystem is the same*. This is the #1 way to silently lose hardlinking.
- Download clients get their own subtree: `${DATA_ROOT}/torrents:/data/torrents`,
  `${DATA_ROOT}/usenet:/data/usenet`.
- Jellyfin gets `${DATA_ROOT}/media:/data/media`, **read-only**.
- In Radarr/Sonarr/Lidarr: **"Use Hardlinks instead of Copy" = enabled** (the default).
  Import Mode = Move is retired.

## Permissions

All services run as **PUID=1000 / PGID=1000**, `TZ` from `.env`. Everything under
`${DATA_ROOT}` must be read/write for that user. Permission mismatches are the classic
*arr import failure — see `references/gotchas.md`. TRaSH's recommended treatment:

```bash
sudo chown -R $USER:$USER "${DATA_ROOT}"
sudo chmod -R a=,a+rX,u+w,g+w "${DATA_ROOT}"   # 775 dirs / 664 files
```

## Open risks

- **Same-disk config backups are not backups.** Use `BACKUP_DEST` on another
  filesystem via `scripts/backup-config.sh`. See `project/deployment.md`.
- **No redundancy** on either tier (single SSD, single HDD).
- **Shared spindle:** torrent random-writes and playback reads now share the HDD.
  Acceptable for a household; if it bites, add a second disk for `torrents/` — do *not*
  revert to cross-device moves.

## Links
- [Hardware](hardware.md) · [Acquisition](acquisition-stack.md) · [Deployment](deployment.md)
- [Media naming](../conventions/media-naming.md) · [Paths](../references/paths.md)
- [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md) ·
  [ADR-0003 (superseded)](../decisions/adr-0003-two-tier-storage-move-not-hardlink.md)
