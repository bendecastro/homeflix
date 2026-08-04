# ADR 0003: Two-tier storage (SSD scratch + HDD library), move-on-import not hardlinks

Date: 2026-06-14

## Status
Superseded by [ADR-0008](adr-0008-single-filesystem-data-root-hardlinks.md) (2026-08-04)

> Source: prior private design package (see `references/source-research.md`).
>
> ⚠️ **Do not implement this ADR.** Its central argument is circular — it treats
> "downloads are on the SSD" as a constraint when that was itself an unexamined
> choice. Its claim that qBittorrent keeps seeding after a cross-filesystem Move is
> also **factually wrong** (the torrent errors with missing files). Retained for the
> decision trail only. See [ADR-0008](adr-0008-single-filesystem-data-root-hardlinks.md).

## Context
Downloads are high-I/O and temporary; the library is large and cold. The SSD is fast
but small (1TB); the HDD is large but slow (4TB). The standard *arr recommendation
(TRaSH) is one shared filesystem so imports are instant **hardlinks** — but that
requires downloads and library on the *same* filesystem. Here they are deliberately
on **different** devices (SSD vs HDD), so hardlinks are impossible.

## Decision
Split storage across two tiers and use the *arr apps' **Completed Download Handling
with Import Mode = Move** instead of hardlinks:

- **SSD (`${CONFIG_ROOT}`, ~700GB target):** `downloads/` (torrents+usenet, seeding),
  `cache/` (Jellyfin transcode/thumbnails), `config/` (all service configs/DBs).
- **HDD (`${DATA_ROOT}`, 4TB):** `library/{movies,tv,music,archive}` (permanent) and
  `backups/{daily,weekly,monthly}`.

Flow: download + seed on SSD → on seed-ratio, Radarr/Sonarr/Lidarr **move** the file
to the HDD library (renamed) → qBittorrent continues seeding from the HDD location →
Jellyfin scans and serves from `${DATA_ROOT}/media`.

## Consequences
- **No instant hardlink import** — there's a real move (cross-device copy+delete) and
  a brief period where the file exists on both tiers. SSD must hold downloads +
  seeding + cache + config concurrently (the ~700GB budget).
- This intentionally **does not follow the TRaSH single-root/hardlink guidance** —
  that gotcha note in `references/gotchas.md` is acknowledged and overridden here.
- For Move to work, **every *arr app and the download client must see the same
  `/downloads` path AND the library path** with consistent permissions (PUID/PGID
  1000). The two prior compose variants disagree on the exact download mount — must be
  reconciled (see `references/gotchas.md` → "compose drift").
- **Backups currently target the same single HDD as the library** — that is NOT a real
  backup (one drive failure loses both). Flagged as an open risk; needs an off-box
  destination. See `project/deployment.md`.

## Links
- `project/storage.md`, `conventions/media-naming.md`, `references/paths.md`
