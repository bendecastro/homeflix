# Context Map

Use this page to choose the smallest context set for a task. Don't read the whole
wiki — read the rows that match what you're doing. Paths starting with `docs/`, `work/` or
`scripts/` are relative to the repository root; the rest are relative to `.bc-agent/`.

## Always useful

- `docs/overview.md` — what we're building, for whom, and current status
- `work/2-active/` and `work/1-planned/` — what is in flight and what is next
- `docs/research/2026-11-12-earlier-task-history/roadmap.md` — the earlier phased build plan (record)

## Hardware / host work

- `docs/components/hardware.md`
- `docs/components/deployment.md`
- `references/paths.md`

## Storage / filesystem / library layout work

- `docs/components/storage.md`
- `conventions/media-naming.md`
- `references/paths.md`

## Media server (player layer) work

- `docs/components/media-server.md`
- `conventions/media-naming.md`
- `docs/components/networking-remote-access.md` (for client access)

## Acquisition stack (*arr / indexers / downloads / requests) work

- `docs/components/acquisition-stack.md`
- `conventions/media-naming.md`
- `references/external-links.md` (TRaSH guides)
- `conventions/secrets.md`

## Networking / remote access / reverse proxy work

- `docs/components/networking-remote-access.md`
- `references/paths.md` (ports, subdomains)
- `conventions/secrets.md`

## Deployment / compose / OS / backups work

- `docs/components/deployment.md`
- `references/commands.md`
- `docs/components/hardware.md`
- `docs/specs/backup-recovery.md`
- `scripts/homeflix_setup/backup.py` (`backup create|list|retrieve|prune|restore`, `verify backup`)

## Stack-contract / static verification work

- `docs/specs/stack-contract.md`
- `scripts/homeflix_setup/contract.py`
- `docs/components/deployment.md` (self-heal pair and phase allowlists)
- `references/commands.md`

## Core application reconciliation

- `docs/specs/setup-reconciliation.md`
- `docs/first-use.md` (two-connection operator truth)
- `scripts/homeflix_setup/api/arr.py` and `api/jellyfin.py`
- `scripts/homeflix_setup/core.py` (`configure_core` / `verify_core`)

## Runtime verification work

- `docs/specs/verification.md`
- `scripts/homeflix_setup/core.py` (`verify_core`)
- `scripts/homeflix_setup/contract.py` (reused, not duplicated)
- `references/commands.md`

## Acquisition VPN gate

- `docs/specs/verification.md` and `docs/specs/setup-reconciliation.md`
- `scripts/homeflix_setup/secrets.py` (`secrets vpn`)
- `scripts/homeflix_setup/vpn.py` (`vpn verify`, `verify vpn --disrupt`)
- `scripts/homeflix_setup/compose.py` (`GLUETUN_SERVICES`, not `compose_up`)
- `references/commands.md`

## Workflow or agent-behavior work

- `AGENTS.md`
- [references/agent-skills.md](references/agent-skills.md) — which skills to invoke
- [conventions/file-layout.md](conventions/file-layout.md) ·
  [conventions/architecture-runway.md](conventions/architecture-runway.md)
- [references/source-research.md](references/source-research.md) — provenance of the prior design
- `conventions/git-and-commit-policy.md`
- `log.md`

## Decisions

For architectural or hard-to-reverse changes, check `docs/decisions/` before adding a new
ADR. The big six homeflix forks: host OS/runtime, filesystem/RAID, media server,
acquisition stack shape, remote access, media naming scheme.
