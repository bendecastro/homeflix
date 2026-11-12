# Seerr migration

Why: Jellyseerr 2.7.3 is archived and lacks fixes for a high-severity path-traversal advisory and four medium data-leak advisories; Seerr is its maintained successor ([ADR-0012](../../../docs/decisions/adr-0012-seerr-request-portal.md)).
Done when: the repo ships the `seerr` service, the setup CLI configures and verifies it, all tests pass, docs and wiki say `seerr`, and the owner's deployment runs Seerr with its migrated requests, users and settings intact.

The full migration plan, with exact commands, validation, risks and rollback, is in
[migration-plan.md](migration-plan.md). Repository steps 1–6 are done; fixture validation
does not establish live Seerr API compatibility.

## Steps

- [x] Repository steps 1–6 (image, CLI, ownership, tests, docs, wiki).
- [ ] Answer where LAN DNS for `*.homeflix` is served for the owner (router, Pi-hole or hosts); a wildcard record makes step 10 a no-op.
- [ ] 7. Stop the old container (flushes the SQLite WAL).
- [ ] 8. Archive the old config directory and confirm `settings.json` and `db/db.sqlite3` are in the archive.
- [ ] 9. Rename the config directory to `seerr` and give it to UID/GID 1000.
- [ ] 10. Add `seerr.homeflix` to LAN DNS wherever `jellyseerr.homeflix` is served.
- [ ] 11. In the deployment checkout, back up `.env` and the override, pull the change, and rename `JELLYSEERR_TAG` and the override block.
- [ ] 12. Pull and start `seerr`, watch logs until its migration finishes, then remove the old container.
- [ ] 13. `scripts/homeflix verify core`; log in via Jellyfin; confirm existing requests, users, Radarr/Sonarr connections and Discover → Networks.

## Outcome

<Filled when the item is finished or dropped.>
