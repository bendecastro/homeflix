# Active Tasks — Live Cursor

Updated: 2026-08-17

> First thing the next agent reads after `index.md`. Keep it true.

## In flight

- **Program fixture-accepted:** [Deep Homeflix operations](../../docs/changes/deep-homeflix-operations/prd.md),
  parent **#3**, slices **#4–#13**. The public CLI, living specs, compatibility adapters,
  and wiki cursor now describe one fixture-accepted program. The program is fixture-accepted
  only. Disposable-host and private-production live acceptance remain separate.
- **Completed slice:** [Agent-first core setup](../project/agent-first-core-setup-plan.md),
  covering local Debian/Ubuntu discovery through API-initialized and verified core on existing
  mounted storage. All nine tasks passed fixture tests and independent spec plus
  quality/security/safety reviews.
- **Verification boundary:** fixture-accepted only; disposable real-host Debian/Ubuntu
  acceptance remains required before claiming general production verification.
- **Follow-ups:** guarded [encrypted storage](../project/agent-first-storage-plan.md) remains
  separate. The former acquisition plan is superseded and is not an executable queue.
  Backup SSH transport is the artifact-repository adapter only; host setup SSH
  remains agent-provided orchestration rather than a CLI transport.
- **Approved defaults:** Debian/Ubuntu local or SSH target; secure terminal secret handoff;
  resumable core-first deployment; API-driven application setup; agent-led composable tools.
- **Blockers:** none for fixture-accepted core or the operations program; real-host
  acceptance needs a disposable target.

## Next up (priority order)

1. Verify core on a disposable Debian/Ubuntu target before claiming general host support.
   Disposable-host and private-production live acceptance remain separate.
2. Execute the encrypted-storage slice with loop-device tests before any real-disk test.
3. Resolve the independent Traefik dashboard, update policy, and remote-access decisions.

## Decisions recorded so far
ADR-0001 (wiki) · 0002 (host) · ~~0003 (storage)~~ superseded · 0004 (Jellyfin) ·
0005 (*arr+VPN) · 0006 (Traefik; remote access open) · 0007 (remote access — Tailscale
primary; Proposed, gated on device inventory) · **0008 (single `/data` root, hardlinks —
Accepted)** · **0011 (WireGuard + port forwarding — Accepted, amends 0005)**.

## Notes
- Source-of-record for original artifacts: the prior private design package (see
  `references/source-research.md`). Stack designed; **deployment state on the box
  unconfirmed** — verify before trusting.
