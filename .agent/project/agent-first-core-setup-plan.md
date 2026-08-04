# Agent-first Core Setup Implementation Plan

Status: In progress
Updated: 2026-08-04

**Goal:** Let an agent take a clone on a local or SSH-accessible Debian/Ubuntu host with an existing mounted data filesystem to a fully initialized, verified Homeflix core without browser wizards.

**Architecture:** A Python 3 standard-library package behind `scripts/homeflix` provides idempotent, JSON-capable primitives; the coding agent remains responsible for host-specific judgment and sequencing. Ignored `.env`, override, and checkpoint files isolate deployment state from the public repository, while live checks remain authoritative.

**Tech stack:** Python 3 standard library, `unittest`, Docker Compose v2, Bash compatibility wrapper, HTTP JSON APIs.

**Execution note:** Work task-by-task. Use TDD where behavior changes. Verify and commit each task independently.

---

## File map

### Create

- `scripts/homeflix` — executable CLI launcher.
- `scripts/homeflix_setup/__init__.py` — package metadata.
- `scripts/homeflix_setup/cli.py` — argument parsing, JSON/text rendering, exit codes.
- `scripts/homeflix_setup/command.py` — injectable subprocess runner and redacted results.
- `scripts/homeflix_setup/state.py` — non-secret checkpoint model and atomic persistence.
- `scripts/homeflix_setup/discover.py` — supported-host and runtime discovery.
- `scripts/homeflix_setup/host.py` — guarded Debian/Ubuntu Docker and Compose preparation.
- `scripts/homeflix_setup/envfile.py` — comment-preserving `.env` reads and atomic updates.
- `scripts/homeflix_setup/compose.py` — Compose project, override, deployment, and readiness helpers.
- `scripts/homeflix_setup/preflight.py` — phase-aware path, ownership, hardlink, and Docker checks.
- `scripts/homeflix_setup/secrets.py` — generated credential management without value output.
- `scripts/homeflix_setup/api/__init__.py` — service API package.
- `scripts/homeflix_setup/api/client.py` — retrying JSON HTTP client with redacted errors.
- `scripts/homeflix_setup/api/jellyfin.py` — startup, authentication, and library operations.
- `scripts/homeflix_setup/api/arr.py` — Radarr/Sonarr discovery and configuration.
- `scripts/homeflix_setup/api/jellyseerr.py` — Jellyfin and *arr connection initialization.
- `scripts/homeflix_setup/core.py` — idempotent core orchestration and verification.
- `docs/agent-setup.md` — agent-facing setup contract and recovery guidance.
- `tests/__init__.py`, `tests/helpers.py` — test package and command/HTTP fixtures.
- `tests/test_cli.py`, `tests/test_state.py`, `tests/test_discover.py`, `tests/test_host.py`, `tests/test_envfile.py`, `tests/test_preflight.py`, `tests/test_compose.py`, `tests/test_core.py` — unit and orchestration tests.
- `tests/test_api_jellyfin.py`, `tests/test_api_arr.py`, `tests/test_api_jellyseerr.py` — API contract fixture tests.
- `tests/test_docs.py`, `tests/test_core_acceptance.py` — documentation and full core-flow fixtures.
- `tests/fixtures/` — redacted API responses and discovery command outputs.

### Modify

- `.gitignore` — ignore `.homeflix/` and `docker-compose.override.yml`.
- `.env.example` — add stable `COMPOSE_PROJECT_NAME` plus generated-service credential keys with empty values.
- `docker-compose.yml` — make data binds fail closed with `bind.create_host_path: false`.
- `scripts/preflight.sh` — preserve the manual entry point while delegating to `scripts/homeflix preflight`.
- `AGENTS.md` — route setup intent to the agent setup guide and state outcome-level boundaries.
- `README.md` — make agent-assisted setup the recommended path and retain manual setup.
- `docs/quickstart.md`, `docs/configuration.md` — document core-first setup, generated local state, and CLI commands.
- `.agent/project/agent-first-setup.md`, `.agent/tasks/active.md`, `.agent/log.md` — maintain execution state.

## Task 1: Establish the CLI, state model, and test harness

- [x] Add failing tests proving `scripts/homeflix --json status` emits one JSON object, reports schema version `1`, and does not create state during a read-only status call.
- [x] Run `python3 -m unittest tests.test_cli tests.test_state -v`; expect failures because the launcher and package do not exist.
- [x] Implement `CommandRunner.run(argv, *, input_text=None, check=False, redact=())`, `SetupState.load(path)`, `SetupState.save(path)`, and `build_parser()` with `status` and global `--json`.
- [x] Persist state atomically through a same-directory temporary file, reject unknown future schema versions, and store no environment values or command output.
- [x] Make `scripts/homeflix` resolve its package relative to itself so it works from any current directory.
- [x] Run `python3 -m unittest tests.test_cli tests.test_state -v`; expect all tests green.
- [x] Commit with `git add scripts/homeflix scripts/homeflix_setup tests/__init__.py tests/helpers.py tests/test_cli.py tests/test_state.py && git commit -m "Add setup CLI foundation"`.

## Task 2: Add Debian/Ubuntu host discovery

- [x] Add fixture-backed failing tests for Debian and Ubuntu covering `/etc/os-release`, Docker/Compose presence, UID/GID, timezone, memory, CPU architecture, `/dev/dri/renderD*`, listening ports, mount source/filesystem/free space, host DNS, and Docker DNS when the daemon is reachable.
- [x] Add refusal tests for unsupported distributions and tests showing absent Docker is reported as an actionable capability gap rather than a parser crash.
- [x] Run `python3 -m unittest tests.test_discover -v`; expect failures because `discover.py` is absent.
- [x] Implement `HostFacts`, `MountFact`, `GraphicsFact`, `discover_host(runner)`, and the `discover` CLI command. Text output may summarize private facts; JSON output must be structured for an agent and must never be persisted automatically.
- [x] Detect SSH only as execution context metadata; keep remote transport outside the CLI so any coding agent can use its native SSH tooling.
- [x] Run `python3 -m unittest tests.test_discover -v` and `scripts/homeflix --json discover | python3 -m json.tool`; expect green tests and valid JSON on the development host.
- [x] Commit with `git add scripts/homeflix_setup/discover.py scripts/homeflix_setup/cli.py tests/test_discover.py tests/fixtures && git commit -m "Add setup host discovery"`.

## Task 3: Prepare Docker safely on supported hosts

- [x] Add failing command-runner tests for Debian and Ubuntu with Docker absent, Docker present but Compose absent, a stopped daemon, missing sudo, and a user not yet in the `docker` group.
- [x] Run `python3 -m unittest tests.test_host -v`; expect failures because `host.py` does not exist.
- [x] Implement `HostPreparationPlan`, `plan_host_preparation(facts)`, and `apply_host_preparation(plan, runner)`. Use Docker's signed Debian/Ubuntu apt repository and Compose plugin packages; do not pipe a network script into a shell.
- [x] Require `host prepare --apply` for package/service/group mutations; the default command prints the exact repository, packages, service, and group changes. Re-discover OS/repository identity before applying.
- [x] Start/enable Docker, add the deployment user to the Docker group when needed, and report whether an SSH reconnect/new login is required. Continue verification through `sudo docker` when authorized rather than pretending current-session group membership changed.
- [x] Run `python3 -m unittest tests.test_host tests.test_discover -v`; expect green. Run `scripts/homeflix --json host prepare --dry-run | python3 -m json.tool` on a Docker-present host and verify it plans no package changes.
- [x] Commit with `git add scripts/homeflix_setup/host.py scripts/homeflix_setup/cli.py tests/test_host.py tests/fixtures && git commit -m "Prepare supported Docker hosts"`.

## Task 4: Generate secure configuration and host overrides

- [ ] Add failing tests proving `.env.example` comments and ordering survive updates, values containing spaces are quoted safely, unknown existing keys survive, writes are mode 0600 and atomic, and rendered/logged results contain key names but not secret values.
- [ ] Add failing tests for `configure` requiring `DATA_ROOT`, `CONFIG_ROOT`, and `CACHE_ROOT`, deriving PUID/PGID/timezone, setting `COMPOSE_PROJECT_NAME=homeflix`, generating a Jellyfin password with `secrets.token_urlsafe`, and refusing paths under a missing mount.
- [ ] Add override tests: QuickSync adds `/dev/dri:/dev/dri`; unresolved LAN DNS adds direct Jellyseerr/Radarr/Sonarr setup ports; rerunning produces byte-identical YAML.
- [ ] Run `python3 -m unittest tests.test_envfile tests.test_compose -v`; expect failures.
- [ ] Implement `EnvDocument`, `update_env(path, updates, secret_keys)`, `ensure_service_credentials()`, `build_override(facts, direct_setup_ports)`, and `configure` with `--data-root`, `--config-root`, `--cache-root`, `--quality-profile`, and `--direct-setup-ports`. Use `JELLYFIN_ADMIN_USER` and `JELLYFIN_ADMIN_PASSWORD` as the canonical generated keys.
- [ ] Implement `secrets reveal jellyfin` as an explicit controlling-terminal-only credential retrieval command; refuse pipes and JSON output so setup never exposes the generated password incidentally.
- [ ] Add `.homeflix/` and `docker-compose.override.yml` to `.gitignore`; add non-secret defaults and empty generated credential keys to `.env.example`.
- [ ] Run the two test modules, `docker compose --env-file .env.example config --quiet`, and `git check-ignore .env .homeflix/setup.json docker-compose.override.yml`; expect all green/ignored.
- [ ] Commit with `git add .gitignore .env.example scripts/homeflix_setup/envfile.py scripts/homeflix_setup/secrets.py scripts/homeflix_setup/compose.py scripts/homeflix_setup/cli.py tests/test_envfile.py tests/test_compose.py && git commit -m "Generate secure host configuration"`.

## Task 5: Make preflight phase-aware and harden storage binds

- [ ] Add failing tests for `preflight --phase core` passing with empty VPN keys, `--phase acquisition` failing with the same file, hardlink inode verification, PUID/PGID mismatch, unsupported filesystems, absent mounts, and `--json` result counts.
- [ ] Add a Compose assertion test that every `${DATA_ROOT}` bind has `bind.create_host_path: false` and every *arr service receives one `${DATA_ROOT}:/data` mount rather than split media/download mounts.
- [ ] Run `python3 -m unittest tests.test_preflight tests.test_compose -v`; expect failures.
- [ ] Implement `CheckResult`, `PreflightReport`, `run_preflight(config, phase, runner)`, and CLI exit behavior. A core report warns on empty VPN values; acquisition reports them as failures.
- [ ] Convert data bind entries to long syntax with `create_host_path: false` without changing container paths or access modes.
- [ ] Replace `scripts/preflight.sh` internals with an `exec` compatibility call to `scripts/homeflix preflight "$@"`.
- [ ] Run tests, `bash -n scripts/preflight.sh`, and `docker compose --env-file .env.example config --quiet`; expect all green.
- [ ] Commit with `git add docker-compose.yml scripts/preflight.sh scripts/homeflix_setup/preflight.py scripts/homeflix_setup/cli.py tests/test_preflight.py tests/test_compose.py && git commit -m "Add phase-aware fail-closed preflight"`.

## Task 6: Deploy and reconcile the core containers

- [ ] Add failing orchestration tests proving `deploy core` invokes only `traefik jellyfin jellyseerr radarr sonarr`, never Gluetun/download/indexer services, waits on explicit HTTP/container readiness conditions, and is a no-op when the desired healthy services already run.
- [ ] Add failure tests ensuring partial startup returns per-service diagnostics with credentials and private IPs redacted.
- [ ] Run `python3 -m unittest tests.test_core tests.test_compose -v`; expect failures.
- [ ] Implement `CORE_SERVICES`, `compose_up(services)`, `compose_ps()`, `wait_for_http()`, `wait_for_container()`, and `deploy_core()`. Resolve the repository directory from the launcher, not the caller's current directory.
- [ ] Record `core_containers_started` only after live reconciliation succeeds.
- [ ] Run the unit tests and a dry-run command that prints the exact Compose invocation without changing containers.
- [ ] Commit with `git add scripts/homeflix_setup/compose.py scripts/homeflix_setup/core.py scripts/homeflix_setup/cli.py tests/test_core.py tests/test_compose.py && git commit -m "Deploy resumable core services"`.

## Task 7: Configure Jellyfin, Radarr, Sonarr, and Jellyseerr through APIs

- [ ] Add redacted fixture tests for Jellyfin's startup-state check, administrator creation, authentication, and idempotent Movies/Shows/Music virtual-folder creation.
- [ ] Add Radarr/Sonarr tests for reading API keys from their config XML, discovering quality profiles by name rather than numeric ID, creating `/data/media/movies` and `/data/media/tv` roots, enabling rename/hardlinks/completed handling, and accepting an already-equivalent configuration.
- [ ] Add Jellyseerr tests for Jellyfin authentication, plain-HTTP Docker service addresses, Radarr/Sonarr connection tests, default non-4K profile/root selection, season folders, sync/search flags, initialization, and rerun reconciliation.
- [ ] Run `python3 -m unittest tests.test_api_jellyfin tests.test_api_arr tests.test_api_jellyseerr -v`; expect failures.
- [ ] Implement `JsonClient` with bounded retry/backoff and redacted `ApiError`; implement `JellyfinClient`, `ArrClient`, and `JellyseerrClient` with the tested operations.
- [ ] Implement `configure_core()` using credentials from `.env`, runtime-discovered API keys/profiles, internal addresses `http://jellyfin:8096`, `radarr:7878`, and `sonarr:8989`, and the selected quality profile name.
- [ ] Never return or log administrator passwords, tokens, or API keys; return only configured object names and booleans.
- [ ] Run all API fixture tests and `python3 -m unittest discover -s tests -v`; expect green.
- [ ] Commit with `git add scripts/homeflix_setup/api scripts/homeflix_setup/core.py scripts/homeflix_setup/cli.py tests/test_api_jellyfin.py tests/test_api_arr.py tests/test_api_jellyseerr.py tests/fixtures && git commit -m "Automate core service initialization"`.

## Task 8: Add live verification and interruption-safe resume

- [ ] Add failing tests for `verify core` checking Compose project identity, service health, Jellyfin initialization/libraries, Radarr/Sonarr roots and media settings, Jellyseerr initialization/default services, QuickSync mapping when selected, and acquisition-service absence.
- [ ] Add resume tests starting from every checkpoint with one live resource missing; safe resources must be repaired and accounts/root folders must not duplicate.
- [ ] Add tests proving checkpoint files containing secret-looking keys are rejected and status output never includes `.env` values.
- [ ] Run `python3 -m unittest tests.test_core tests.test_state -v`; expect failures.
- [ ] Implement `verify_core()`, `reconcile_core()`, checkpoint validation, and `setup core` as a convenience composition of configure → core preflight → deploy → API configure → verify. Keep the individual primitives public for agents that need alternate sequencing.
- [ ] Run the full unit suite and a dry-run `scripts/homeflix --json setup core --dry-run | python3 -m json.tool`; expect green and no acquisition services in the plan.
- [ ] Commit with `git add scripts/homeflix_setup scripts/homeflix tests/test_core.py tests/test_state.py && git commit -m "Verify and resume core setup"`.

## Task 9: Publish agent guidance and acceptance coverage

- [ ] Add a failing documentation check that the README contains the exact setup intents, all referenced local paths exist, AGENTS routes setup without duplicating the full guide, and the manual quickstart remains linked.
- [ ] Add a fixture acceptance test simulating a supported host with existing ext4 storage from discovery through verified core, plus cases for empty VPN credentials, no QuickSync, DNS failure/direct ports, interrupted resume, and rerun idempotence.
- [ ] Run `python3 -m unittest tests.test_docs tests.test_core_acceptance -v`; expect failures.
- [ ] Write `docs/agent-setup.md` around outcomes, decision points, secure gates, CLI capability discovery (`scripts/homeflix --help`), partial deployment, recovery, and evidence expected before completion.
- [ ] Add a short setup-intent section to `AGENTS.md`; make agent-assisted setup the first README path and retain `docs/quickstart.md` as the manual fallback. Update configuration docs for ignored local state and mode-0600 `.env`.
- [ ] Run `python3 -m unittest discover -s tests -v`, `docker compose --env-file .env.example config --quiet`, `bash -n scripts/preflight.sh`, and the Markdown link checker introduced by `tests.test_docs`; expect all green.
- [ ] Update the design page, active cursor, and log with verified scope; do not claim a real-host acceptance run unless one occurred.
- [ ] Commit with `git add AGENTS.md README.md docs .agent scripts tests && git commit -m "Document agent-first core setup"`.

## Validation

Per-task checks are mandatory. Before marking the plan done, run:

```bash
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config --quiet
bash -n scripts/preflight.sh
scripts/homeflix --json status | python3 -m json.tool
git diff --check
git ls-files .env docker-compose.override.yml .homeflix
```

The final `git ls-files` command must print nothing. A real Debian/Ubuntu host acceptance run
is required before changing documentation from “agent-assisted setup available” to
“production verified.”

## Risks and rollback

- Service APIs change across image versions: fixture tests pin expected contracts, and API
  errors stop configuration without deleting existing service data.
- Automatic configuration could overwrite user choices: clients compare current state and
  create or update only fields owned by setup; no full config-file replacement.
- Core convenience orchestration could start acquisition accidentally: the explicit
  `CORE_SERVICES` allowlist and acceptance test make that a blocking regression.
- Rollback is per commit. Existing manual Compose usage remains functional throughout; local
  `.env`, override, and service appdata are never removed automatically.

## Open questions and blockers

None. The approved defaults are Debian/Ubuntu, secure secret handoff, resumable core-first
deployment, API-driven initialization, and balanced 720p/1080p unless the user chooses
another discovered profile.

## Links

- [Approved design](agent-first-setup.md)
- [Storage follow-up](agent-first-storage-plan.md)
- [Acquisition follow-up](agent-first-acquisition-plan.md)
- [Deployment](deployment.md)
- [Storage](storage.md)
- [Acquisition stack](acquisition-stack.md)
