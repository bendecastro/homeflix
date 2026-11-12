# Guarded encrypted storage

Why: an agent should be able to validate existing storage, or provision one explicitly approved dedicated disk or partition as LUKS2 plus ext4 with automatic unlock, persistent mounting and a recovery header backup, without risking the wrong disk.
Done when: `storage discover`, `storage plan` and `storage apply` work as specified in [implementation-plan.md](implementation-plan.md), proven with loop-device tests before any real-disk test, and `docs/agent-setup.md` documents the confirmation and recovery flow.

The task-by-task plan (approved design, status Proposed when written) is
[implementation-plan.md](implementation-plan.md).

## Steps

- [ ] Task 1: discover storage targets safely.
- [ ] Task 2: plan existing and encrypted storage.
- [ ] Task 3: apply guarded encrypted storage plans.
- [ ] Task 4: persist and verify encrypted storage.
- [ ] Task 5: agent-facing acceptance and documentation.
- [ ] Loop-device tests before any real-disk test.

## Outcome

<Filled when the item is finished or dropped.>
