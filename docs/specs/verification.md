# Verification

## Requirements

- The system SHALL expose explicit static, read-only runtime, and disruptive verification intents. `(satisfied #4 for static; satisfied #5 for runtime; satisfied #9 for VPN gate; satisfied #10 for fail-closed)`
- Static verification SHALL validate configuration and the stack contract without requiring running containers. `(satisfied #4)`
- Runtime verification SHALL remain non-destructive and SHALL fail when any mandatory runtime domain is unavailable, unknown, or skipped. `(satisfied #5; VPN gate #9)`
- Optional or intentionally unselected capabilities MAY report not applicable without weakening mandatory evidence. `(satisfied #5)`
- Core runtime verification SHALL succeed when classified non-core or optional helper services are already present; their presence is a warning, not a mandatory failure. Unknown project services SHALL still fail. `(satisfied #14)`
- Core Traefik readiness SHALL use a family-facing proxy path and SHALL NOT require a published dashboard on `:8080`. `(satisfied #15)`
- Rendering SHALL restore `create_host_path: false` when the project file declared it and the Compose JSON renderer omitted the key. Runtime `verify core` SHALL use that same restored model. `(satisfied #15)`
- An explicit `verify core --discover-probe` intent MAY create and delete a uniquely named library probe to prove unconditional Jellyfin refresh; default `verify core` SHALL NOT write library files. `(satisfied #14)`
- Existing-stack `verify core` SHALL inspect using the dedicated Jellyfin application key and the unique default non-4K quality profile already selected in Jellyseerr when admin or `QUALITY_PROFILE` env keys are absent. `(satisfied #16)`
- Application-data directory walks SHALL accept group-writable directories owned by the expected uid; secret files and other-writable directories SHALL still be refused. `(satisfied #16)`
- Disruptive fail-closed verification SHALL require an explicit operator command and SHALL NOT run as part of routine runtime verification. `(satisfied #10)`
- Non-disruptive VPN-gate evidence SHALL be collectable while gated services are already running, and only while every running gated service shares the Gluetun network namespace; a gated service running outside that namespace, or whose namespace cannot be inspected, SHALL fail the gate. `(satisfied #18)`
- Disruptive verification SHALL identify only the active tunnel interface, prove external access is blocked after disruption, and restore Gluetun plus every previously running namespace-dependent service after success, failure, timeout, or interruption. `(satisfied #10)`
- The post-disruption blocked-egress probe SHALL give its own tool timeout room to elapse inside the probe budget, so a firewall that drops packets yields observable blocked-egress evidence rather than an uninspectable transaction; a genuinely hung probe SHALL still fall through to compensation. `(satisfied #19)`
- The post-disruption blocked-egress probe SHALL be bounded so it cannot consume the compensation window. Restore SHALL use an independent budget (or floor timeout) so Gluetun and previously running dependents still restart when the prove deadline is exhausted. `(satisfied #10)`
- A disruptive verification success SHALL require healthy post-restore egress through a non-host address. `(satisfied #10)`
- Verification output SHALL distinguish pass, warning, failure, not-applicable, and unknown outcomes. `(satisfied #5 for runtime; #9 for VPN gate; #10 for fail-closed)`
- Verification SHALL emit bounded boolean evidence without public IPs, private addresses, API keys, passwords, forwarded-port values, or secret-bearing URLs. `(satisfied #5 for runtime; #9 for VPN gate; #10 for fail-closed)`
- Acquisition verification SHALL distinguish port forwarding that is not configured from a configured forwarded port that is unavailable. Both SHALL NOT be reported as a bare listen-port agreement failure. `(satisfied #23)`
- Fixture acceptance and live acceptance SHALL be reported as different evidence levels. `(satisfied #12 for phase-level acquisition fixture journeys)`
- Live request-to-library, disposable-host, and private-production live acceptance remain separate. They are not part of this program's fixture acceptance.

## Key scenarios

1. Docker is unavailable during runtime verification; the command fails rather than succeeding with skips.
2. Static verification runs successfully on a machine with no running Homeflix stack.
3. Fail-closed verification is interrupted after tunnel disruption; compensation still restores the prior running service set.
4. One dependent service fails to restart; the transaction fails and reports bounded recovery guidance.
5. Fixture tests pass, but no real host has been exercised; completion is reported only as fixture acceptance. Disposable-host and private-production live acceptance remain separate.
6. After disruption, a hung blocked-egress probe exhausts the prove deadline; compensation still issues `compose restart` for Gluetun and the snapshot.
7. Core verification on a host that already runs classified acquisition or optional helpers passes; an unknown project service still fails.
8. `verify core --discover-probe` surfaces a uniquely named probe through `/Library/Refresh` and removes only that probe.
9. `verify acquisition` reports not-applicable when port forwarding is disabled, and a configured-but-unavailable failure when forwarding is on but the status file cannot be read.
