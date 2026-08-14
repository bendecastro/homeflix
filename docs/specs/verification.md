# Verification

## Requirements

- The system SHALL expose explicit static, read-only runtime, and disruptive verification intents. `(pending #3; static+runtime #5)`
- Static verification SHALL validate configuration and the stack contract without requiring running containers. `(satisfied #4)`
- Runtime verification SHALL remain non-destructive and SHALL fail when any mandatory runtime domain is unavailable, unknown, or skipped. `(satisfied #5)`
- Optional or intentionally unselected capabilities MAY report not applicable without weakening mandatory evidence. `(satisfied #5)`
- Disruptive fail-closed verification SHALL require an explicit operator command and SHALL NOT run as part of routine runtime verification. `(pending #3)`
- Disruptive verification SHALL identify only the active tunnel interface, prove external access is blocked after disruption, and restore Gluetun plus every previously running namespace-dependent service after success, failure, timeout, or interruption. `(pending #3)`
- A disruptive verification success SHALL require healthy post-restore egress through a non-host address. `(pending #3)`
- Verification output SHALL distinguish pass, warning, failure, not-applicable, and unknown outcomes. `(satisfied #5 for runtime)`
- Verification SHALL emit bounded boolean evidence without public IPs, private addresses, API keys, passwords, forwarded-port values, or secret-bearing URLs. `(satisfied #5 for runtime)`
- Fixture acceptance and live acceptance SHALL be reported as different evidence levels. `(pending #3)`

## Key scenarios

1. Docker is unavailable during runtime verification; the command fails rather than succeeding with skips.
2. Static verification runs successfully on a machine with no running Homeflix stack.
3. Fail-closed verification is interrupted after tunnel disruption; compensation still restores the prior running service set.
4. One dependent service fails to restart; the transaction fails and reports bounded recovery guidance.
5. Fixture tests pass, but no real host has been exercised; completion is reported only as fixture acceptance.
