# Architecture review evidence

Date: 2026-08-14

This change follows a review of the private production downstream and the newer public setup architecture. The review used the deletion test and the shared module/interface/depth/seam/adapter/leverage/locality vocabulary.

## Generalized findings

1. **Accepted Compose decisions are not one executable stack contract.** Schema validation and scattered assertions can miss deletion of one VPN namespace declaration, one hardlink-preserving *arr mount, or one required route while the rest of the stack remains valid.
2. **First-use behavior is distributed across guides and application UIs.** A built-in connection test can pass while newly imported titles remain undiscovered; qBittorrent also has ordering-sensitive path, authentication-bypass, and forwarded-port requirements.
3. **Verification intents are conflated.** Static policy, read-only runtime checks, and a disruptive fail-closed transaction need different success and safety semantics.
4. **The backup consistency promise is not fail-closed.** If a SQLite snapshot fails, the existing shell workflow can retain and archive the earlier live copy. Local and SSH destination behavior is duplicated across backup and restore.

## Existing public leverage

The public repository already provides the canonical `scripts/homeflix` Python interface, fixture runners, API adapters, phase-aware preflight, resumable core reconciliation, and structured secret-free output. This program deepens those modules rather than introducing a second implementation.

The existing proposed acquisition plan is absorbed into this change. Its secure secret handoff, Gluetun-first startup, fail-closed proof, qBittorrent/NZBGet reconciliation, Prowlarr/*arr connections, and resumability requirements remain in force.

## Rejected directions

- Do not hide explicit safety-critical Compose declarations behind additional YAML generation.
- Do not merge core and acquisition into one phase.
- Do not merge preflight capability checks with deployed-outcome verification.
- Do not generalize the already cohesive forwarded-port and dashboard-credential adapters.
- Do not enforce unresolved TLS, remote-access, or image-update choices as stack-contract violations.
- Do not copy production state, paths, addresses, credentials, or household assumptions into this public repository.
