# PRD: Deep Homeflix operations

## Problem Statement

Homeflix has a strong agent-first core setup interface, but several load-bearing operational contracts remain distributed across Compose declarations, shell scripts, application UIs, prose, and proposed plans.

A syntactically valid rendered Compose model can violate the intended VPN, hardlink, proxy, or self-heal architecture. First-use application settings can pass their own built-in tests while the end-to-end behavior is still wrong. Verification currently lacks one explicit model for static, runtime, and intentionally disruptive evidence. Backup commands promise consistent application configuration while a failed SQLite snapshot can silently fall back to an unsafe live copy.

These gaps make installation and recovery less reliable for operators and force agents to reconstruct private production lessons rather than use a deep public module with a small, truthful interface.

## Solution

Deepen the existing `scripts/homeflix` architecture around four coordinated capabilities:

1. A pure stack-contract module validates the rendered Compose model against bounded, accepted structural invariants and is reused by static checks, runtime verification, and CI.
2. Core and acquisition reconciliation place first-use application behavior behind idempotent interfaces while preserving their strict phase seam.
3. Verification exposes explicit static, read-only runtime, and disruptive fail-closed intents with truthful success semantics and structured, secret-free evidence.
4. Backup and scratch restore use fail-closed snapshot creation and one artifact-repository seam with local and SSH adapters.

Existing shell command paths remain compatibility adapters. The proposed acquisition setup plan is absorbed into this PRD so there is one dependency-ordered public queue.

## User Stories

1. As an operator, I want CI to reject a Homeflix stack that would leak an acquisition service outside Gluetun, so that a valid Compose file is not mistaken for a private stack.
2. As an operator, I want CI to reject split or missing *arr data mounts, so that hardlink imports and continued seeding remain structural guarantees.
3. As an operator, I want CI to reject incomplete self-heal declarations, so that recovery mechanisms cannot become documented no-ops.
4. As an operator, I want proxy subnet and route ownership checked together, so that VPN-backed interfaces remain reachable without broad firewall bypasses.
5. As an agent, I want one canonical rendered stack model, so that I do not infer configuration from an incidental representative service.
6. As a maintainer, I want one stack-contract report reused everywhere, so that CI, preflight, and verification cannot drift.
7. As a maintainer, I want contract fixtures that remove one load-bearing declaration at a time, so that the deletion test remains executable.
8. As an operator, I want core setup to remain usable without acquisition credentials, so that family-facing services are not blocked by VPN setup.
9. As an operator, I want core reconciliation to configure reliable Jellyfin discovery for both known and newly imported titles, so that successful imports appear without manual scans.
10. As an operator, I want connection verification to test the behavior that matters rather than trust an application’s shallow built-in test, so that a passing setup result is meaningful.
11. As an operator, I want acquisition setup to start Gluetun before any risky service, so that no downloader or indexer can start with direct egress.
12. As an operator, I want VPN secrets entered through a controlling terminal and never returned in JSON, so that agent-led setup does not expose credentials.
13. As an operator, I want qBittorrent paths and categories reconciled to the single-root layout, so that downloads do not fail under a nonexistent stock path.
14. As an operator, I want qBittorrent’s forwarded listen port reconciled after tunnel reconnects, so that inbound peers and seeding remain effective.
15. As an operator, I want the localhost authentication prerequisite verified without exposing the WebUI credential, so that automatic port updates cannot silently stop.
16. As an operator, I want optional NZBGet to remain stopped until selected and configured, so that defined services are not mistaken for enabled services.
17. As an operator, I want Prowlarr, Radarr, and Sonarr connections reconciled through Docker service addresses, so that internal traffic follows the intended topology.
18. As an operator, I want acquisition reruns to repair safe drift without duplicating clients, categories, applications, or connections, so that interrupted setup is resumable.
19. As an agent, I want static verification to work without a running stack, so that architecture failures are cheap to diagnose.
20. As an agent, I want runtime verification to fail when a mandatory domain cannot be observed, so that a successful exit never means “everything skipped.”
21. As an operator, I want disruptive fail-closed verification to require an explicit command, so that a routine status check never changes live state.
22. As an operator, I want disruptive verification to restore Gluetun and every previously running namespace-dependent service after success, failure, timeout, or interruption, so that the safety test does not become an outage.
23. As an operator, I want verification output to report booleans and bounded diagnostics without public IPs, API keys, or passwords, so that evidence can be shared safely.
24. As an operator, I want backup creation to abort if any SQLite database cannot be snapshotted consistently, so that an archive never overstates its recovery quality.
25. As an operator, I want the same backup lifecycle for local off-filesystem and SSH destinations, so that transport choice does not change correctness.
26. As an operator, I want archive retrieval and extraction to reject unsafe names, links, special files, and path traversal, so that a restore drill cannot write outside its scratch destination.
27. As an operator, I want scratch restore to verify every SQLite snapshot and refuse the live configuration root, so that restore evidence is safe and meaningful.
28. As an operator, I want existing shell commands to continue working as compatibility adapters, so that documentation and automation do not break during migration.
29. As a maintainer, I want the canonical implementation in Python’s standard library, so that Homeflix remains dependency-light on supported hosts.
30. As a maintainer, I want fixture acceptance clearly distinguished from live acceptance, so that public claims match available evidence.

## Implementation Decisions

- `scripts/homeflix` remains the canonical operator and agent interface. Shell entry points delegate to it and own no independent configuration, verification, or backup behavior.
- The stack-contract module accepts one rendered Compose mapping and returns a structured report. Rendering is an adapter outside the module and occurs once per operation.
- The initial stack contract is intentionally bounded to structural invariants whose deletion silently breaks privacy, hardlink storage, routing, recovery, or phase isolation:
  - the exact VPN-risky service set shares Gluetun’s network namespace;
  - every namespace-sharing service has the required healthcheck, Gluetun health-server probe, and deunhealth pairing;
  - Radarr, Sonarr, and Lidarr receive exactly the single data root at `/data` with fail-closed host binds;
  - Gluetun owns proxy routes for namespace-sharing services;
  - the proxy network is pinned and represented in Gluetun’s outbound allowlist separately from the household LAN;
  - core and acquisition deployment allowlists remain explicit and disjoint.
- Open TLS, remote-access, image-update, and optional-service decisions are not contract violations.
- `verify contract` is static. Existing phase verification remains read-only and includes the static contract. A separate explicit VPN fail-closed command owns disruption and compensation.
- Runtime verification cannot pass when Docker or another mandatory runtime domain is unavailable. Optional or intentionally unselected capabilities may report as not applicable without weakening required evidence.
- The verification result model distinguishes pass, warning, failure, not-applicable, and unknown. Unknown mandatory evidence fails the selected verification mode.
- Core reconciliation owns family-facing initialization and reliable library discovery. It ensures both the targeted Jellyfin connection and the unconditional refresh mechanism needed for genuinely new titles, without exposing the Jellyfin credential used by the applications.
- Acquisition reconciliation preserves the existing approved core/acquisition seam and absorbs the proposed acquisition plan: secure provider-secret entry, Gluetun-only startup, current fail-closed evidence, selected download clients, qBittorrent/NZBGet desired state, Prowlarr applications, *arr download clients, and resumable verification.
- Application adapters accept dependencies rather than creating transports internally. Reconciliation reports only owned state and never overwrites unrelated user choices.
- Backup snapshot creation is fail-closed: any discovered SQLite snapshot failure prevents artifact publication. Logs, media, `.env`, and LUKS recovery material remain outside the artifact.
- The artifact-repository seam provides list, get, put, and prune behavior. Local and SSH adapters satisfy the same interface; scheduling remains a compatibility concern rather than a new scheduler product.
- Restore remains scratch-only, rejects the live configuration root, validates archive members before extraction, and requires at least one valid SQLite database for successful recovery evidence.
- Structured output remains bounded, schema-versioned, and secret-free. Existing setup state continues to store only non-secret checkpoints and evidence.
- The architecture uses Python 3’s standard library and the existing command-runner, environment-document, API-client, state, fixture, and deadline patterns.
- A reusable defect found while proving a live deployment blocks that deployment's adoption closeout until every accepted acceptance row is implemented, fixture-accepted in public, and re-adopted. Fixing the original symptom while leaving an accepted row unmet does not clear the defect: otherwise a green live verify can close the loop while operators still get an undifferentiated failure for the same class. An enhancement or newly scoped follow-up that was not an accepted row of that defect may stay open as remaining public work and does not block closeout. Informal wiki or comment notes cannot waive this; change the planning contract if the gate should move.

## Testing Decisions

The highest public seam is the structured `scripts/homeflix --json` interface. Fixture journeys should exercise complete operator outcomes through that seam, while pure modules receive focused tests for combinatorial safety rules.

- Stack-contract tests render or load safe Compose fixtures, mutate one load-bearing declaration per case, and assert stable structured findings. A real `.env.example` render is checked in CI.
- Configuration extraction tests scan all relevant services and require agreement; no field depends on Radarr or Jellyfin merely being present as a representative.
- Core reconciliation tests use existing HTTP fixtures to prove targeted and unconditional Jellyfin refresh mechanisms, behavioral inspection, idempotence, conflicts, and redaction.
- Acquisition tests retain the proposed plan’s fake-runner and API-fixture coverage for secrets, Gluetun ordering, egress comparison, fail-closed disruption, restoration, qBittorrent/NZBGet, Prowlarr, *arr connections, resumability, and output scanning.
- Verification tests exercise static, runtime, and disruptive intents through one result model. Required-domain absence, timeout, malformed state, failed compensation, and all-skip paths must fail.
- Backup tests use real temporary SQLite databases and a temporary local artifact repository. They inject snapshot failures, retention edges, unsafe archive members, restore conflicts, and integrity failures. A fake SSH adapter proves equivalent command ordering, quoting, redaction, and failure semantics.
- Compatibility tests execute each legacy shell path and assert delegation to the canonical CLI without duplicate implementation.
- Full acceptance runs the Python test suite, rendered Compose validation, shell syntax and ShellCheck where available, documentation-link tests, secret-output scans, and `git diff --check`.
- Public completion remains fixture acceptance until a disposable supported host and the private production downstream provide live evidence.

## Out of Scope

- Remote access, TLS certificate strategy, and household device onboarding.
- Choosing between image pinning and unattended updates.
- Adding or requesting media titles as part of setup.
- Provider-specific indexer account automation beyond secure credential gates and generic adapters.
- Media backup, `.env` escrow, LUKS header backup, or full bare-metal recovery.
- Shipping an SSH transport inside the CLI; an agent may still operate a remote checkout using its own SSH capability.
- Replacing Docker Compose, Gluetun, Jellyfin, Jellyseerr, the *arr applications, or the single-root hardlink architecture.
- Claiming general production support from fixtures alone.

## Further Notes

- Planning evidence is recorded in `docs/changes/deep-homeflix-operations/architecture-review.md`.
- This PRD supersedes the proposed agent-first acquisition implementation plan while preserving its safety decisions and intended behavior.
- Production-specific paths, addresses, credentials, household facts, and live state must never be copied into this public repository.
- The private production queue adopts public commits, gathers redacted live evidence, and returns generalized defects to public before it closes. Closeout uses the reusable-defect rule in Implementation Decisions.
