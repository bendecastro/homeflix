# Verification

## Requirements

- The system SHALL expose explicit static, read-only runtime, and disruptive verification intents. `(satisfied #4 for static; satisfied #5 for runtime; satisfied #9 for VPN gate; satisfied #10 for fail-closed)`
- Static verification SHALL validate configuration and the stack contract without requiring running containers. `(satisfied #4)`
- Runtime verification SHALL remain non-destructive and SHALL fail when any mandatory runtime domain is unavailable, unknown, or skipped. `(satisfied #5; VPN gate #9)`
- Optional or intentionally unselected capabilities MAY report not applicable without weakening mandatory evidence. `(satisfied #5)`
- Core runtime verification SHALL succeed when classified non-core or optional helper services are already present; their presence is a warning, not a mandatory failure. Unknown project services SHALL still fail. `(satisfied #14)`
- An explicit `verify core --discover-probe` intent MAY create and delete a uniquely named library probe to prove unconditional Jellyfin refresh; default `verify core` SHALL NOT write library files. `(satisfied #14)`
- Disruptive fail-closed verification SHALL require an explicit operator command and SHALL NOT run as part of routine runtime verification. `(satisfied #10)`
- Disruptive verification SHALL identify only the active tunnel interface, prove external access is blocked after disruption, and restore Gluetun plus every previously running namespace-dependent service after success, failure, timeout, or interruption. `(satisfied #10)`
- The post-disruption blocked-egress probe SHALL be bounded so it cannot consume the compensation window. Restore SHALL use an independent budget (or floor timeout) so Gluetun and previously running dependents still restart when the prove deadline is exhausted. `(satisfied #10)`
- A disruptive verification success SHALL require healthy post-restore egress through a non-host address. `(satisfied #10)`
- Verification output SHALL distinguish pass, warning, failure, not-applicable, and unknown outcomes. `(satisfied #5 for runtime; #9 for VPN gate; #10 for fail-closed)`
- Verification SHALL emit bounded boolean evidence without public IPs, private addresses, API keys, passwords, forwarded-port values, or secret-bearing URLs. `(satisfied #5 for runtime; #9 for VPN gate; #10 for fail-closed)`
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
