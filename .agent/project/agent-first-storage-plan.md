# Agent-first Encrypted Storage Implementation Plan

Status: Proposed
Updated: 2026-08-04

**Goal:** Let an agent safely validate existing storage or provision one explicitly approved dedicated disk/partition as LUKS2 plus ext4 with automatic unlock, persistent mounting, and a recovery header backup.

**Architecture:** Storage discovery and planning are read-only and produce a short-lived plan bound to stable block-device metadata. Destructive application is a separate command that re-discovers the device, rejects any changed/root/mounted target, and requires the plan confirmation token plus secure recovery-passphrase entry.

**Tech stack:** Python 3 standard library, `unittest`, `lsblk` JSON, `findmnt`, `cryptsetup`, `parted`, `mkfs.ext4`, `/etc/crypttab`, `/etc/fstab`, systemd.

**Execution note:** Work task-by-task. Use TDD where behavior changes. Verify and commit each task independently.

---

## File map

### Create

- `scripts/homeflix_setup/storage.py` — block-device discovery, fingerprints, plans, validation, and apply orchestration.
- `scripts/homeflix_setup/systemfiles.py` — marker-bounded atomic edits for `crypttab` and `fstab`.
- `tests/test_storage_discovery.py` — `lsblk`/`findmnt` fixture tests.
- `tests/test_storage_plan.py` — existing/dedicated plan and refusal tests.
- `tests/test_storage_apply.py` — exact destructive command and interruption tests.
- `tests/test_systemfiles.py` — idempotent system-file edit tests.
- `tests/fixtures/storage/` — redacted Debian/Ubuntu block-device fixtures.

### Modify

- `scripts/homeflix_setup/cli.py` — `storage discover|plan|apply|verify` commands.
- `scripts/homeflix_setup/preflight.py` — consume verified storage facts.
- `docs/agent-setup.md`, `docs/quickstart.md`, `docs/hardware.md` — storage choices and human gates.
- `.agent/project/agent-first-setup.md`, `.agent/tasks/active.md`, `.agent/log.md` — execution state.

## Task 1: Model block devices and reject unsafe targets

- [ ] Add failing fixture tests for SATA/NVMe/USB disks, partitions, LVM, mounted devices, encrypted mappings, root/boot/swap ancestry, absent serial/WWN, and names changing between `/dev/sdX` values.
- [ ] Run `python3 -m unittest tests.test_storage_discovery -v`; expect failures because `storage.py` does not exist.
- [ ] Implement immutable `BlockDevice`, `MountUse`, and `DeviceInventory` models from `lsblk --json --bytes --output NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,FSVER,LABEL,UUID,PARTUUID,MODEL,SERIAL,WWN,MAJ:MIN,PKNAME,MOUNTPOINTS,RO,RM` plus `findmnt --json`.
- [ ] Implement `stable_device_id()` preferring WWN, then serial+model+size, then PARTUUID; targets without a stable identity may be inspected but cannot be destructively applied.
- [ ] Implement `unsafe_reasons()` that blocks read-only devices, mounted filesystems, swap, root/boot ancestry, active device-mapper/LVM/RAID membership, and any target containing mounted descendants.
- [ ] Run the discovery tests and `scripts/homeflix --json storage discover | python3 -m json.tool`; expect green tests and valid JSON without persisting serials automatically.
- [ ] Commit with `git add scripts/homeflix_setup/storage.py scripts/homeflix_setup/cli.py tests/test_storage_discovery.py tests/fixtures/storage && git commit -m "Discover storage targets safely"`.

## Task 2: Produce immutable existing-storage and destructive plans

- [ ] Add failing tests for `storage plan --existing /mount` verifying ext4 hardlinks/free space/write access, and for `--device <stable-id>` producing either a whole-disk GPT/single-partition plan or an existing-empty-partition plan.
- [ ] Add refusal tests for path/device ambiguity, unsupported exFAT/NTFS, changed mount state, targets smaller than 100GB, root descendants, non-empty signatures without `--replace-signatures`, and plans older than 30 minutes.
- [ ] Run `python3 -m unittest tests.test_storage_plan -v`; expect failures.
- [ ] Implement `StoragePlan` schema version 1 with target fingerprint, mode, exact commands, resulting mapper/mount names, expected UUID placeholders, creation/expiry timestamps, and a SHA-256 confirmation token over canonical non-secret plan fields.
- [ ] Write plans atomically to `.homeflix/storage-plan.json`; text output must show model, size, stable ID, existing signatures, every destructive operation, and only a short confirmation token.
- [ ] Make `storage plan --existing` non-destructive and immediately usable by core configuration after live verification.
- [ ] Run plan tests and verify two runs against identical fixtures produce identical canonical plan content except timestamps/token.
- [ ] Commit with `git add scripts/homeflix_setup/storage.py scripts/homeflix_setup/cli.py tests/test_storage_plan.py && git commit -m "Plan existing and encrypted storage"`.

## Task 3: Apply LUKS2 and ext4 only to a revalidated plan

- [ ] Add failing tests proving `storage apply` requires root, a non-expired plan, exact confirmation token, stable-ID/fingerprint match, no new mount/signature/root relationship, and a controlling terminal for twice-entered recovery-passphrase input.
- [ ] Add command-runner tests for whole-disk order: install/check tools → `wipefs` → GPT/single aligned partition → settle → LUKS2 format → open mapper → ext4 format. Partition mode must omit partition-table operations.
- [ ] Add interruption tests after each command; rerun must detect completed safe steps, refuse contradictory state, and never repeat `luksFormat` or `mkfs` on an initialized target.
- [ ] Run `python3 -m unittest tests.test_storage_apply -v`; expect failures.
- [ ] Implement `revalidate_plan()`, `read_secret_twice_from_tty()`, `apply_partition_layout()`, `format_luks2()`, `open_mapper()`, and `format_ext4()` using argv arrays and stdin rather than shell command strings.
- [ ] Keep recovery passphrases out of arguments, environment, output, state, and temporary files. Redact stderr before returning errors.
- [ ] Run apply tests; manually inspect the fake-runner command transcript to confirm the selected device appears only where expected and no secret appears.
- [ ] Commit with `git add scripts/homeflix_setup/storage.py scripts/homeflix_setup/cli.py tests/test_storage_apply.py && git commit -m "Apply guarded encrypted storage plans"`.

## Task 4: Configure automatic unlock, mounting, and recovery artifacts

- [ ] Add failing tests for generating a 64-byte random root-owned key mode 0400, adding it as a second LUKS keyslot, marker-bounded idempotent `crypttab`/`fstab` entries by UUID, `nofail` mount behavior, systemd reload/start verification, and rollback of system files when verification fails.
- [ ] Add tests for a mode-0600 LUKS header backup under `/var/backups/homeflix/`, checksum generation, and output that reports paths/checksum without exposing key material or recovery passphrases.
- [ ] Run `python3 -m unittest tests.test_systemfiles tests.test_storage_apply -v`; expect failures.
- [ ] Implement `AtomicSystemFile`, `ensure_auto_unlock_key()`, `add_luks_key()`, `ensure_crypttab_entry()`, `ensure_fstab_entry()`, `backup_luks_header()`, and `verify_storage()`.
- [ ] Refuse automatic unlock when the OS filesystem is detectably unencrypted unless the user explicitly approves that weaker threat model; record only the approval boolean in setup state.
- [ ] Do not reboot automatically. Report the exact mapper/mount/systemd checks and require the later acceptance task to perform a controlled reboot.
- [ ] Run the storage/system-file suite and `python3 -m unittest discover -s tests -v`; expect green.
- [ ] Commit with `git add scripts/homeflix_setup/storage.py scripts/homeflix_setup/systemfiles.py scripts/homeflix_setup/cli.py tests/test_systemfiles.py tests/test_storage_apply.py && git commit -m "Persist and verify encrypted storage"`.

## Task 5: Integrate storage with the agent workflow

- [ ] Add an acceptance fixture proving an agent can select existing storage with no destructive command and another proving a dedicated disk cannot proceed until the exact displayed plan token is supplied.
- [ ] Add tests that the root disk, stale plan, changed `/dev/sdX` assignment with the same stable identity, changed stable identity at the same path, and missing off-host backup destination produce the documented outcomes.
- [ ] Update `docs/agent-setup.md` so agents present the plan in human terms, ask one explicit destructive confirmation, and stop rather than improvising around a refusal.
- [ ] Document recovery-passphrase separation, on-host header backup limitations, optional off-host copy, and controlled reboot verification without embedding operator-specific paths.
- [ ] Run the full test suite, Markdown link checks, and a fixture-backed dry run for both storage modes.
- [ ] Update the design page, active cursor, and log with verified scope.
- [ ] Commit with `git add docs .agent scripts tests && git commit -m "Document agent-managed encrypted storage"`.

## Validation

```bash
python3 -m unittest tests.test_storage_discovery tests.test_storage_plan tests.test_storage_apply tests.test_systemfiles -v
python3 -m unittest discover -s tests -v
scripts/homeflix --json storage discover | python3 -m json.tool
scripts/homeflix storage plan --existing /path/to/test-mount --dry-run
git diff --check
```

A destructive real-device acceptance test requires a disposable disk or loop-device harness
and explicit human approval. Fixture success alone must not be described as real-device
verification.

## Risks and rollback

- A wrong storage target destroys data. Stable identity, root/mount/signature refusals,
  short-lived plans, exact confirmation, and revalidation are mandatory and not bypassed by
  setup convenience commands.
- LUKS formatting is irreversible without a prior backup; the CLI states this before
  confirmation. Rollback applies only to generated system-file entries and unopened/new
  mappings, never restoration of overwritten data.
- Automatic unlock weakens protection if the key is stored on an unencrypted OS disk; this
  case requires separate explicit approval.
- Tool/package differences between Debian and Ubuntu are contained behind capability checks;
  unsupported versions stop with package names rather than attempting alternate commands.

## Open questions and blockers

None. Both existing mounted storage and explicitly approved dedicated-disk provisioning are
in scope; no automatic reboot is allowed.

## Links

- [Approved design](agent-first-setup.md)
- [Core setup prerequisite](agent-first-core-setup-plan.md)
- [Storage architecture](storage.md)
- [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md)
