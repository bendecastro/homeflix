# Agent-assisted core setup

Use this guide when working with a capable coding agent in a Homeflix checkout. The current
supported outcome is **Homeflix core on the local Debian or Ubuntu machine, using an existing
mounted data filesystem**. The CLI executes on the machine containing the checkout; it has no
SSH transport. An agent may use its own SSH capability to operate a checkout on another host,
but SSH-accessible orchestration remains a capability and design target, not a shipped CLI
feature.

Encrypted-storage provisioning and acquisition (VPN, Gluetun, download clients, and indexers)
are planned follow-up slices. They are not part of current core setup. The automated
implementation is **fixture-tested only** and has **not received disposable real Debian/Ubuntu
acceptance**. Treat live-host completion as unverified until the evidence below is collected on
the target.

## Copy and paste an intent

- `Set up Homeflix core on this Debian or Ubuntu machine using my existing mounted storage.`
- `Resume Homeflix core setup and verify the live deployment.`
- `Show me a dry-run plan for Homeflix core setup without changing this machine.`

## Start with capabilities, not a universal script

The agent should first run `scripts/homeflix --help`, then use `scripts/homeflix --json discover`
to inspect the target without persisting its private facts. It should select the smallest
public primitives suited to the discovered host rather than replaying a rigid sequence.
Discovery covers distribution support, identity, Docker and Compose, mounted filesystems,
ports, DNS, and usable graphics devices. The user must review plans before mutation.

Current core needs existing absolute `DATA_ROOT`, `CONFIG_ROOT`, and `CACHE_ROOT` directories.
`DATA_ROOT` must be on a mounted, hardlink-capable filesystem rather than the host root
filesystem. Downloads and media remain under the same root and are exposed to each *arr
container as one `/data` mount.

## Decisions and human gates

Before changing the host, the agent presents discovered facts, capability gaps, chosen paths,
the quality-profile name, whether QuickSync is usable, and whether unresolved LAN DNS requires
temporary direct setup ports. The person chooses non-inferable paths and preferences and
approves the exact host-preparation plan. `host prepare` is read-only by default; applying it
requires `--apply` and the reviewed plan fingerprint. Sudo authentication stays a human gate.

Use `scripts/homeflix --json setup core --dry-run` for a non-mutating setup plan. Dry-run must
show only Traefik, Jellyfin, Jellyseerr, Radarr, and Sonarr and an empty acquisition-mutation
list. Configuration writes ignored local files and generates the Jellyfin administrator
credential without printing its value. Retrieve that credential only from an unredirected
controlling terminal with `scripts/homeflix secrets reveal jellyfin`; never paste it into chat
or JSON output.

Core preflight may create and remove bounded hardlink probes inside existing `torrents/` and
`media/` directories. The agent must explain that operation before a live run. Missing VPN
credentials are warnings during core preflight. Core can be configured and verified without
them, but setup then stops safely: do not start acquisition services or improvise provider
credential handling.

## Existing-storage core outcome

The agent may compose these capabilities after discovery and approval:

1. Plan or prepare missing Docker/Compose prerequisites with `host prepare`.
2. Use `configure` with the three reviewed existing paths and a chosen quality-profile name.
   During `initialize core`, that exact name is resolved and validated independently against
   live Radarr and Sonarr; initialization fails safely if either service does not provide it.
   DNS failure produces deterministic direct Jellyseerr/Radarr/Sonarr setup ports;
   QuickSync is selected only when discovery proves the Intel render device usable.
3. Run `preflight --phase core` and review every failure or warning.
4. Reconcile only the core allowlist with `deploy core`.
5. Reconcile application state with `initialize core`.
6. Inspect the live result with `verify core`.

`scripts/homeflix setup core` is a resumable convenience composition of those primitives, not
a replacement for agent judgment. The API operations initialize the Jellyfin administrator
and exact Movies/Shows/Music libraries, Radarr/Sonarr roots and media settings, and Jellyseerr
connections. They discover runtime IDs and reconcile equivalent resources rather than assuming
fixed IDs.

## Recovery, status, and resume

Run `scripts/homeflix --json status` to read ignored, non-secret checkpoint evidence. A
checkpoint is not proof that a live resource still exists. After interruption, rediscover the
host, review current configuration, and use `scripts/homeflix setup core` or the individual
primitive matching the failed stage. Deployment and API reconciliation inspect live state and
repair safe missing resources. On a no-change rerun, `deploy core` reports `already_ready`, while
`setup core` completes as `verified` without changes. Neither may duplicate the administrator,
libraries, root folders, servers, or services.

Do not delete appdata, rewrite user-owned application choices, start the whole Compose project,
or run acquisition to recover core. If verification cannot inspect a domain, report it as
failed or unknown rather than declaring completion.

## Evidence required before completion

A live-host completion report should include, without secrets or private addresses:

- supported Debian/Ubuntu discovery and an existing mounted filesystem identity/type;
- reviewed path and host-preparation decisions;
- core preflight results, including a successful hardlink and cleanup check;
- exact Compose project identity and the five healthy, ready core services;
- acquisition-service absence and the expected QuickSync selection or “not selected” result;
- initialized Jellyfin libraries, exact Radarr/Sonarr roots and settings, and initialized
  Jellyseerr default connections;
- a successful `verify core`, followed by a no-duplicate rerun; and
- confirmation that `.env`, `.homeflix/setup.json`, and the generated override remain ignored.

Until this is performed on a disposable real Debian/Ubuntu host, report only fixture acceptance,
not production verification. For a fully manual alternative, use the [manual quickstart](quickstart.md).
Configuration and local-artifact details are in the [configuration reference](configuration.md).
