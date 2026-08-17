# Agent-first setup

Status: Approved
Updated: 2026-08-17

## Goal

A person should be able to clone Homeflix, open the repository in a capable coding agent,
and ask it to set up Homeflix on a local or SSH-accessible Debian/Ubuntu host. The agent
handles every mechanical or API-addressable step and asks the person only for real choices,
explicit destructive approval, unavailable sudo authentication, and secure provider-secret
entry.

## Supported setup

- Debian and Ubuntu hosts, locally or over SSH.
- Existing mounted storage, or optional dedicated-disk provisioning with LUKS2 and ext4.
- A resumable core phase that does not require VPN credentials.
- API-driven Jellyfin, Radarr, Sonarr, and Jellyseerr initialization.
- A later acquisition phase gated on VPN egress and fail-closed verification.
- QuickSync detection and an ignored host-specific Compose override when supported.

Other distributions remain usable through the manual quickstart, but automated host
provisioning does not claim to support them initially.

## Delivery status

The existing-storage core slice is implemented for a checkout running locally on a Debian or
Ubuntu target and passes fixture acceptance, including interruption-safe resume. The CLI has no
SSH transport; an agent may operate a target checkout through its own SSH capability. The
VPN/acquisition program (issues #4–#13) is fixture-accepted shipped CLI: stack contract,
truthful verification, fail-closed backup, the VPN gate, and selected acquisition. Encrypted
storage remains a later approved follow-up plan, not a shipped feature. Disposable-host and
private-production live acceptance remain separate. No disposable real-host acceptance has
occurred, so the public implementation is not yet described as generally production-verified.

## Architecture

The coding agent remains the orchestrator because it can inspect an unfamiliar host and
exercise judgment. A dependency-light Python 3 CLI at `scripts/homeflix` provides small,
idempotent primitives for deterministic work. Commands expose structured output for agents,
dry-run or plan modes for mutations, and live reconciliation rather than trusting checkpoint
state blindly.

The root `AGENTS.md` stays lean: it routes setup intent to `docs/agent-setup.md`, states
outcomes and safety boundaries, and lets the agent select the relevant CLI primitives. It
does not prescribe a brittle universal sequence.

## Local state and secrets

- `.env` is the canonical configuration and secret file, is ignored, and must be mode 0600.
- `docker-compose.override.yml` is ignored and holds detected host adaptations such as
  QuickSync and temporary direct setup ports.
- `.homeflix/setup.json` is ignored and stores only non-secret checkpoints and host facts.
- No command prints secret values or writes them to setup state.
- `scripts/homeflix secrets vpn` reads provider credentials from a controlling terminal
  without echoing them and updates `.env` atomically.

Generated service credentials can live in `.env` without being passed to containers unless
Compose explicitly references them. The setup CLI consumes them for API initialization.

## Agent workflow

1. Discover OS, Docker, identity, storage, hardware acceleration, ports, and DNS.
2. Infer safe defaults and ask only for choices that cannot be established from the host.
3. Validate an existing filesystem, or present an exact block-device plan and wait for
   explicit destructive confirmation before provisioning encrypted storage.
4. Prepare Docker, directories, `.env`, and the ignored override.
5. Run core preflight; missing VPN credentials are not a core failure.
6. Start Traefik, Jellyfin, Jellyseerr, Radarr, and Sonarr explicitly.
7. Use service APIs to create the Jellyfin administrator, libraries, Radarr/Sonarr roots and
   media settings, and Jellyseerr connections. Discover profile IDs at runtime; recommend a
   balanced 720p/1080p profile unless the user chooses otherwise.
8. Verify live APIs, mounts, roots, initialization state, and hardware passthrough.
9. Stop cleanly at a resumable checkpoint when VPN credentials are unavailable.
10. After secure credential entry, start Gluetun alone, verify tunnel egress and fail-closed
    behavior, then start and connect the selected acquisition services.

## Human gates

The agent should not send the user through a browser when a supported API can perform the
same operation. Human action is limited to:

- choosing the target host and any non-inferable media preferences;
- confirming the exact destructive storage plan;
- satisfying sudo authentication if the agent cannot;
- entering VPN, indexer, or provider credentials through the secure terminal helper;
- making router/DNS changes the agent cannot access; and
- choosing an off-host destination for the LUKS header backup.

Direct host ports provide a working fallback while LAN DNS remains unconfigured.

## Safety properties

- Storage plans identify devices with stable metadata and are rejected if the fingerprint,
  mount state, root-device relationship, or plan age changes before application.
- `${DATA_ROOT}` mounts use `bind.create_host_path: false`, preventing fallback writes to the
  system disk when storage is absent.
- The Compose project name is pinned so checkout renames cannot create a duplicate stack.
- Acquisition containers do not start before Gluetun verification.
- Internal service connections use Docker DNS and plain HTTP; public/LAN URLs are separate.
- Re-running any completed phase reconciles and repairs safe drift rather than duplicating
  resources or accounts.

## Delivery slices

1. [Agent-first core setup](agent-first-core-setup-plan.md): existing storage through a
   fully initialized and verified core stack.
2. [Encrypted storage provisioning](agent-first-storage-plan.md): optional guarded LUKS2 and
   ext4 preparation plus recovery artifacts.
3. [Deep Homeflix operations](../../docs/changes/deep-homeflix-operations/prd.md), issues
   #4–#13: stack contract, truthful verification, reliable discovery, fail-closed
   backup, VPN gate, selected acquisition, and program fixture acceptance. This
   supersedes the earlier acquisition implementation plan. The program is
   fixture-accepted only.

Each slice is independently testable and useful. Core setup shipped first.

## Success criteria

- On a clean supported host with existing mounted storage, an agent can reach a working
  Jellyseerr dashboard and connected Jellyfin/Radarr/Sonarr services without browser wizards.
- Empty VPN credentials do not block or accidentally start acquisition services.
- Interrupted setup resumes without duplicate resources.
- Dedicated root disks and changed/stale storage targets are refused.
- Secrets and host-specific state remain absent from tracked files and command output.
- Manual setup remains documented as a fallback.
