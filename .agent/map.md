# Context Map

Use this page to choose the smallest context set for a task. Don't read the whole
wiki — read the rows that match what you're doing.

## Always useful

- `project/overview.md` — what we're building and for whom
- `project/roadmap.md` — where we are in the build
- `tasks/active.md` — the live cursor

## Hardware / host work

- `project/hardware.md`
- `project/deployment.md`
- `references/paths.md`

## Storage / filesystem / library layout work

- `project/storage.md`
- `conventions/media-naming.md`
- `references/paths.md`

## Media server (player layer) work

- `project/media-server.md`
- `conventions/media-naming.md`
- `project/networking-remote-access.md` (for client access)

## Acquisition stack (*arr / indexers / downloads / requests) work

- `project/acquisition-stack.md`
- `conventions/media-naming.md`
- `references/external-links.md` (TRaSH guides)
- `conventions/secrets.md`

## Networking / remote access / reverse proxy work

- `project/networking-remote-access.md`
- `references/paths.md` (ports, subdomains)
- `conventions/secrets.md`

## Deployment / compose / OS / backups work

- `project/deployment.md`
- `references/commands.md`
- `project/hardware.md`
- `docs/specs/backup-recovery.md`
- `scripts/homeflix_setup/backup.py` (`backup create|list|retrieve|prune|restore`)

## Stack-contract / static verification work

- `docs/specs/stack-contract.md`
- `scripts/homeflix_setup/contract.py`
- `project/deployment.md` (self-heal pair and phase allowlists)
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
- `conventions/git-and-commit-policy.md`
- `log.md`

## Decisions

For architectural or hard-to-reverse changes, check `decisions/` before adding a new
ADR. The big six homeflix forks: host OS/runtime, filesystem/RAID, media server,
acquisition stack shape, remote access, media naming scheme.
