# homeflix Agent Log

Append-only chronological journal of what happened and when. Newest at the bottom.

Entry format (keep the prefix consistent so the log stays greppable —
`grep "^## \[" log.md | tail -5` gives the last 5 entries):

`## [YYYY-MM-DD] <kind> | <short title>`

where `<kind>` is one of: `scaffold | ingest | decision | build | fix | blocked | lint | query`.

---

## [2026-06-14] scaffold | Initialized homeflix agent wiki

Created the project agent wiki under `.agent/` using Karpathy's LLM-wiki pattern
adapted for producing a practical outcome (modeled on an earlier project's wiki).
Laid down AGENTS.md (maintainer schema), index/home/map, project pages (overview,
roadmap, hardware, storage, media-server, acquisition-stack, networking-remote-access,
deployment), references, conventions, decisions (ADR-0001), tasks, and templates.

## [2026-06-14] ingest | Folded in prior research from the prior private design package

Read the existing NAS design package (working 14-service `docker-compose.yml`,
HOMEFLIX-STRUCTURE, HARDLINK-SETUP, VPN-ANALYSIS, guides). Converted the scaffold into
real design: rewrote hardware/storage/media-server/acquisition/networking/deployment +
overview + roadmap with verified facts. Recorded ADR-0002 (mini-PC/Debian/Docker),
0003 (two-tier storage, move-not-hardlink), 0004 (Jellyfin+Jellyseerr), 0005 (*arr +
Gluetun/ProtonVPN, 3-on-VPN split), 0006 (Traefik; remote access OPEN). Filled
references/paths, gotchas, commands, source-research; conventions/media-naming, and
and noted that host-private items stay out of the wiki (values never copied).
Flagged gaps: no remote access (LAN-only), backups on same HDD as library, Traefik
dashboard unauthenticated, :latest+Watchtower on a family box, compose drift between
two variants, host transcode capability to verify. Next: ADR-0007 remote
access + reconcile the compose. Deployment state on the box is unconfirmed.

## [2026-06-14] decision | ADR-0007 drafted — remote access (Tailscale primary)

Drafted ADR-0007 resolving the ADR-0006 open fork. Recommendation: Tailscale via
node-sharing (free, zero public surface), `tailscale serve` for Jellyfin HTTPS on the
tailnet, admin tools tailnet-only. Deciding factor = TV clients: Tailscale covers
phones/tablets/laptops/Apple TV/Android-Fire TV but NOT Roku/Samsung/LG native apps —
for those, prefer adding a Google/Fire TV stick, fallback Cloudflare Tunnel (+ Access)
with a video-ToS caveat. Port-forward+public TLS rejected. Status Proposed, gated on
confirming the family device inventory in overview.md; flips to Accepted (with ADR-0006)
once devices are known. Wired into index, networking page, roadmap, active cursor.

## [2026-08-04] decision | ADR-0008 supersedes ADR-0003 — one filesystem, hardlink imports

Re-examined the storage split while scoping homeflix as a public repo, and reversed it.
ADR-0003's argument was **circular**: it asserted downloads and library were "deliberately
on different devices, so hardlinks are impossible" — treating the consequence of an
unexamined choice (why the SSD at all?) as an external constraint. Its sole benefit,
"fast downloads on SSD," fails because throughput is capped by the internet link, not the
~100–150 MB/s USB3 HDD. Worse, ADR-0003 and the prior package's hardlink guide both claimed
qBittorrent keeps seeding after the cross-filesystem Move — **it doesn't**; the torrent
errors with missing files and the *arr apps never re-point the client (confirmed against
TRaSH's hardlinks guide). Also unaccounted: full cross-device copy per import, SSD write
amplification for every downloaded byte, and a config/cache budget (100GB/200GB) inflated
by ~10x.

ADR-0008: keep both drives but reassign roles. SSD = `${CONFIG_ROOT}` + `${CACHE_ROOT}`
(SQLite random I/O, transcode scratch — where it genuinely wins). HDD = one `${DATA_ROOT}`
holding `torrents/`, `usenet/`, `media/` → hardlink imports, instant and free, perma-seed
works. *arr apps mount the **single root** `${DATA_ROOT}:/data` (splitting it into two
bind mounts silently breaks hardlinks — new gotcha). Import Mode = Move retired in favour
of "Use Hardlinks instead of Copy". Side effects: the compose-drift gotcha dissolves (no
separate downloads mount to drift), the SSD requirement drops ~700GB → ~50–100GB, and the
layout is now the standard TRaSH one — better for replicators. Backups-on-same-HDD risk
unchanged.

Updated: ADR-0003 (marked Superseded + warning), storage, paths, gotchas, commands,
deployment, hardware, media-server, acquisition-stack, media-naming, roadmap, overview,
source-research, index, active.

## [2026-08-04] direction | homeflix to become a public, replicable repo

Intent: publish homeflix publicly so anyone can replicate it, with this deployment as just
one instance, and keep `.agent/` visible as a CV artifact demonstrating AI-assisted
engineering. Implications recorded: all host-specific paths become variables; the wiki is
already secret-clean (grepped — no keys, IPs, or credentials) but has dead links into the
the prior private design package (which holds host secrets and stays unpublished). Open questions parked in `tasks/active.md` pending an ADR-0009.

## [2026-08-04] correction | Verified layout against TRaSH — two fixes to ADR-0008 detail

Checked the ADR-0008 layout against the canonical sources (TRaSH File-and-Folder-Structure
+ Docker setup, Servarr wiki). The core decision holds exactly — single `/data` root,
*arr apps mount the whole root, download clients get only their subtree, media server gets
`/data/media` read-only. TRaSH is explicit that the `/movies` + `/tv` + `/downloads` mount
style (which the prior package's compose used) "makes them look like two or three file
systems, even if they aren't" and loses hardlinking. Two corrections to my own writeup:

1. **Never put data or appdata under `/home`** — TRaSH warns restrictive home-dir
   permissions create a mess for containers running as PUID 1000. the prior design used
   `${CONFIG_ROOT}/...` and I'd carried it forward. `${CONFIG_ROOT}`/`${CACHE_ROOT}` moved to
   `${CONFIG_ROOT} ${CACHE_ROOT}`. Added to gotchas.
2. **Exact usenet subfolder shape** — TRaSH uses `usenet/incomplete/` +
   `usenet/complete/{movies,tv,music}`, not a flat `usenet/{movies,tv,music}` + sibling
   `incomplete/`. Fixed in storage + commands.

Also captured operational guidance for later: seed goals are set **on grab** (changing them
doesn't affect in-flight torrents); enable Remove Completed Downloads in the *arr apps so
they clean up once the goal is met; private trackers enforce minimum seed *time* (commonly
72h–5d) independent of ratio, and hit-and-runs get accounts banned. Because hardlinks make
seeding storage-free, default to ratio 1.0 + ~3 day minimum.

## [2026-08-04] build | First deliverables — parameterised docker-compose.yml + .env.example

Rebuilt the prior private design package into `homeflix/docker-compose.yml` on the
ADR-0008 single `/data` root, with every host path parameterised (`${DATA_ROOT}`,
`${CONFIG_ROOT}`, `${CACHE_ROOT}`, `${DOMAIN}`, `${PUID}`/`${PGID}`/`${TZ}`, VPN provider +
credentials, ports, and per-image tags). Added `.env.example` (documented, incl. the
"DATA_ROOT must be one filesystem" warning and the not-under-/home rule) and `.gitignore`.
Validates with `docker compose config`. **Not yet run against real services.**

Mounts now hardlink-safe: Radarr/Sonarr/Lidarr get the single root `${DATA_ROOT}:/data`;
qBittorrent `${DATA_ROOT}/torrents:/data/torrents`; NZBGet `${DATA_ROOT}/usenet:/data/usenet`;
Jellyfin `${DATA_ROOT}/media:/data/media:ro`.

Three latent bugs in the prior package's compose found and fixed:
1. **Traefik could not route to qBittorrent/NZBGet/Prowlarr at all.** They run with
   `network_mode: container:gluetun`, so they hold no IP of their own and Traefik's Docker
   provider can't discover them — the labels on those containers were dead. Moved their
   routers onto the `gluetun` container, which is on `traefik-network`.
2. **Gluetun healthcheck wrong three ways** — probed `:8888/v1/openvpn/status`; the control
   server is `:8000`, the route is `/v1/vpn/status`, and it requires auth by default
   (gluetun-wiki). Dropped it for gluetun's own built-in tunnel healthcheck +
   `HEALTH_TARGET_ADDRESS`.
3. **Bazarr had media read-only in effect** — it writes subtitles alongside the media, so
   its mount must stay writable; only Jellyfin gets `:ro`.

Also: Overseerr dropped (ADR-0004 chose Jellyseerr — closes that open item); `version:` and
empty `volumes: {}` removed (obsolete in Compose v2); images switched to `lscr.io/linuxserver/*`;
duplicate `DNS_ADDRESS` key removed; QuickSync `/dev/dri` passthrough added commented-out
pending host verification. Two open decisions deliberately left at prior behaviour with ⚠️
comments in-file: Traefik `--api.insecure=true`, and `:latest`+Watchtower (tags are now
pinnable via `.env` if that flips).

## [2026-08-04] release | Published as a public repo; sanitized and given a replicator layer

Moved homeflix into a standalone public repo. Twelve design branches were resolved with
the operator before any files were touched; the notable ones:

- **Full ADR trail kept, not flattened.** Superseding is standard ADR practice, and the
  ADR-0003 → ADR-0008 reversal is the most valuable thing in the wiki. Solved the real
  problem — *routing*, not volume — with two doors: `docs/` answers "how do I run this,"
  the wiki answers "why is it built this way." A replicator never needs to open
  `decisions/`.
- **Whole wiki published live**, including `log.md` and `tasks/`. A curated snapshot
  would read as marketing and would silently rot against the working copy.
- **Split by page type, not find-and-replace.** System-describing pages moved to neutral
  second person; ADRs and the log keep first-person attribution, since they are dated
  records of who decided what.
- **Operator-private material stripped** to a private note outside the repo: disk
  encryption status, backup roles, actual mount paths, LAN addressing, household device
  inventory, and the specific host model. The *hardware class* stayed, because it is
  load-bearing for ADR-0002 and for the "prefer direct play" constraint — strip it and
  the ADRs become assertions without reasoning.
- **Replicability set at "preflight validation"** rather than README-only or an
  interactive installer. Rationale: this domain's defining failure is silent — a
  split-filesystem layout still imports successfully, just by copying, wasting double the
  disk and killing seeding, with nothing appearing broken for months. `scripts/preflight.sh`
  therefore **proves** hardlinking works (creates a real hardlink, asserts a shared inode)
  instead of inferring it.
- **README leads with the system, reveals the method second** — leading with "built with
  AI" invites judgment of the claim rather than the work.

Wrote README (architecture diagram, quickstart, design rationale, honest note that
ADR-0003 was reversed), MIT LICENSE, `docs/{quickstart,hardware,configuration}.md`, and
`scripts/preflight.sh`.

Two bugs found by testing the preflight script rather than assuming it worked:
1. **`stat -c %d` does not dereference symlinks** — GNU stat needs `-L`. Without it a
   symlinked `media/` reported the symlink's own device and a cross-filesystem layout
   **passed** the check. Caught only because the hardlink smoke test failed while the
   device check succeeded; the belt-and-braces design paid for itself. Now `stat -Lc`.
2. **`.env.example` executed on source** — `WATCHTOWER_SCHEDULE=0 0 2 * * *` unquoted ran
   as a command. Quoted it and `LAN_SUBNET`.
Both positive and negative cases are now verified.

Verified clean: no secrets, no personal paths, no broken links, compose valid.

## [2026-08-04] maintenance | Flattened the agent wiki structure

Moved the nested wiki contents directly into `.agent/`, removed the empty research
placeholder, and updated repository links and maintainer guidance for the new layout.

## [2026-08-04] scaffold | Added root agent entry points

Added a lean root `AGENTS.md` that routes coding agents into the maintained `.agent/`
wiki, states the load-bearing project constraints, and documents safe validation commands.
Added `CLAUDE.md` as a symlink to the same file so Claude receives identical guidance.

## [2026-08-04] design | Approved agent-first setup and implementation slices

Chose an agent-led, phased setup instead of a documentation-only playbook or one rigid
installer. The target is Debian/Ubuntu locally or over SSH, with composable idempotent CLI
primitives, ignored resumable state, secure terminal secret handoff, optional guarded
LUKS2/ext4 provisioning, core-first deployment without VPN credentials, and API-driven
Jellyfin/Jellyseerr/Radarr/Sonarr initialization. Split delivery into independently testable
core, encrypted-storage, and VPN/acquisition plans.

## [2026-08-04] implementation | Agent setup task 1 — CLI foundation

Added the Python standard-library setup launcher, structured status command, injectable
redacting command runner, and atomic schema-versioned local state. State accepts only typed
non-secret facts and boolean checkpoints; malformed versions, secret/output-shaped fields,
and corrupt JSON fail safely. Nineteen focused tests pass, including cross-directory use,
machine-readable errors, overlapping-secret redaction, and failed-write preservation. Fresh
spec and quality reviews passed after their findings were fixed.

## [2026-08-04] implementation | Agent setup task 2 — host discovery

Added read-only, bounded Debian/Ubuntu discovery for Docker/Compose, identity, timezone,
CPU/memory, graphics, ports, mounts, DNS, and SSH context. Every uncertain probe distinguishes
confirmed absence from missing/error/not-tested state; unsupported distributions refuse
cleanly, Docker/Compose gaps are actionable, nested mounts are preserved, and no host facts
are persisted. Thirty-seven tests pass; fresh spec and quality reviews passed.

## [2026-08-04] implementation | Agent setup task 3 — guarded Docker preparation

Added exact read-only Debian/Ubuntu Docker preparation plans with explicit fingerprint
confirmation before apply. The apply path fully rediscovers/rebuilds the plan, refuses stale
state and conflicting distro packages, stages apt repository files atomically, bounds
privileged child processes, verifies identity/group/service state, and reports partial or
cleanup failures structurally. Fifty-eight fixture tests pass and fresh spec/safety reviews
passed. No real apt, systemd, group, repository, or Docker mutation was run.

## [2026-08-05] implementation | Agent setup task 4 — secure host configuration

Added atomic mode-0600 dotenv updates with real Bash/Compose-compatible encoding, effective
last-assignment semantics, inline-comment preservation, generated-once Jellyfin credentials,
and controlling-terminal-only reveal. Configuration verifies mounted storage, derives host
identity, pins the Compose project, probes actual LAN service resolution, and renders a
byte-stable ignored override. QuickSync is enabled only for accessible Intel render devices.
Seventy-nine tests and fresh spec/security reviews passed; no real deployment files were
created.

## [2026-08-05] implementation | Agent setup task 5 — phase-aware preflight

Replaced the shell implementation with structured core/acquisition preflight while preserving
the repository-relative wrapper. Core warns on missing VPN secrets; acquisition fails. Checks
cover mounted non-symlink storage, ownership, known filesystem risks, exact Compose validity,
and real torrent/media plus acquisition-only Usenet/media hardlinks with fail-closed cleanup.
Every data bind now disables host-path creation. Ninety-eight tests and fresh spec/quality
reviews passed; no real deployment paths or containers were touched.

## [2026-08-05] implementation | Agent setup task 6 — resumable core deployment

Added an immutable five-service core allowlist with `--no-deps`, exact Compose context,
side-effect-free dry-run, live-state no-op reconciliation, container plus HTTP readiness,
and sanitized partial-failure diagnostics. One global deadline bounds readiness. Exact env
bytes, Compose/override files, and data mount identity are snapshot-checked across preflight
immediately before mutation; checkpoints are evidence only and record after full readiness.
One hundred twenty-one tests and fresh spec/quality reviews passed; no real container command
was run.

## [2026-08-05] implementation | Agent setup task 7 — API initialization

Added bounded, redacted clients for Jellyfin, Radarr/Sonarr, and Jellyseerr. Setup follows
Jellyfin's startup/auth/library sequence, discovers *arr profiles by name, creates exact roots,
preserves unowned settings while enabling rename/hardlinks/completed handling, and connects
Jellyseerr over Docker DNS with non-4K defaults. Loopback API transport rejects proxies,
redirects, and URL escapes; app secrets are read through owner-checked no-follow traversal,
including normal read-only 0644 files. Initialized Jellyseerr must verify its internal
Jellyfin connection. One hundred forty-eight fixture/localhost tests and fresh spec/security
reviews passed; no live APIs or appdata were accessed.

## [2026-08-09] implementation | Agent setup task 8 — verification and resume

Added ordered `setup core` orchestration, checkpoint-independent reconciliation, and a
strictly evidence-driven verifier for the exact Homeflix project, five core services, API
state, conditional QuickSync mapping, and acquisition absence. Standalone initialization
attests live deployment readiness before mutation; one global deadline bounds work, and
Jellyfin authentication sessions are always closed. Checkpoint/state output is typed,
bounded, and secret-free. One hundred eighty-one fixture/temp tests and fresh spec/security
reviews passed; no live deployment, API, or appdata operation was run.

## [2026-08-09] implementation | Agent setup task 9 — guidance and acceptance

Published agent-assisted setup as the first README path, added a lean AGENTS intent route and
an outcome-oriented core setup guide, retained the manual fallback, and documented ignored
mode-0600 configuration/state artifacts. Added fail-closed Markdown checks and a clean-state
fixture journey spanning Debian discovery, ext4 preflight, exact core deployment, bounded
partial API failure, resume, verification, and no-op rerun. One hundred eighty-seven tests and
fresh spec/quality reviews passed. The core slice is fixture-accepted only; no disposable real
Debian/Ubuntu host, live Docker deployment, appdata, or service API was accessed.
