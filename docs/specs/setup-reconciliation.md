# Setup reconciliation

## Requirements

- The system SHALL preserve separate core and acquisition phases. Acquisition credentials or capabilities SHALL NOT block core setup. `(satisfied #9 for credential isolation)`
- Reconciliation SHALL inspect live state and make only the smallest safe changes to setup-owned state. `(pending #3)`
- A no-change rerun SHALL NOT duplicate accounts, libraries, roots, connections, categories, clients, applications, or indexers. `(pending #3)`
- Core reconciliation SHALL configure reliable Jellyfin discovery for both known and genuinely new imported titles. `(pending #3)`
- Core reconciliation SHALL verify unconditional library refresh behavior rather than trusting only a targeted connection's built-in test. `(pending #3)`
- Acquisition reconciliation SHALL require current VPN health and fail-closed evidence before starting any risky service. `(pending #10; VPN gate #9)`
- Provider secrets SHALL enter through a controlling terminal, SHALL NOT be accepted as command arguments or JSON input, and SHALL NOT appear in structured output or setup state. `(satisfied #9)`
- qBittorrent reconciliation SHALL configure the single-root download paths, selected categories, durable credentials, localhost port-update prerequisite, and current forwarded listen port without exposing secret values. `(pending #3)`
- Optional NZBGet SHALL remain stopped and unconfigured unless selected; provider servers SHALL remain disabled until credentials are present. `(pending #3)`
- Prowlarr and the *arr applications SHALL use Docker service addresses that preserve the Gluetun topology. `(pending #3)`
- Reconciliation SHALL preserve unrelated user-owned application choices and SHALL fail on ambiguous conflicting resources. `(pending #3)`

## Key scenarios

1. Core reaches verified family-facing services with empty VPN credentials and no acquisition containers.
2. A new title unknown to Jellyfin is discovered after the import event without a manual scan.
3. qBittorrent starts with a stock invalid path; reconciliation repairs it and a second run is a no-op.
4. A tunnel reconnect changes the forwarded port; reconciliation updates qBittorrent without printing either credential or port evidence.
5. Interrupted acquisition setup resumes without duplicate clients, categories, or application connections.
