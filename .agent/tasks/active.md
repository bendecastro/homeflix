# Active Tasks — Live Cursor

Updated: 2026-08-04

> First thing the next agent reads after `index.md`. Keep it true.

## In flight

- **Effort:** [Agent-first setup](../project/agent-first-setup.md) — design approved;
  implementation has not started.
- **In progress:** [Agent-first core setup](../project/agent-first-core-setup-plan.md),
  covering supported-host discovery through API-initialized Jellyfin/Jellyseerr/Radarr/Sonarr
  on existing mounted storage. Tasks 1–7 passed spec and quality/security review; executing
  task 8: live verification and interruption-safe resume orchestration.
- **Follow-ups:** guarded [encrypted storage](../project/agent-first-storage-plan.md) and
  VPN-gated [acquisition setup](../project/agent-first-acquisition-plan.md).
- **Approved defaults:** Debian/Ubuntu local or SSH target; secure terminal secret handoff;
  resumable core-first deployment; API-driven application setup; agent-led composable tools.
- **Blockers:** none for the core implementation slice.

## Next up (priority order)

1. Execute the core plan task-by-task, beginning with the tested CLI/state foundation.
2. Run its fixture acceptance suite, then verify on a disposable Debian/Ubuntu target before
   claiming general host support.
3. Execute the encrypted-storage slice with loop-device tests before any real-disk test.
4. Execute acquisition only with authorized VPN/provider credentials and real fail-closed
   evidence.
5. Resolve the independent Traefik dashboard, update policy, remote-access, and off-box
   backup decisions already tracked elsewhere.

## Decisions recorded so far
ADR-0001 (wiki) · 0002 (host) · ~~0003 (storage)~~ superseded · 0004 (Jellyfin) ·
0005 (*arr+VPN) · 0006 (Traefik; remote access open) · 0007 (remote access — Tailscale
primary; Proposed, gated on device inventory) · **0008 (single `/data` root, hardlinks —
Accepted)**.

## Notes
- Source-of-record for original artifacts: the prior private design package (see
  `references/source-research.md`). Stack designed; **deployment state on the box
  unconfirmed** — verify before trusting.
