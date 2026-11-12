# Ideas

Undecided ideas and observations, one line each, newest at the bottom. An idea that
someone decides to do becomes a numbered work item in `1-planned/`; delete its line here.

- Music library (Lidarr + media-server music).
- Photos / home-video library.
- Books / audiobooks (Readarr / Audiobookshelf).
- Per-user quality profiles / parental controls beyond basic libraries.
- Watch analytics / dashboards.
- 4K tier with dedicated quality profiles.
- Offline downloads for family travel.
- Redundant/HA setup or second node.
- `verify acquisition` post-restore listen-port settle: after a fail-closed restore, Gluetun can already have a forwarded-port file while qBittorrent still has the previous listen port, so `port_agrees` fails falsely; a short wait or retry would avoid it. Needs a fixture that restores then verifies immediately.
- Verify host transcode capability (QuickSync) before relying on it.
