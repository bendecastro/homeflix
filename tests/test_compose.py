from __future__ import annotations

from dataclasses import replace
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from scripts.homeflix_setup.compose import (
    CORE_SERVICES, PROXY_SUBNET_CANDIDATES, _atomic_write, build_override, compose_inventory,
    compose_ps, compose_up, configure,
)
from scripts.homeflix_setup.discover import (
    GraphicsDeviceFact, GraphicsFact, HostFacts, LanNetworkFact, MountFact,
    ProxyNetworkFact,
)
from scripts.homeflix_setup.envfile import EnvDocument


def facts(
    *,
    mount: str = "/",
    quicksync: bool = False,
    lan_network: LanNetworkFact = LanNetworkFact("enp1s0", "192.168.1.0/24", "ok", None),
    proxy_network: ProxyNetworkFact = ProxyNetworkFact(status="absent"),
) -> HostFacts:
    return HostFacts(
        os_id="debian", os_version_id="12", os_pretty_name="Debian", supported=True,
        uid=1234, gid=2345, timezone="Europe/Lisbon", memory_bytes=1, architecture="x86_64",
        cpu_model="fixture", graphics=GraphicsFact(
            ("/dev/dri/renderD128",) if quicksync else (),
            devices=(GraphicsDeviceFact("/dev/dri/renderD128", "0x8086", "ok", True, True) ,) if quicksync else (),
        ),
        listening_ports=(), listening_ports_status="ok", listening_ports_reason=None,
        mounts=(MountFact(mount, "/dev/fixture", "ext4", 1000),), mounts_status="ok", mounts_reason=None,
        docker_present=True, docker_cli_status="ok", docker_cli_reason=None,
        compose_present=True, compose_status="ok", compose_reason=None,
        docker_daemon_reachable=True, docker_daemon_status="ok", docker_daemon_reason=None,
        host_nameservers=("192.0.2.1",), host_search_domains=(), host_dns_status="ok",
        host_dns_reason=None, ssh_context=False,
        lan_dns_domain="local", lan_dns_status="resolved", lan_network=lan_network,
        proxy_network=proxy_network,
    )


class ComposeOverrideTests(unittest.TestCase):
    def assert_valid_override(self, override: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yml").write_text(
                "services:\n  jellyfin:\n    image: busybox\n  jellyseerr:\n    image: busybox\n  radarr:\n    image: busybox\n  sonarr:\n    image: busybox\n",
                encoding="utf-8",
            )
            (root / "override.yml").write_text(override, encoding="utf-8")
            result = subprocess.run(
                ["docker", "compose", "-f", str(root / "compose.yml"), "-f", str(root / "override.yml"), "config", "--quiet"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_quicksync_and_direct_ports_are_bounded_and_deterministic(self) -> None:
        first = build_override(facts(quicksync=True), True)
        second = build_override(facts(quicksync=True), True)
        self.assertEqual(first, second)
        self.assertIn("/dev/dri:/dev/dri", first)
        for port in (5055, 7878, 8989):
            self.assertIn(f'"{port}:{port}"', first)
        self.assertNotIn("password", first.casefold())
        self.assert_valid_override(first)

    def test_no_adaptations_is_valid_minimal_yaml(self) -> None:
        override = build_override(facts(), False)
        self.assertEqual(override, "services: {}\n")
        self.assert_valid_override(override)

    def test_usable_nonstandard_intel_render_node_selects_stable_dri_mapping(self) -> None:
        device = GraphicsDeviceFact("/dev/dri/renderD129", "0x8086", "ok", True, True)
        host = replace(facts(), graphics=GraphicsFact((device.path,), devices=(device,)))
        self.assertIn("/dev/dri:/dev/dri", build_override(host, False))

    def test_non_intel_unknown_or_inaccessible_graphics_do_not_map_dri(self) -> None:
        cases = (
            GraphicsDeviceFact("/dev/dri/renderD128", "0x1002", "ok", True, True),
            GraphicsDeviceFact("/dev/dri/renderD128", None, "error", True, True),
            GraphicsDeviceFact("/dev/dri/renderD128", "0x8086", "ok", True, False),
        )
        for device in cases:
            with self.subTest(device=device):
                host = replace(facts(), graphics=GraphicsFact((device.path,), devices=(device,)))
                self.assertNotIn("/dev/dri", build_override(host, False))

    def test_unresolved_or_error_lan_dns_enables_direct_setup_ports(self) -> None:
        for status in ("unresolved", "error"):
            with self.subTest(status=status):
                override = build_override(replace(facts(), lan_dns_status=status), False)
                for port in (5055, 7878, 8989):
                    self.assertIn(f'"{port}:{port}"', override)
                self.assert_valid_override(override)

    def test_configure_derives_host_values_generates_secret_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = Path(__file__).resolve().parents[1] / ".env.example"
            (root / ".env.example").write_bytes(example.read_bytes())
            data = root / "storage" / "data"
            config = root / "config"
            cache = root / "cache"
            for path in (data, config, cache):
                path.mkdir(parents=True)
            host = facts(mount=str(root))
            with patch("scripts.homeflix_setup.secrets.secrets.token_urlsafe", return_value="fixture-secret") as token:
                result = configure(root, host, data_root=str(data), config_root=str(config), cache_root=str(cache), quality_profile="Balanced HD", direct_setup_ports=True)
                first_env = (root / ".env").read_bytes()
                first_override = (root / "docker-compose.override.yml").read_bytes()
                rerun = configure(root, host, data_root=str(data), config_root=str(config), cache_root=str(cache), quality_profile="Balanced HD", direct_setup_ports=True)
            document = EnvDocument.load(root / ".env")
            self.assertEqual(document.get("PUID"), "1234")
            self.assertEqual(document.get("PGID"), "2345")
            self.assertEqual(document.get("TZ"), "Europe/Lisbon")
            self.assertEqual(document.get("COMPOSE_PROJECT_NAME"), "homeflix")
            self.assertEqual(document.get("QUALITY_PROFILE"), "Balanced HD")
            self.assertEqual(token.call_count, 1)
            self.assertEqual((root / ".env").read_bytes(), first_env)
            self.assertEqual((root / "docker-compose.override.yml").read_bytes(), first_override)
            self.assertEqual((root / "docker-compose.override.yml").stat().st_mode & 0o777, 0o644)
            self.assertNotIn("fixture-secret", repr(result))
            self.assertNotIn("fixture-secret", repr(rerun))

    def test_override_atomic_setup_failure_closes_fd_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.yml"
            path.write_text("services: {}\n", encoding="utf-8")
            with mock.patch("scripts.homeflix_setup.compose.os.fchmod", side_effect=OSError("mode failure")), mock.patch(
                "scripts.homeflix_setup.compose.os.close", wraps=os.close
            ) as close:
                with self.assertRaisesRegex(OSError, "mode failure"):
                    _atomic_write(path, "services:\n", 0o644)
            close.assert_called_once()
            self.assertEqual(path.read_text(encoding="utf-8"), "services: {}\n")
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_configure_refuses_nonexistent_or_unverified_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("DATA_ROOT=\n", encoding="utf-8")
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(ValueError, "does not exist"):
                configure(root, facts(mount=str(root)), data_root=str(root / "missing"), config_root=str(existing), cache_root=str(existing))
            self.assertFalse((root / ".env").exists())
            outside_mount = root / "mount"
            outside_mount.mkdir()
            with self.assertRaisesRegex(ValueError, "not under"):
                configure(root, facts(mount=str(outside_mount)), data_root=str(existing), config_root=str(existing), cache_root=str(existing))
            self.assertFalse((root / ".env").exists())


class ComposeExecutionTests(unittest.TestCase):
    class Runner:
        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode
            self.commands: list[tuple[str, ...]] = []
            self.timeouts: list[float | None] = []

        def run(self, argv, **kwargs):
            command = tuple(argv)
            self.commands.append(command)
            self.timeouts.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(command, self.returncode, self.stdout, "secret raw error")

    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / ".env").write_text("COMPOSE_PROJECT_NAME=homeflix\n", encoding="utf-8")
        return temporary, root

    def test_compose_up_has_explicit_context_project_and_services(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = self.Runner()
        compose_up(root, CORE_SERVICES, runner)
        self.assertEqual(runner.commands, [(
            "docker", "compose", "--project-directory", str(root),
            "--env-file", str(root / ".env"), "--project-name", "homeflix",
            "up", "--detach", "--no-deps", *CORE_SERVICES,
        )])
        rendered = " ".join(runner.commands[0])
        self.assertIn("--no-deps", rendered)
        for forbidden_dependency in ("gluetun", "qbittorrent", "prowlarr", "nzbget"):
            self.assertNotIn(forbidden_dependency, rendered)

    def test_compose_ps_accepts_array_and_json_lines_and_rejects_malformed(self) -> None:
        records = [
            {"Service": "traefik", "State": "running", "Health": "healthy"},
            {"Service": "radarr", "State": "exited", "Health": ""},
        ]
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        for stdout in (json.dumps(records), "\n".join(json.dumps(item) for item in records)):
            with self.subTest(stdout=stdout):
                states = compose_ps(root, self.Runner(stdout))
                self.assertEqual(states["traefik"], {"state": "running", "health": "healthy"})
                self.assertEqual(states["radarr"]["state"], "exited")
        malformed_records = (
            "not-json",
            json.dumps({"State": "running"}),
            json.dumps([1]),
            json.dumps({"Service": "   ", "State": "running"}),
            json.dumps({"Service": "radarr", "State": "   "}),
            json.dumps({"Service": "radarr", "State": "mystery"}),
            json.dumps({"Service": "radarr", "State": "running", "Health": "   "}),
            json.dumps({"Service": "radarr", "State": "running", "Health": "mystery"}),
            json.dumps({"Service": "radarré", "State": "running"}),
            json.dumps({"Service": "radarrK", "State": "running"}),
            json.dumps({"Service": "ſonarr", "State": "running"}),
            json.dumps({"Service": ".radarr", "State": "running"}),
            json.dumps({"Service": "-radarr", "State": "running"}),
            json.dumps({"Service": "radarr service", "State": "running"}),
            json.dumps([
                {"Service": "radarr", "State": "running"},
                {"Service": " radarr ", "State": "running"},
            ]),
        )
        for malformed in malformed_records:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                compose_ps(root, self.Runner(malformed))

    def test_compose_inventory_requires_exact_raw_project_and_service_identity(self) -> None:
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        valid = {"Service": "radarr", "State": "running", "Health": "healthy", "Project": "homeflix"}
        self.assertEqual(compose_inventory(root, self.Runner(json.dumps([valid])), project_name="homeflix")[0]["service"], "radarr")
        for field, value in (("Service", "Radarr"), ("Service", " radarr"), ("Project", "Homeflix"), ("Project", "homeflix ")):
            with self.subTest(field=field, value=value):
                record = dict(valid); record[field] = value
                with self.assertRaises(ValueError):
                    compose_inventory(root, self.Runner(json.dumps([record])), project_name="homeflix")
        with self.assertRaises(ValueError):
            compose_inventory(root, self.Runner(json.dumps([valid, valid])), project_name="homeflix")

    def test_compose_ps_uses_callers_remaining_timeout(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = self.Runner("[]")
        compose_ps(root, runner, timeout=1.25)
        self.assertEqual(runner.timeouts, [1.25])

    def test_project_name_uses_exact_ascii_compose_grammar_before_commands(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        valid_runner = self.Runner()
        compose_up(root, CORE_SERVICES, valid_runner, project_name=" homeflix ")
        self.assertEqual(len(valid_runner.commands), 1)
        self.assertIn("homeflix", valid_runner.commands[0])
        self.assertNotIn(" homeflix ", valid_runner.commands[0])

        for invalid in ("Homeflix", "homéflix", "-homeflix", "_homeflix"):
            with self.subTest(project_name=invalid):
                runner = self.Runner()
                with self.assertRaises(ValueError):
                    compose_up(root, CORE_SERVICES, runner, project_name=invalid)
                self.assertEqual(runner.commands, [])

    def test_compose_up_rejects_non_core_service(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(ValueError):
            compose_up(root, ("gluetun",), self.Runner())


class StorageBindTests(unittest.TestCase):
    def test_data_binds_fail_closed_and_arr_services_share_one_root(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(repository_root / ".env.example"), "config", "--format", "json"],
            cwd=repository_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        document = EnvDocument.load(repository_root / ".env.example")
        data_root = document.get("DATA_ROOT")
        self.assertIsNotNone(data_root)
        data_binds = [
            volume
            for service in rendered["services"].values()
            for volume in service.get("volumes", [])
            if volume.get("type") == "bind"
            and (volume.get("source") == data_root or volume.get("source", "").startswith(data_root + "/"))
        ]
        self.assertTrue(data_binds)
        for volume in data_binds:
            self.assertIs(volume.get("bind", {}).get("create_host_path"), False, volume)
        for service_name in ("radarr", "sonarr", "lidarr"):
            volumes = rendered["services"][service_name]["volumes"]
            roots = [volume for volume in volumes if volume.get("source") == data_root]
            self.assertEqual(len(roots), 1, service_name)
            self.assertEqual(roots[0]["target"], "/data")
            self.assertFalse(any(volume.get("target") in {"/data/media", "/data/torrents", "/data/usenet"} for volume in volumes))


class VpnFirewallTests(unittest.TestCase):
    # ProtonVPN's WireGuard gateway. NAT-PMP is negotiated with it over the tunnel, so any
    # allowed-subnet entry covering it diverts those packets to the LAN interface instead.
    VPN_GATEWAYS = (ipaddress.ip_address("10.2.0.1"),)

    def test_allowed_subnets_do_not_cover_the_vpn_gateway(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(repository_root / ".env.example"), "config", "--format", "json"],
            cwd=repository_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        allowed = rendered["services"]["gluetun"]["environment"]["FIREWALL_OUTBOUND_SUBNETS"]
        networks = [ipaddress.ip_network(entry.strip()) for entry in allowed.split(",") if entry.strip()]
        self.assertTrue(networks)
        for network in networks:
            for gateway in self.VPN_GATEWAYS:
                self.assertNotIn(
                    gateway,
                    network,
                    f"{network} covers VPN gateway {gateway}; NAT-PMP port forwarding would fail silently",
                )

    def test_lan_subnet_has_no_compose_fallback(self) -> None:
        compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("${LAN_SUBNET:-", compose)
        self.assertIn("${LAN_SUBNET:?", compose)
        self.assertNotIn("${PROXY_SUBNET:-", compose)
        self.assertIn("${PROXY_SUBNET:?", compose)

    def test_proxy_network_is_pinned_to_the_allowlisted_subnet(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(repository_root / ".env.example"), "config", "--format", "json"],
            cwd=repository_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        pinned = rendered["networks"]["traefik-network"]["ipam"]["config"]
        self.assertEqual(len(pinned), 1, pinned)
        proxy_subnet = ipaddress.ip_network(pinned[0]["subnet"])
        allowed = rendered["services"]["gluetun"]["environment"]["FIREWALL_OUTBOUND_SUBNETS"]
        networks = [ipaddress.ip_network(entry.strip()) for entry in allowed.split(",") if entry.strip()]
        self.assertIn(proxy_subnet, networks, "Traefik could not reach the services behind Gluetun")


class SubnetSelectionTests(unittest.TestCase):
    def configured(
        self,
        lan_network: LanNetworkFact,
        proxy_network: ProxyNetworkFact = ProxyNetworkFact(status="absent"),
    ) -> EnvDocument:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        example = Path(__file__).resolve().parents[1] / ".env.example"
        (root / ".env.example").write_bytes(example.read_bytes())
        data = root / "storage" / "data"
        config = root / "config"
        cache = root / "cache"
        for path in (data, config, cache):
            path.mkdir(parents=True)
        configure(
            root,
            facts(
                mount=str(root),
                lan_network=lan_network,
                proxy_network=proxy_network,
            ),
            data_root=str(data),
            config_root=str(config),
            cache_root=str(cache),
        )
        return EnvDocument.load(root / ".env")

    def test_configure_writes_discovered_lan_and_a_non_overlapping_proxy_subnet(self) -> None:
        document = self.configured(LanNetworkFact("enp1s0", "192.168.1.0/24", "ok", None))
        self.assertEqual(document.get("LAN_SUBNET"), "192.168.1.0/24")
        proxy = ipaddress.ip_network(document.get("PROXY_SUBNET"))
        self.assertFalse(proxy.overlaps(ipaddress.ip_network("192.168.1.0/24")))

    def test_proxy_subnet_avoids_a_lan_that_claims_the_first_candidate(self) -> None:
        document = self.configured(LanNetworkFact("enp1s0", PROXY_SUBNET_CANDIDATES[0], "ok", None))
        self.assertEqual(document.get("LAN_SUBNET"), PROXY_SUBNET_CANDIDATES[0])
        self.assertEqual(document.get("PROXY_SUBNET"), PROXY_SUBNET_CANDIDATES[1])

    def test_proxy_subnet_avoids_other_routed_host_networks(self) -> None:
        document = self.configured(LanNetworkFact(
            "enp1s0",
            "192.168.1.0/24",
            "ok",
            None,
            (PROXY_SUBNET_CANDIDATES[0],),
        ))

        self.assertEqual(document.get("PROXY_SUBNET"), PROXY_SUBNET_CANDIDATES[1])

    def test_existing_homeflix_proxy_subnet_is_preserved_on_rerun(self) -> None:
        proxy_cidr = PROXY_SUBNET_CANDIDATES[0]
        document = self.configured(
            LanNetworkFact(
                "enp1s0",
                "192.168.1.0/24",
                "ok",
                None,
                (proxy_cidr,),
            ),
            ProxyNetworkFact(proxy_cidr, "ok"),
        )

        self.assertEqual(document.get("PROXY_SUBNET"), proxy_cidr)

    def test_existing_proxy_refuses_foreign_more_specific_route(self) -> None:
        proxy_cidr = PROXY_SUBNET_CANDIDATES[0]
        with self.assertRaises(ValueError) as error:
            self.configured(
                LanNetworkFact(
                    "enp1s0",
                    "192.168.1.0/24",
                    "ok",
                    None,
                    (proxy_cidr, "172.30.0.128/25"),
                ),
                ProxyNetworkFact(proxy_cidr, "ok"),
            )

        self.assertIn("overlaps another host route", str(error.exception))

    def test_configure_refuses_when_proxy_network_ownership_is_unknown(self) -> None:
        with self.assertRaises(ValueError) as error:
            self.configured(
                LanNetworkFact("enp1s0", "192.168.1.0/24", "ok", None),
                ProxyNetworkFact(status="unknown", reason="Docker daemon is not reachable"),
            )

        self.assertIn("PROXY_SUBNET cannot be verified", str(error.exception))

    def test_configure_refuses_when_existing_proxy_network_cannot_be_inspected(self) -> None:
        with self.assertRaises(ValueError) as error:
            self.configured(
                LanNetworkFact("enp1s0", "192.168.1.0/24", "ok", None),
                ProxyNetworkFact(status="error", reason="inspect failed"),
            )

        self.assertIn("PROXY_SUBNET cannot be verified", str(error.exception))

    def test_configure_refuses_when_lan_discovery_did_not_resolve(self) -> None:
        with self.assertRaises(ValueError) as error:
            self.configured(LanNetworkFact("enp1s0", None, "unresolved", "no unambiguous IPv4 default route was found"))
        self.assertIn("LAN_SUBNET cannot be determined", str(error.exception))

    def test_configure_refuses_public_lan_bypass_even_if_facts_claim_success(self) -> None:
        with self.assertRaises(ValueError) as error:
            self.configured(LanNetworkFact("enp2s0", "93.184.216.0/24", "ok", None))

        self.assertIn("local-use address space", str(error.exception))

    def test_configure_refuses_a_lan_that_contains_the_vpn_gateway(self) -> None:
        with self.assertRaises(ValueError) as error:
            self.configured(LanNetworkFact("enp1s0", "10.0.0.0/8", "ok", None))
        self.assertIn("contains VPN gateway", str(error.exception))


if __name__ == "__main__":
    unittest.main()
