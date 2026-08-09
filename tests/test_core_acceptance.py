from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

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
        if command[:2] == ("findmnt", "--json"):
            payload = {"filesystems": [{"target": str(self.mount), "source": "/dev/fixture", "fstype": "ext4"}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "config" in command and "--quiet" in command:
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
                discovery.commands[f"getent ahosts {service}.local"] = [2, "", "not found"]
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

            fixture = StatefulCoreFixture("")
            fixture.containers.remove("radarr")
            fixture.libraries.pop("Music")
            runner = JourneyRunner(fixture, root / "mounted")
            preflight = run_preflight(environment, "core", runner)
            self.assertTrue(preflight.passed, preflight.to_dict())
            self.assertEqual({item.status for item in preflight.results if item.name.startswith("vpn_")}, {"warn"})

            common = {
                "runner": runner,
                "transports": {"jellyfin": fixture.jellyfin, "radarr": fixture.arr("radarr"), "sonarr": fixture.arr("sonarr"), "jellyseerr": fixture.jellyseerr},
                "api_key_reader": lambda *args: fixture.key,
                "settings_key_reader": lambda *args: fixture.key,
                "http_waiter": lambda *args, **kwargs: ReadinessResult(True, "ready"),
            }
            deployed = deploy_core(
                root, runner=runner,
                preflight_runner=lambda config, phase, command_runner: run_preflight(config, phase, command_runner),
                http_waiter=common["http_waiter"],
                container_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
            )
            self.assertEqual(deployed["status"], "ready")
            initialized = configure_core(root, **common)
            self.assertEqual(initialized["status"], "configured")
            verified = verify_core(root, quicksync_inspector=lambda *args: None, **common)
            self.assertTrue(verified["passed"], verified)
            self.assertEqual(set(fixture.containers), set(CORE_SERVICES))
            self.assertEqual(set(fixture.libraries), {"Movies", "Shows", "Music"})
            self.assertEqual(fixture.creations["library"], 1)

            before = dict(fixture.creations)
            rerun = reconcile_core(
                root, preflight_runner=lambda config, phase, command_runner: run_preflight(config, phase, command_runner),
                container_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                quicksync_inspector=lambda *args: None, **common,
            )
            self.assertEqual(rerun["status"], "verified")
            self.assertEqual(fixture.creations, before)
            self.assertEqual(len(fixture.roots["radarr"]), 1)
            self.assertEqual(len(fixture.roots["sonarr"]), 1)
            self.assertEqual(len(fixture.servers["radarr"]), 1)
            self.assertEqual(len(fixture.servers["sonarr"]), 1)

            mutation_commands = [command for command in fixture.commands if "up" in command]
            self.assertEqual(len(mutation_commands), 1)
            self.assertEqual(mutation_commands[0][-5:], CORE_SERVICES)
            state = SetupState.load(root / ".homeflix" / "setup.json")
            rendered = json.dumps((configured, preflight.to_dict(), deployed, initialized, verified, rerun, {"schema_version": state.schema_version, "checkpoints": state.checkpoints, "host_facts": state.host_facts}))
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
