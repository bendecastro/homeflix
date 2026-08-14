# Active Tasks — Live Cursor

Updated: 2026-08-14

> First thing the next agent reads after `index.md`. Keep it true.

## In flight

- **Queued program:** [Deep Homeflix operations](../../docs/changes/deep-homeflix-operations/prd.md),
  parent **#3**, ready-for-agent slices **#4–#13**. It deepens the canonical CLI with an
  executable stack contract, reliable first-use reconciliation, truthful verification
  modes, fail-closed backup recovery, and the absorbed acquisition phase. Issue map:
  [tasks.md](../../docs/changes/deep-homeflix-operations/tasks.md).
- **In this worktree:** issue **#5** truthful core runtime verification — `verify_core`
  reuses the stack contract, adds docker/mount/hardlink domains, fail-closes on
  mandatory unknown/skip, and reports `failure` / `not-applicable`. #4 remains in
  the parent tree.
- **Completed slice:** [Agent-first core setup](../project/agent-first-core-setup-plan.md),
  covering local Debian/Ubuntu discovery through API-initialized and verified core on existing
  mounted storage. All nine tasks passed fixture tests and independent spec plus
  quality/security/safety reviews.
- **Verification boundary:** fixture-accepted only; disposable real-host Debian/Ubuntu
  acceptance remains required before claiming general production verification.
- **Follow-ups:** guarded [encrypted storage](../project/agent-first-storage-plan.md) remains
  separate. The former acquisition plan is superseded and absorbed by issues #9–#13.
  SSH remains agent-provided orchestration rather than a CLI transport.
- **Approved defaults:** Debian/Ubuntu local or SSH target; secure terminal secret handoff;
  resumable core-first deployment; API-driven application setup; agent-led composable tools.
- **Blockers:** none for fixture-accepted core; real-host acceptance needs a disposable target.

## Next up (priority order)

1. Drain deep-operations issues #4–#13 in dependency order; #4, #6, and #7 can start.
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
