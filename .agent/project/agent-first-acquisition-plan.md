# Agent-first Acquisition Setup Implementation Plan

Status: Superseded by [Deep Homeflix operations](../../docs/changes/deep-homeflix-operations/prd.md) / parent #3
Updated: 2026-08-17

**Goal:** Resume a verified core deployment into a VPN-gated acquisition stack, with secure provider-secret entry, demonstrated tunnel/fail-closed behavior, and automated connections among download clients, Prowlarr, Radarr, and Sonarr.

**Architecture:** Acquisition is a separate phase whose allowlist initially contains only Gluetun. The setup CLI verifies tunnel egress and deliberately removes the tunnel interface inside Gluetun to prove fallback traffic is blocked; only after recovery and a second healthy check may it start selected download/indexer services and configure their APIs.

**Tech stack:** Python 3 standard library, `unittest`, Docker Compose v2, Gluetun, qBittorrent Web API, NZBGet JSON-RPC, Prowlarr/Radarr/Sonarr API v3.

**Execution note:** Do not execute this page as a separate plan. It is a historical record.
Its accepted safety decisions were absorbed into public issues #9–#12 and the program
fixture-acceptance handoff in #13. Items below that still describe work are
**historical / absorbed**, not an executable queue. Live request-to-library,
disposable-host, and private-production acceptance remain separate.

---

## File map

### Create

- `scripts/homeflix_setup/vpn.py` — Gluetun lifecycle, tunnel discovery, egress, and fail-closed checks.
- `scripts/homeflix_setup/acquisition.py` — selected-service orchestration and end-to-end verification.
- `scripts/homeflix_setup/api/qbittorrent.py` — authentication, password, paths, categories, and preferences.
- `scripts/homeflix_setup/api/nzbget.py` — optional Usenet client configuration and health.
- `scripts/homeflix_setup/api/prowlarr.py` — API key, app connections, indexer status.
- `tests/test_vpn.py`, `tests/test_acquisition.py` — gate and orchestration tests.
- `tests/test_api_qbittorrent.py`, `tests/test_api_nzbget.py`, `tests/test_api_prowlarr.py` — redacted fixture tests.
- `tests/fixtures/acquisition/` — redacted service API and network command fixtures.

### Modify

- `scripts/homeflix_setup/secrets.py` — controlling-terminal provider-secret entry.
- `scripts/homeflix_setup/preflight.py` — acquisition credentials and selected-client checks.
- `scripts/homeflix_setup/compose.py` — Gluetun-only and gated service allowlists.
- `scripts/homeflix_setup/api/arr.py` — download-client configuration.
- `scripts/homeflix_setup/cli.py` — `secrets vpn`, `vpn verify`, `deploy acquisition`, and `verify acquisition`.
- `.env.example` — optional generated client credentials and acquisition selection defaults.
- `docs/agent-setup.md`, `docs/quickstart.md`, `docs/configuration.md` — secure resume and provider-specific gates.
- `.agent/project/agent-first-setup.md`, `.agent/tasks/active.md`, `.agent/log.md` — execution state.

## Task 1: Add secure VPN/provider credential handoff

- [x] Add failing tests proving `secrets vpn` refuses stdin/pipes and missing `/dev/tty`, enters user/password twice where confirmation applies, atomically updates mode-0600 `.env`, and emits key names/status only.
- [x] Add tests for provider-specific required keys discovered from a small supported schema: ProtonVPN/OpenVPN uses service credentials, while unsupported providers direct the agent to Gluetun's current documentation without accepting guessed keys.
- [x] Run `python3 -m unittest tests.test_envfile tests.test_acquisition -v`; expect failures.
- [x] Implement `read_from_tty(prompt, confirm=False)`, `set_vpn_secrets(provider, vpn_type)`, and `secrets vpn`. Never accept secret values as CLI arguments or JSON input.
- [x] Extend acquisition preflight so missing keys fail acquisition but leave `status` and core operations usable.
- [x] Run targeted tests and scan captured output for fixture secret values; expect no matches.
- Historical / absorbed — Commit with `git add .env.example scripts/homeflix_setup/secrets.py scripts/homeflix_setup/preflight.py scripts/homeflix_setup/cli.py tests/test_acquisition.py tests/test_envfile.py && git commit -m "Add secure VPN credential handoff"`.

## Task 2: Start and verify Gluetun before any client

- [x] Add failing tests proving `vpn verify` runs core verification, acquisition preflight, `docker compose up -d gluetun`, built-in health polling, target/container DNS tests, and host-versus-tunnel public-IP comparison without printing either address.
- [x] Add refusal tests for equal host/tunnel egress, unresolved container DNS, unhealthy Gluetun, absent `/dev/net/tun`, and any already-running gated service before first verification.
- [x] Run `python3 -m unittest tests.test_vpn -v`; expect failures.
- [x] Implement `GluetunStatus`, `start_gluetun_only()`, `wait_gluetun_healthy()`, `resolve_in_namespace()`, and `compare_egress()` using bounded timeouts and redacted diagnostics.
- [x] Pin the Gluetun-only allowlist in code; assert in tests that it excludes qBittorrent, NZBGet, and Prowlarr.
- [x] Run VPN tests plus a dry-run JSON plan; expect only Gluetun mutation.
- Historical / absorbed — Commit with `git add scripts/homeflix_setup/vpn.py scripts/homeflix_setup/compose.py scripts/homeflix_setup/cli.py tests/test_vpn.py tests/fixtures/acquisition && git commit -m "Gate acquisition on verified VPN egress"`.

## Task 3: Prove fail-closed behavior and recover the tunnel

- Historical / absorbed — Add failing fake-runner tests for discovering the active tunnel interface from Gluetun routes, bringing only that interface down, proving a bounded HTTPS request from the same namespace fails, restoring/restarting Gluetun, and repeating health/egress checks.
- Historical / absorbed — Add safety tests refusing the loopback, Ethernet/default Docker, or unknown interface; cleanup must execute after timeout, failed probe, or interruption.
- Historical / absorbed — Run `python3 -m unittest tests.test_vpn -v`; expect failures.
- Historical / absorbed — Implement `discover_tunnel_interface()`, `set_link_state()`, `probe_external_access()`, and `verify_fail_closed()` with `try/finally`. A passing result requires: tunnel-up egress succeeds, tunnel-down egress fails, recovery becomes healthy, and recovered egress succeeds through a non-host address.
- Historical / absorbed — Store only timestamp, Gluetun image identity, and boolean evidence in setup state; never store public IPs.
- Historical / absorbed — Make verification expire when Gluetun's image/configuration changes or after 24 hours, requiring re-verification before a fresh acquisition deploy.
- Historical / absorbed — Run tests and inspect dry-run command ordering; expect cleanup/recovery after every injected failure.
- Historical / absorbed — Commit with `git add scripts/homeflix_setup/vpn.py scripts/homeflix_setup/state.py tests/test_vpn.py && git commit -m "Verify VPN fail-closed behavior"`.

## Task 4: Configure qBittorrent and optional NZBGet securely

- Historical / absorbed — Add qBittorrent fixture tests for extracting the temporary password from bounded container logs, authenticating, replacing it with a generated `.env` credential, setting `/data/torrents` paths, disabling alternate paths, creating movies/tv/music categories, and reconciling reruns.
- Historical / absorbed — Add NZBGet tests for generated UI credentials, `/data/usenet/{incomplete,complete}` paths, category destinations, and leaving provider servers disabled until securely supplied credentials are present.
- Historical / absorbed — Run `python3 -m unittest tests.test_api_qbittorrent tests.test_api_nzbget -v`; expect failures.
- Historical / absorbed — Implement `QBittorrentClient` and `NzbgetClient`; ensure temporary/default credentials are never returned in command output and generated values are written only to `.env`.
- Historical / absorbed — Start only the user-selected clients (`torrent`, `usenet`, or both) after a current VPN verification. Do not start NZBGet merely because it exists in Compose.
- Historical / absorbed — Run targeted API tests and the full secret-output scan.
- Historical / absorbed — Commit with `git add scripts/homeflix_setup/api/qbittorrent.py scripts/homeflix_setup/api/nzbget.py scripts/homeflix_setup/acquisition.py scripts/homeflix_setup/cli.py tests/test_api_qbittorrent.py tests/test_api_nzbget.py tests/fixtures/acquisition && git commit -m "Configure VPN-backed download clients"`.

## Task 5: Connect Prowlarr, Radarr, and Sonarr

- Historical / absorbed — Add Prowlarr fixture tests for reading its config API key, adding Radarr/Sonarr applications with Docker service addresses, syncing only enabled indexers, and accepting equivalent existing apps.
- Historical / absorbed — Extend Arr tests for qBittorrent at `gluetun:${QBITTORRENT_PORT}` with movies/tv categories, optional NZBGet at `gluetun:${NZBGET_PORT}`, completed-download handling, and hardlink import settings.
- Historical / absorbed — Add tests proving `localhost`, host-published ports, and a direct non-Gluetun client address are rejected for these connections.
- Historical / absorbed — Run `python3 -m unittest tests.test_api_prowlarr tests.test_api_arr -v`; expect failures.
- Historical / absorbed — Implement `ProwlarrClient`, Arr download-client reconciliation, and `configure_acquisition()`. Start Prowlarr only after the VPN gate; provider-specific indexers remain disabled until the user supplies their credentials securely.
- Historical / absorbed — Return configured service names and booleans only; redact all API keys and provider URLs containing credentials.
- Historical / absorbed — Run targeted and full tests.
- Historical / absorbed — Commit with `git add scripts/homeflix_setup/api/prowlarr.py scripts/homeflix_setup/api/arr.py scripts/homeflix_setup/acquisition.py tests/test_api_prowlarr.py tests/test_api_arr.py tests/fixtures/acquisition && git commit -m "Connect acquisition services"`.

## Task 6: Verify resumable acquisition and document remaining provider gates

- Historical / absorbed — Add acceptance tests proving acquisition cannot deploy with missing credentials, stale VPN evidence, failed tunnel recovery, or an unselected client; reruns must not duplicate categories/apps/clients.
- Historical / absorbed — Add verification for Gluetun health, network namespace sharing, selected client APIs, paths/categories, Arr clients, Prowlarr apps, and absence of unselected services.
- Historical / absorbed — Add a fixture end-to-end handoff that stops at “indexer/provider credentials required” without calling the setup complete when no usable indexer exists.
- Historical / absorbed — Implement `deploy_acquisition()`, `verify_acquisition()`, and the convenience `setup acquisition` composition.
- Historical / absorbed — Update agent/manual docs with the single secure credential command, evidence required before starting clients, optional torrent/Usenet choice, and the provider-specific indexer boundary.
- Historical / absorbed — Run the full suite, Compose validation, Markdown links, and dry-run plans for torrent-only, Usenet-only, and both.
- Historical / absorbed — Update the design page, active cursor, and log; claim request-to-playback success only after an authorized real title traverses the complete path.
- Historical / absorbed — Commit with `git add docs .agent scripts tests && git commit -m "Complete agent-led acquisition setup"`.

## Validation

```bash
python3 -m unittest tests.test_vpn tests.test_acquisition tests.test_api_qbittorrent tests.test_api_nzbget tests.test_api_prowlarr tests.test_api_arr -v
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config --quiet
scripts/homeflix --json setup acquisition --dry-run | python3 -m json.tool
git diff --check
git ls-files .env .homeflix docker-compose.override.yml
```

Real-host acceptance requires authorized VPN/provider credentials. Logs and reports must show
boolean/network-health evidence without displaying public IPs or secrets.

## Risks and rollback

- A weak VPN test could permit traffic leaks. Client startup is gated on a controlled
  tunnel-down probe and successful recovery, not merely a provider-reported IP.
- Bringing down the wrong interface could disrupt routing. Interface classification refuses
  non-tunnel devices and always restores/restarts Gluetun in `finally`.
- API automation could expose temporary passwords. Clients consume them internally and tests
  scan every output/error path for fixture secrets.
- Rollback stops the selected acquisition services; it does not delete appdata, categories,
  indexers, or downloads. Core services remain running.

## Open questions and blockers

Provider/indexer-specific credentials and selections cannot be inferred. The secure handoff
and agent conversation collect them at execution time; their absence produces a resumable
partial state rather than unsafe defaults.

## Links

- [Approved design](agent-first-setup.md)
- [Core setup prerequisite](agent-first-core-setup-plan.md)
- [Acquisition architecture](acquisition-stack.md)
- [ADR-0005](../decisions/adr-0005-arr-stack-gluetun-protonvpn.md)
- [Secrets convention](../conventions/secrets.md)
