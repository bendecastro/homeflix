# Active Tasks — Live Cursor

Updated: 2026-08-16

> First thing the next agent reads after `index.md`. Keep it true.

## In flight

- **Queued program:** [Deep Homeflix operations](../../docs/changes/deep-homeflix-operations/prd.md),
  parent **#3**, ready-for-agent slices **#4–#13**. It deepens the canonical CLI with an
  executable stack contract, reliable first-use reconciliation, truthful verification
  modes, fail-closed backup recovery, and the absorbed acquisition phase. Issue map:
  [tasks.md](../../docs/changes/deep-homeflix-operations/tasks.md).
- **In this worktree:** issue **#8** SSH artifact-repository parity — `BACKUP_DEST`
  `user@host:/abs/path` uses the same list/get/put/prune contract as the local
  adapter via `ssh`/`scp` (`-oBatchMode=yes`, argv lists, finite timeout, redacted
  dest). Invalid dest forms are refused before any command. Compatibility shells
  stay thin exec adapters; cron `15 3 * * * /bin/bash $REPO/scripts/backup-config.sh`
  is unchanged. Fixture-accepted only; not live-host proof. No live SSH.
- **Also in this worktree:** issue **#7** fail-closed local backup and scratch restore —
  `scripts/homeflix --json backup {create,list,retrieve,prune,restore}` snapshots
  `CONFIG_ROOT` with the SQLite Online Backup API, refuses same-filesystem
  destinations, and restores only into empty scratch space after archive
  member validation. Fixture-accepted only; not live-host proof.
- **Also in this worktree:** issue **#6** reliable Jellyfin discovery — core
  initialize/reconcile creates one path-targeted Emby/Jellyfin connection
  (`/Library/Media/Updated`) and one unconditional `Library/Refresh` webhook in
  both Radarr and Sonarr. The targeted Test is `/Notifications/Admin` only; the
  path-targeted update is not a full-library scan. Inspection is GET-only and
  redacts the dedicated key. Fixture-accepted only; not live-host proof. Do not
  start acquisition services as part of this slice.
- **Completed slice:** [Agent-first core setup](../project/agent-first-core-setup-plan.md),
  covering local Debian/Ubuntu discovery through API-initialized and verified core on existing
  mounted storage. All nine tasks passed fixture tests and independent spec plus
  quality/security/safety reviews.
- **Verification boundary:** fixture-accepted only; disposable real-host Debian/Ubuntu
  acceptance remains required before claiming general production verification.
- **Follow-ups:** guarded [encrypted storage](../project/agent-first-storage-plan.md) remains
  separate. The former acquisition plan is superseded and absorbed by issues #9–#13.
  Backup SSH transport is the artifact-repository adapter only; host setup SSH
  remains agent-provided orchestration rather than a CLI transport.
- **Approved defaults:** Debian/Ubuntu local or SSH target; secure terminal secret handoff;
  resumable core-first deployment; API-driven application setup; agent-led composable tools.
- **Blockers:** none for fixture-accepted core; real-host acceptance needs a disposable target.

## Next up (priority order)

1. Drain deep-operations issues #4–#13 in dependency order; #4, #6, #7, #8, #9, and #10
   are in this tree.
2. Verify core on a disposable Debian/Ubuntu target before claiming general host support.
3. Execute the encrypted-storage slice with loop-device tests before any real-disk test.
4. Resolve the independent Traefik dashboard, update policy, and remote-access decisions.

## Decisions recorded so far
ADR-0001 (wiki) · 0002 (host) · ~~0003 (storage)~~ superseded · 0004 (Jellyfin) ·
0005 (*arr+VPN) · 0006 (Traefik; remote access open) · 0007 (remote access — Tailscale
primary; Proposed, gated on device inventory) · **0008 (single `/data` root, hardlinks —
Accepted)**.

## Notes
- Source-of-record for original artifacts: the prior private design package (see
  `references/source-research.md`). Stack designed; **deployment state on the box
  unconfirmed** — verify before trusting.
