# Verify core on a disposable Debian/Ubuntu host

Why: core setup and the deep-operations program are fixture-accepted only; general Debian/Ubuntu host support must not be claimed before a real-host run.
Done when: core setup and `verify core` pass on a disposable Debian/Ubuntu target and the redacted result is recorded as live acceptance. Disposable-host and private-production live acceptance remain separate.

## Steps

- [ ] Obtain a disposable Debian/Ubuntu target.
- [ ] Run core setup on existing mounted storage and `scripts/homeflix verify core`.
- [ ] Record the redacted evidence under `docs/research/`.

## Outcome

<Filled when the item is finished or dropped.>
