# Validation

Commands taken from the root `AGENTS.md` and the Seerr migration plan.

## Build / test validation

```sh
# Unit and fixture tests (fast; tests/test_docs.py also reads the record under docs/ and work/)
python3 -m unittest discover -s tests -v

# Compose interpolation and schema without creating a real .env
docker compose --env-file .env.example config --quiet

# Shell syntax only (safe; does not touch deployment paths)
bash -n scripts/preflight.sh
```

`scripts/homeflix preflight` is an integration check for a configured host (contacts Docker
and writes hardlink test files under `DATA_ROOT`); run it only when host validation is
intended. `docker compose up` changes external state and is never a routine check.

## Baseline checks

```sh
git status --short
```

## Agent guidance

- After changing files, run the most relevant available validation.
- If no project-specific tests exist yet, verify syntax/structure and summarize what you checked.
- Do not claim build/test/runtime validation was run unless it actually ran.
- Add newly discovered canonical commands here and to `../references/commands.md`.
