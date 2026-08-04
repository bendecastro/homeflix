# ADR 0008: Single-filesystem `/data` root on the HDD; hardlink imports

Date: 2026-08-04

## Status
Accepted

> Supersedes [ADR-0003](adr-0003-two-tier-storage-move-not-hardlink.md).

## Context

ADR-0003 split storage so that **downloads live on the SSD** and the **library lives on
the HDD**, and concluded that hardlinks were therefore impossible, mandating *arr
Import Mode = **Move***.

Re-examined 2026-08-04 while preparing homeflix for a public, replicable repo. The
reasoning does not hold up:

**The argument was circular.** ADR-0003's Context states the two tiers are "deliberately
on different devices … so hardlinks are impossible." That treats the *consequence of a
choice* as an *external constraint*. The prior design never asked the actual question —
*why should downloads be on the SSD at all?* — it inherited "we own an SSD and an HDD,
so use both" from the hardware inventory and built everything downstream to accommodate
it.

**The only stated benefit does not survive scrutiny.** `HARDLINK-SETUP.md` justifies the
split with "fast downloads on SSD." Download throughput is bounded by the internet
connection, not the disk: the USB3 4TB HDD sustains ~100–150 MB/s (≈800–1200 Mbps).
There is no plausible link speed on this host that makes the HDD the bottleneck. Torrent
random-writes are harder on spinning disks, but qBittorrent's preallocation and write
cache absorb that at family-server scale.

**The documented flow is factually broken.** Both ADR-0003 and `HARDLINK-SETUP.md` claim
"qBittorrent continues seeding from the HDD location" after the *arr move. It does not.
A cross-filesystem Move relocates the file while the torrent still references the old
SSD path; qBittorrent finds nothing and the torrent errors with missing files. The *arr
apps do not re-point the download client. TRaSH lists perma-seeding as a *benefit of
hardlinks* for exactly this reason:

> "You CAN'T hardlink across separate file systems, partitions, volumes or mounts …
> \[Hardlinks let you\] have a file in multiple locations without using double your
> storage space … You want to perma-seed?"
> — [TRaSH: Hardlinks and Instant Moves](https://trash-guides.info/File-and-Folder-Structure/Hardlinks-and-Instant-Moves/)

**Further costs ADR-0003 understated:**
- Every import is a full cross-device copy (a 60GB remux ≈ 8 minutes, present on both
  tiers meanwhile) rather than an instant, free metadata operation.
- **SSD write amplification:** every downloaded byte is written to the SSD, then read
  and rewritten to the HDD. The entire download volume is charged against the SSD's
  write endurance for no gain.
- The ~700GB SSD budget became a permanent operational tax, with seeding data pinning
  SSD space indefinitely.
- The sizing that justified that budget was wrong: `storage.md` allotted 100GB to
  config and 200GB to cache. Real *arr + Jellyfin config is ~2–20GB; transcode cache is
  transient and small.

## Decision

**Put downloads and the media library on one filesystem — the HDD — and use hardlink
imports.** The SSD is retained, but for the role it actually earns.

```
${CONFIG_ROOT}   (SSD)   per-service config + SQLite DBs   — fast random I/O
${CACHE_ROOT}    (SSD)   Jellyfin transcode scratch        — ephemeral, write-heavy

${DATA_ROOT}     (HDD)   ONE filesystem, one bind mount
  ├── torrents/{movies,tv,music}
  ├── usenet/{movies,tv,music}
  └── media/{movies,tv,music}
```

- The *arr apps mount the **single root** `${DATA_ROOT}:/data`. They must **not** get
  `torrents/` and `media/` as two separate bind mounts — Docker would make those
  distinct mountpoints and hardlinks across them would fail even though the host
  filesystem is the same.
- Download clients get their own subtree (`${DATA_ROOT}/torrents:/data/torrents`,
  `${DATA_ROOT}/usenet:/data/usenet`).
- Jellyfin gets `${DATA_ROOT}/media:/data/media`, read-only.
- *arr setting: **"Use Hardlinks instead of Copy" = enabled** (the default). Import Mode
  = Move is retired.
- Paths are parameterised (`${CONFIG_ROOT}`, `${CACHE_ROOT}`, `${DATA_ROOT}`) rather
  than hardcoded, for the public repo. Example: `${CONFIG_ROOT}`,
  `${CACHE_ROOT}`, `${DATA_ROOT}`.

## Consequences

- **Imports are instant and free.** No cross-device copy, no window where the file
  exists twice, no move-vs-copy failure mode.
- **Perma-seeding works.** The torrent keeps its file; the library holds a hardlink to
  the same blocks. Either "copy" can be deleted independently; space is reclaimed only
  when both are gone.
- **The compose-drift gotcha dissolves.** The `/downloads` mount mismatch between the
  two prior compose variants was an artifact of the split layout. With a single `/data`
  root every service sees identical paths — the class of bug is designed out, not
  patched.
- **SSD requirement collapses** from ~700GB to roughly 50–100GB, and the SSD stops
  being a wear item. This materially widens the hardware range that can replicate
  homeflix (small boot SSD + one big disk is the common case).
- **This restores, rather than overrides, TRaSH guidance.** The `references/gotchas.md`
  note about the single-root/hardlink recommendation is no longer overridden.
- **New minor gotcha:** hardlinked files appear twice in naive `du`/file-manager
  totals while consuming space once. Recorded in `references/gotchas.md`.
- **Accepted trade-off:** torrent random-write I/O and library reads now share one
  spindle. Acceptable for a household; if it ever bites, the fix is a second disk for
  `torrents/` — *not* a return to cross-device moves.
- **Unchanged open risk:** backups still target the same physical HDD as the library, so
  they are still not a real backup. Off-box destination remains required
  (`project/deployment.md`).
- **Migration:** any media already on `${DATA_ROOT}/media` must be relocated to
  `${DATA_ROOT}/media` and the *arr root folders re-pointed. Nothing is deployed yet, so
  this is currently a paper change.

## Links
- Supersedes [ADR-0003](adr-0003-two-tier-storage-move-not-hardlink.md)
- `project/storage.md` · `project/deployment.md` · `references/paths.md` ·
  `references/gotchas.md` · `conventions/media-naming.md`
- [TRaSH: Hardlinks and Instant Moves](https://trash-guides.info/File-and-Folder-Structure/Hardlinks-and-Instant-Moves/)
