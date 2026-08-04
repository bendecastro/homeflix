# Homeflix agent guidance

Homeflix is a public, self-hosted media stack. Runtime artifacts live at the repository
root; `.agent/` is the maintained project wiki and the source of truth for architecture,
decisions, current work, and operational gotchas.

## Start here

Before non-trivial work, read in this order:

1. [`.agent/index.md`](.agent/index.md) — project status and catalog
2. [`.agent/AGENTS.md`](.agent/AGENTS.md) — wiki maintenance rules and session protocol
3. [`.agent/map.md`](.agent/map.md) — choose the smallest task-specific context set
4. [`.agent/tasks/active.md`](.agent/tasks/active.md) and any linked plan

For a narrow task, do not read the whole wiki; use the context map.

## Project constraints

- Keep `.agent/` current in the same change when work alters project state, establishes a
  durable fact, changes a service, or makes an architectural decision. Follow the update
  triggers in `.agent/AGENTS.md`.
- Check existing ADRs before changing architecture. Do not silently reverse accepted
  decisions.
- Never commit secrets, real `.env` values, operator-private infrastructure details, or
  machine-specific absolute paths. Portable configuration belongs in environment variables.
- Preserve the single `${DATA_ROOT}:/data` mount for the *arr services. Splitting downloads
  and media into separate container mounts breaks hardlink imports even when both host paths
  are on one filesystem.
- Do not describe the stack as deployed or production-verified unless that has been freshly
  established; the checked-in Compose configuration has been validated but not run against
  real services.

## Validation

Use the smallest checks relevant to the change:

```bash
# Compose interpolation and schema without creating a real .env
docker compose --env-file .env.example config --quiet

# Shell syntax only (safe; does not touch deployment paths)
bash -n scripts/preflight.sh
```

`./scripts/preflight.sh` is an integration check for a configured host: it reads `.env`,
contacts Docker, and creates then removes hardlink test files under `DATA_ROOT`. Run it only
when host validation is intended. Starting the stack (`docker compose up`) changes external
state and must not be treated as a routine repository check.
