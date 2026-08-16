# Backup and recovery

## Requirements

- A backup artifact SHALL contain consistently snapshotted application configuration from `CONFIG_ROOT` and SHALL exclude media, `.env`, and LUKS recovery material. `(satisfied #7)`
- Discovery of any SQLite database that cannot be snapshotted consistently SHALL prevent artifact publication. `(satisfied #7)`
- Snapshot creation SHALL omit transient logs and database WAL/SHM files from the published artifact. `(satisfied #7)`
- One artifact-repository interface SHALL provide list, get, put, and prune behavior for local off-filesystem and SSH destinations. `(satisfied #8)`
- A local repository SHALL be refused when it shares the configured data filesystem. `(satisfied #7)`
- Archive names and members SHALL be validated before extraction; absolute paths, traversal, unsafe links, special files, and writes outside the scratch destination SHALL be refused. `(satisfied #7)`
- Restore SHALL refuse the live `CONFIG_ROOT`, require an empty scratch destination, and verify every restored SQLite database. `(satisfied #7)`
- Successful restore evidence SHALL require at least one valid SQLite database. `(satisfied #7)`
- Retention SHALL prune only matching Homeflix backup artifacts after a new artifact has been stored successfully. `(satisfied #8)`
- Existing backup and restore shell commands SHALL remain compatibility adapters to the canonical Homeflix interface. `(satisfied #8)`

## Key scenarios

1. One of several SQLite snapshots fails; no artifact reaches the repository and no retention pruning occurs.
2. A local off-filesystem repository and an SSH repository produce the same lifecycle result.
3. A crafted archive attempts path traversal or a symlink escape; restore refuses it before extraction.
4. A scratch restore contains one corrupt database; recovery verification fails.
5. Existing cron invokes the compatibility command after migration and reaches the canonical backup behavior.
