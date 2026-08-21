# Parking Lot — Deferred / Future Ideas

Updated: 2026-08-19

The v2 wishlist. Things explicitly *not* in v1 scope. Pull from here in Roadmap
Phase 6.

- Music library (Lidarr + media-server music).
- Photos / home-video library.
- Books / audiobooks (Readarr / Audiobookshelf).
- Per-user quality profiles / parental controls beyond basic libraries.
- Watch analytics / dashboards.
- 4K tier with dedicated quality profiles.
- Offline downloads for family travel.
- Redundant/HA setup or second node.
- **`verify acquisition` post-restore listen-port settle.** After a successful fail-closed restore, Gluetun can already have a forwarded-port file while qBittorrent still has the previous listen port. That is a listen mismatch, not “configured but unavailable”. A short wait or retry before failing `port_agrees` would avoid a false failure in that window. Not a closeout blocker; needs a fixture that restores then verifies immediately.

> Add items here whenever a "nice idea" surfaces mid-build so it doesn't derail v1.
