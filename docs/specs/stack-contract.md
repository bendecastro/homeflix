# Stack contract

## Requirements

- The system SHALL derive one canonical model from rendered Compose configuration and reuse it for static and runtime validation. `(satisfied #4)`
- The system SHALL reject any rendered model where the exact VPN-risky service set does not share Gluetun's network namespace. `(satisfied #4)`
- Every service sharing Gluetun's network namespace SHALL define an active healthcheck that probes Gluetun's namespace-sensitive health endpoint and SHALL carry the deunhealth restart label. `(satisfied #4)`
- Radarr, Sonarr, and Lidarr SHALL each receive exactly the single configured data root at `/data`; their bind SHALL refuse automatic host-path creation. `(satisfied #4)`
- Gluetun SHALL own proxy routes for services that have no independently discoverable network address. `(satisfied #4)`
- The proxy network SHALL use the configured pinned subnet, and Gluetun SHALL allowlist that subnet separately from the household LAN. `(satisfied #4)`
- Core and acquisition deployment allowlists SHALL be explicit and disjoint. Core operations SHALL NOT start acquisition services. `(satisfied #4)`
- The initial contract SHALL NOT fail unresolved TLS, remote-access, image-update, or optional-service decisions. `(satisfied #4)`
- Contract findings SHALL be structured, bounded, deterministic, and free of resolved secret values. `(satisfied #4)`

## Key scenarios

1. Removing Prowlarr's Gluetun namespace declaration fails even when qBittorrent and NZBGet remain correct.
2. Splitting one *arr service's downloads and media into separate mounts, or remounting one *arr `/data` onto a different host path, fails even when siblings still share one filesystem.
3. Removing either half of the healthcheck/deunhealth pair fails.
4. Moving a VPN-backed proxy route from Gluetun to the namespace-sharing service, or deleting only its Host rule while another router key remains, fails.
5. A configured but unresolved remote-access or image-pin choice does not become a contract failure.
