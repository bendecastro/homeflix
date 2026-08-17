from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.homeflix_setup.api import ApiError
from scripts.homeflix_setup.compose import CORE_SERVICES, configure
from scripts.homeflix_setup.core import ReadinessResult, configure_core, deploy_core, reconcile_core, verify_core
from scripts.homeflix_setup.discover import GraphicsFact, MountFact, discover_host
from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.preflight import run_preflight
from scripts.homeflix_setup.state import SetupState
from tests.test_core import StatefulCoreFixture
from tests.test_discover import FixtureRunner


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SENTINELS = ("MEMORY_ONLY_TOKEN", "FIXTURE_API_KEY_1234567890ABCDE", "192.0.2.53")


class JourneyRunner:
    """Stateful command fake: real primitives interpret its Docker/mount responses."""

    def __init__(self, fixture: StatefulCoreFixture, mount: Path) -> None:
        self.fixture = fixture
        self.mount = mount

    def run(self, argv, **kwargs):
        command = tuple(argv)
        if command == ("docker", "--version"):
            return subprocess.CompletedProcess(command, 0, "Docker fixture\n", "")
        if command == ("docker", "compose", "version"):
            return subprocess.CompletedProcess(command, 0, "Docker Compose fixture\n", "")
        if command == ("docker", "info"):
            return subprocess.CompletedProcess(command, 0, "fixture\n", "")
        if command == (
            "findmnt", "--json", "--target", str(self.mount / "data"),
            "--output", "TARGET,SOURCE,FSTYPE",
        ):
            payload = {"filesystems": [{"target": str(self.mount), "source": "/dev/fixture", "fstype": "ext4"}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command == (
            "docker", "compose", "--project-directory", str(self.mount.parent),
            "--env-file", str(self.mount.parent / ".env"), "config", "--quiet",
        ):
            return subprocess.CompletedProcess(command, 0, "", "")
        return self.fixture.run(argv, **kwargs)


class CoreFixtureAcceptanceTests(unittest.TestCase):
    def test_existing_ext4_core_journey_is_safe_resumable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(ROOT / ".env.example", root / ".env.example")
            shutil.copy2(ROOT / "docker-compose.yml", root / "docker-compose.yml")
            data = root / "mounted" / "data"
            config_root = root / "mounted" / "config"
            cache = root / "mounted" / "cache"
            for path in (data / "torrents", data / "media", config_root, cache):
                path.mkdir(parents=True, exist_ok=True)

            discovery = FixtureRunner("discovery-debian.json")
            discovery.commands["find /dev/dri -maxdepth 1 -name renderD* -type c -print"] = [0, "", ""]
            for service in ("jellyseerr", "radarr", "sonarr"):
                discovery.commands[f"getent ahosts {service}.homeflix"] = [2, "", "not found"]
            facts = discover_host(discovery)
            facts = replace(
                facts,
                uid=os.getuid(), gid=os.getgid(), graphics=GraphicsFact(),
                mounts=(MountFact(str(root / "mounted"), "/dev/fixture", "ext4", 10**12),),
            )
            self.assertTrue(facts.supported)
            self.assertEqual(facts.os_id, "debian")
            self.assertEqual(facts.lan_dns_status, "unresolved")
            self.assertFalse(facts.graphics.quicksync_usable)

            configured = configure(
                root, facts, data_root=str(data), config_root=str(config_root), cache_root=str(cache),
                quality_profile="Fixture HD",
            )
            environment = EnvDocument.load(root / ".env")
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual(environment.get("VPN_USER"), "")
            self.assertEqual(environment.get("VPN_PASSWORD"), "")
            self.assertFalse(configured["override"]["adaptations"]["quicksync"])
            self.assertTrue(configured["override"]["adaptations"]["direct_setup_ports"])
            override = (root / "docker-compose.override.yml").read_text(encoding="utf-8")
            for port in (5055, 7878, 8989):
                self.assertIn(f'"{port}:{port}"', override)
            self.assertNotIn("/dev/dri", override)

            fixture = StatefulCoreFixture("", clean=True)
            self.assertEqual(fixture.containers, set())
            self.assertFalse(fixture.startup)
            self.assertFalse(fixture.admin)
            self.assertEqual(fixture.libraries, {})
            self.assertEqual(fixture.roots, {"radarr": [], "sonarr": []})
            self.assertEqual(fixture.media_ok, {"radarr": False, "sonarr": False})
            self.assertEqual(fixture.completed_ok, {"radarr": False, "sonarr": False})
            self.assertFalse(fixture.initialized)
            self.assertEqual(fixture.servers, {"radarr": [], "sonarr": []})
            runner = JourneyRunner(fixture, root / "mounted")
            preflight = run_preflight(environment, "core", runner)
            self.assertTrue(preflight.passed, preflight.to_dict())
            vpn = {item.name: item.status for item in preflight.results if item.name.startswith("vpn_")}
            self.assertEqual(vpn["vpn_wireguard_private_key"], "warn")
            self.assertEqual(vpn.get("vpn_provider", "pass"), "pass")

            def http_waiter(url, *, headers=None, timeout=0):
                host = (headers or {}).get("Host", "").split(".", 1)[0]
                service = host or (
                    "traefik" if ":8080/" in url else
                    "jellyfin" if ":8096/" in url else "unknown"
                )
                ready = timeout > 0 and service in fixture.containers
                return ReadinessResult(ready, "ready" if ready else "fixture service unavailable")

            def container_waiter(service, probe, *, timeout=0):
                observed = probe(timeout).get(service, {}) if timeout > 0 else {}
                ready = observed.get("state") == "running" and observed.get("health") in {"", "healthy"}
                return ReadinessResult(ready, "ready" if ready else "fixture container unavailable")

            def quicksync_inspector(repository_root, command_runner, project_name):
                selected = "/dev/dri" in (Path(repository_root) / "docker-compose.override.yml").read_text(encoding="utf-8")
                return False if selected else None

            common = {
                "runner": runner,
                "transports": {"jellyfin": fixture.jellyfin, "radarr": fixture.arr("radarr"), "sonarr": fixture.arr("sonarr"), "jellyseerr": fixture.jellyseerr},
                "api_key_reader": lambda *args: fixture.key,
                "settings_key_reader": lambda *args: fixture.key,
                "http_waiter": http_waiter,
            }
            deployed = deploy_core(
                root, runner=runner,
                preflight_runner=lambda config, phase, command_runner: run_preflight(config, phase, command_runner),
                http_waiter=common["http_waiter"],
                container_waiter=container_waiter,
            )
            self.assertEqual(deployed["status"], "ready")
            fixture.fail_next_sonarr_root = True
            with self.assertRaises(ApiError) as interrupted:
                configure_core(root, **common)
            self.assertEqual(interrupted.exception.service, "sonarr")
            self.assertEqual(interrupted.exception.code, "transport_error")
            self.assertTrue(fixture.startup)
            self.assertTrue(fixture.admin)
            self.assertEqual(set(fixture.libraries), {"Movies", "Shows", "Music"})
            self.assertEqual(fixture.roots, {"radarr": ["/data/media/movies"], "sonarr": []})
            self.assertEqual(fixture.creations, {"account": 1, "library": 3, "library_options": 3, "root": 1, "settings": 1, "server": 0, "application_key": 1, "notification": 2})
            self.assertEqual(fixture.media_ok, {"radarr": True, "sonarr": False})
            self.assertEqual(fixture.completed_ok, {"radarr": True, "sonarr": False})
            self.assertFalse(fixture.initialized)

            resumed = reconcile_core(
                root, preflight_runner=lambda config, phase, command_runner: run_preflight(config, phase, command_runner),
                container_waiter=container_waiter,
                quicksync_inspector=quicksync_inspector, **common,
            )
            self.assertEqual(resumed["status"], "verified")
            self.assertFalse(resumed["configure"]["radarr"]["targeted_connection_changed"])
            self.assertFalse(resumed["configure"]["radarr"]["refresh_connection_changed"])
            self.assertTrue(resumed["configure"]["sonarr"]["targeted_connection_changed"])
            self.assertTrue(resumed["configure"]["sonarr"]["refresh_connection_changed"])
            radarr_check = next(item for item in resumed["verify"]["checks"] if item["domain"] == "radarr")
            sonarr_check = next(item for item in resumed["verify"]["checks"] if item["domain"] == "sonarr")
            self.assertEqual(radarr_check["status"], "pass")
            self.assertEqual(sonarr_check["status"], "pass")
            self.assertIn("Jellyfin discovery", radarr_check["reason"])
            self.assertEqual(set(fixture.containers), set(CORE_SERVICES))
            self.assertTrue(fixture.startup)
            self.assertTrue(fixture.admin)
            self.assertTrue(fixture.initialized)
            self.assertTrue(fixture.jellyfin_connected)
            self.assertEqual(fixture.media_ok, {"radarr": True, "sonarr": True})
            self.assertEqual(fixture.completed_ok, {"radarr": True, "sonarr": True})
            self.assertEqual(fixture.creations, {"account": 1, "library": 3, "library_options": 3, "root": 2, "settings": 2, "server": 2, "application_key": 1, "notification": 4})
            self.assertEqual(len(fixture.libraries), 3)
            self.assertEqual(fixture.roots, {"radarr": ["/data/media/movies"], "sonarr": ["/data/media/tv"]})
            self.assertEqual(fixture.updates["media"], {"radarr": 1, "sonarr": 1})
            self.assertEqual(fixture.updates["completed"], {"radarr": 1, "sonarr": 1})
            self.assertEqual(fixture.updates["server"], {"radarr": 0, "sonarr": 0})
            self.assertEqual(len(fixture.servers["radarr"]), 1)
            self.assertEqual(len(fixture.servers["sonarr"]), 1)
            self.assertTrue(fixture.servers["radarr"][0]["isDefault"])
            self.assertTrue(fixture.servers["sonarr"][0]["isDefault"])
            self.assertEqual(len(fixture.application_keys), 1)
            self.assertEqual(len(fixture.notifications["radarr"]), 2)
            self.assertEqual(len(fixture.notifications["sonarr"]), 2)
            self.assertEqual(
                {item["implementation"] for item in fixture.notifications["radarr"]},
                {"MediaBrowser", "Webhook"},
            )
            self.assertEqual(
                {item["implementation"] for item in fixture.notifications["sonarr"]},
                {"MediaBrowser", "Webhook"},
            )

            before = (dict(fixture.creations), {kind: dict(values) for kind, values in fixture.updates.items()})
            configuration_mutations_before_rerun = tuple(fixture.configuration_mutations)
            api_calls_before_rerun = len(fixture.api_calls)
            rerun = reconcile_core(
                root, preflight_runner=lambda config, phase, command_runner: run_preflight(config, phase, command_runner),
                container_waiter=container_waiter,
                quicksync_inspector=quicksync_inspector, **common,
            )
            self.assertEqual(rerun["status"], "verified")
            self.assertEqual((fixture.creations, fixture.updates), before)
            self.assertEqual(tuple(fixture.configuration_mutations), configuration_mutations_before_rerun)
            rerun_calls = fixture.api_calls[api_calls_before_rerun:]
            expected_rerun_calls = [
                ("jellyfin", "GET", "/System/Info/Public"),
                ("jellyfin", "POST", "/Users/AuthenticateByName"),
                ("jellyfin", "GET", "/Library/VirtualFolders"),
                ("jellyfin", "GET", "/Library/VirtualFolders"),
                ("jellyfin", "GET", "/Auth/Keys"),
                ("jellyfin", "POST", "/Sessions/Logout"),
            ]
            for service in ("radarr", "sonarr"):
                expected_rerun_calls.extend((
                    (service, "GET", "/api/v3/qualityprofile"),
                    (service, "GET", "/api/v3/rootfolder"),
                    (service, "GET", "/api/v3/config/naming"),
                    (service, "GET", "/api/v3/config/mediamanagement"),
                    (service, "GET", "/api/v3/config/downloadclient"),
                    (service, "GET", "/api/v3/notification"),
                ))
            expected_rerun_calls.extend((
                ("jellyseerr", "GET", "/api/v1/settings/public"),
                ("jellyseerr", "GET", "/api/v1/settings/jellyfin"),
                ("jellyseerr", "POST", "/api/v1/settings/radarr/test"),
                ("jellyseerr", "GET", "/api/v1/settings/radarr"),
                ("jellyseerr", "POST", "/api/v1/settings/sonarr/test"),
                ("jellyseerr", "GET", "/api/v1/settings/sonarr"),
                ("jellyseerr", "GET", "/api/v1/settings/public"),
                ("jellyfin", "GET", "/System/Info/Public"),
                ("jellyfin", "POST", "/Users/AuthenticateByName"),
                ("jellyfin", "GET", "/Library/VirtualFolders"),
                ("jellyfin", "POST", "/Sessions/Logout"),
            ))
            for service in ("radarr", "sonarr"):
                expected_rerun_calls.extend((
                    (service, "GET", "/api/v3/qualityprofile"),
                    (service, "GET", "/api/v3/rootfolder"),
                    (service, "GET", "/api/v3/config/naming"),
                    (service, "GET", "/api/v3/config/mediamanagement"),
                    (service, "GET", "/api/v3/config/downloadclient"),
                    (service, "GET", "/api/v3/notification"),
                ))
            expected_rerun_calls.extend((
                ("jellyseerr", "GET", "/api/v1/settings/public"),
                ("jellyseerr", "GET", "/api/v1/settings/jellyfin"),
                ("jellyseerr", "GET", "/api/v1/settings/radarr"),
                ("jellyseerr", "GET", "/api/v1/settings/sonarr"),
            ))
            self.assertEqual(rerun_calls, expected_rerun_calls)
            self.assertEqual(len(fixture.roots["radarr"]), 1)
            self.assertEqual(len(fixture.roots["sonarr"]), 1)
            self.assertEqual(len(fixture.servers["radarr"]), 1)
            self.assertEqual(len(fixture.servers["sonarr"]), 1)
            self.assertEqual(len(fixture.notifications["radarr"]), 2)
            self.assertEqual(len(fixture.notifications["sonarr"]), 2)

            mutation_commands = [command for command in fixture.commands if "up" in command]
            self.assertEqual(len(mutation_commands), 1)
            self.assertEqual(mutation_commands[0][-5:], CORE_SERVICES)
            state = SetupState.load(root / ".homeflix" / "setup.json")
            rendered = json.dumps((configured, preflight.to_dict(), deployed, resumed, rerun, {"schema_version": state.schema_version, "checkpoints": state.checkpoints, "host_facts": state.host_facts}))
            for sentinel in PRIVATE_SENTINELS:
                self.assertNotIn(sentinel, rendered)
            for acquisition in ("gluetun", "qbittorrent", "nzbget", "prowlarr", "lidarr", "bazarr"):
                self.assertNotIn(acquisition, " ".join(" ".join(command) for command in mutation_commands))

        tracked = subprocess.run(
            ["git", "ls-files", ".env", "docker-compose.override.yml", ".homeflix"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertEqual(tracked.stdout, "")


if __name__ == "__main__":
    unittest.main()
