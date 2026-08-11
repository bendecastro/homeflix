from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.discover import discover_host
from tests.helpers import parse_single_json


FIXTURES = Path(__file__).with_name("fixtures")


class FixtureRunner:
    def __init__(self, fixture_name: str) -> None:
        payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
        self.commands = payload["commands"]
        self.environment = payload["environment"]
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def run(self, argv: list[str] | tuple[str, ...], *, timeout: float | None = None, **_: object) -> subprocess.CompletedProcess[str]:
        key = " ".join(argv)
        self.calls.append((tuple(argv), timeout))
        return_code, stdout, stderr = self.commands[key]
        return subprocess.CompletedProcess(list(argv), return_code, stdout, stderr)


class TimeoutRunner(FixtureRunner):
    def __init__(self, fixture_name: str, timed_out_command: tuple[str, ...]) -> None:
        super().__init__(fixture_name)
        self.timed_out_command = timed_out_command

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        timeout: float | None = None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if tuple(argv) == self.timed_out_command:
            raise subprocess.TimeoutExpired(list(argv), timeout, output="poison output")
        return super().run(argv, timeout=timeout, **kwargs)


class HostDiscoveryTests(unittest.TestCase):
    def test_debian_discovers_supported_host_and_private_runtime_facts(self) -> None:
        runner = FixtureRunner("discovery-debian.json")

        facts = discover_host(runner)
        result = facts.to_dict()

        self.assertEqual(result["os"], {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)", "codename": "bookworm", "supported": True})
        self.assertEqual(result["identity"], {"uid": 1000, "gid": 1001, "user": "homeflix", "groups": ["homeflix", "sudo", "docker"], "groups_status": "ok", "session_groups": ["homeflix", "sudo", "docker"], "session_groups_status": "ok", "privilege_escalation": "sudo_noninteractive"})
        self.assertEqual(result["timezone"], "Europe/London")
        self.assertEqual(result["memory_bytes"], 16_777_216_000)
        self.assertEqual(result["cpu"], {"architecture": "x86_64", "model": "Fixture Intel CPU"})
        self.assertEqual(
            result["graphics"],
            {
                "status": "ok",
                "render_devices": ["/dev/dri/renderD128"],
                "available": True,
                "quicksync_usable": True,
                "devices": [{
                    "path": "/dev/dri/renderD128",
                    "vendor": "0x8086",
                    "vendor_status": "ok",
                    "readable": True,
                    "writable": True,
                    "quicksync_usable": True,
                }],
            },
        )
        self.assertEqual(result["listening_ports"], {"status": "ok", "ports": [80, 8096]})
        self.assertEqual(
            result["mounts"]["items"][1],
            {
                "target": "/srv/media",
                "source": "/dev/mapper/media",
                "filesystem": "ext4",
                "free_bytes": 1099511627776,
            },
        )
        self.assertEqual(
            result["docker"],
            {
                "present": True,
                "cli_status": "ok",
                "compose_present": True,
                "compose_status": "ok",
                "daemon_reachable": True,
                "daemon_status": "ok",
                "service_enabled": True,
                "service_status": "enabled",
                "conflicting_packages": [],
                "conflicting_packages_status": "ok",
            },
        )
        self.assertEqual(
            result["host_dns"],
            {
                "status": "ok",
                "nameservers": ["192.0.2.53"],
                "search": ["lan.example"],
            },
        )
        self.assertEqual(
            result["lan_dns"],
            {
                "domain": "local",
                "status": "resolved",
                "services": [
                    {"hostname": "jellyseerr.local", "status": "resolved"},
                    {"hostname": "radarr.local", "status": "resolved"},
                    {"hostname": "sonarr.local", "status": "resolved"},
                ],
            },
        )
        self.assertEqual(result["docker_dns"]["status"], "not_tested")
        self.assertIn("non-mutating", result["docker_dns"]["reason"])
        self.assertEqual(result["execution_context"], {"ssh": True})
        self.assertIn(
            (
                "findmnt",
                "--list",
                "--json",
                "--bytes",
                "--output",
                "TARGET,SOURCE,FSTYPE,AVAIL",
            ),
            [argv for argv, _ in runner.calls],
        )
        self.assertTrue(all(timeout is not None and timeout > 0 for _, timeout in runner.calls))
        self.assertEqual(result["probe_errors"], {})

    def test_ubuntu_without_docker_reports_actionable_capability_gaps(self) -> None:
        facts = discover_host(FixtureRunner("discovery-ubuntu.json")).to_dict()

        self.assertTrue(facts["os"]["supported"])
        self.assertEqual(facts["cpu"]["architecture"], "aarch64")
        self.assertEqual(
            facts["graphics"], {"status": "ok", "render_devices": [], "available": False, "quicksync_usable": False, "devices": []}
        )
        self.assertEqual(facts["listening_ports"], {"status": "ok", "ports": [53]})
        self.assertFalse(facts["docker"]["present"])
        self.assertEqual(facts["docker"]["cli_status"], "missing")
        self.assertFalse(facts["docker"]["compose_present"])
        self.assertEqual(facts["docker"]["compose_status"], "missing")
        self.assertFalse(facts["docker"]["daemon_reachable"])
        self.assertEqual(facts["docker"]["daemon_status"], "missing")
        self.assertIsNone(facts["docker"]["service_enabled"])
        self.assertEqual(facts["docker"]["service_status"], "not_found")
        self.assertEqual(facts["docker_dns"], {"status": "not_tested", "reason": "Docker daemon is not reachable"})
        gap_codes = {gap["code"] for gap in facts["capability_gaps"]}
        self.assertEqual(gap_codes, {"docker_missing", "compose_missing"})
        self.assertIn("Install Docker", facts["capability_gaps"][0]["action"])

    def test_docker_service_probe_distinguishes_disabled_not_found_and_error(self) -> None:
        cases = (
            ([1, "disabled\n", ""], False, "disabled"),
            ([4, "not-found\n", "private unit detail"], None, "not_found"),
            ([1, "unexpected\n", "private systemd failure"], None, "error"),
            ([124, "enabled\n", "probe timed out"], None, "error"),
        )
        for response, enabled, status in cases:
            with self.subTest(status=status, response=response):
                runner = FixtureRunner("discovery-debian.json")
                runner.commands["systemctl is-enabled docker"] = response
                docker = discover_host(runner).to_dict()["docker"]
                self.assertIs(docker["service_enabled"], enabled)
                self.assertEqual(docker["service_status"], status)
                if status in {"not_found", "error"}:
                    self.assertIn("service_reason", docker)
                    self.assertNotIn("private", json.dumps(docker))

    def test_conflicting_package_probe_is_allowlisted_and_structured(self) -> None:
        runner = FixtureRunner("discovery-ubuntu.json")
        runner.commands["dpkg-query --show --showformat=${binary:Package}\\t${db:Status-Abbrev}\\n"] = [
            0,
            "docker.io\tii \nprivate-package\tii \nrunc\tii \ndocker-doc\trc \n",
            "",
        ]
        docker = discover_host(runner).to_dict()["docker"]
        self.assertEqual(docker["conflicting_packages"], ["docker.io", "runc"])
        self.assertEqual(docker["conflicting_packages_status"], "ok")
        self.assertNotIn("private-package", json.dumps(docker))

    def test_conflicting_package_probe_error_does_not_claim_empty(self) -> None:
        runner = FixtureRunner("discovery-ubuntu.json")
        runner.commands["dpkg-query --show --showformat=${binary:Package}\\t${db:Status-Abbrev}\\n"] = [1, "docker.io\tii \n", "private failure"]
        docker = discover_host(runner).to_dict()["docker"]
        self.assertIsNone(docker["conflicting_packages"])
        self.assertEqual(docker["conflicting_packages_status"], "error")
        self.assertNotIn("private", json.dumps(docker))

    def test_group_probe_failures_are_explicit_unknown_not_empty_success(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["id -nG homeflix"] = [1, "", "private configured groups"]
        runner.commands["id -nG"] = [124, "", "private session groups"]
        identity = discover_host(runner).to_dict()["identity"]
        self.assertEqual(identity["groups"], [])
        self.assertEqual(identity["groups_status"], "error")
        self.assertEqual(identity["session_groups"], [])
        self.assertEqual(identity["session_groups_status"], "error")

    def test_unsupported_distribution_is_structured_refusal(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["cat /etc/os-release"] = [0, "ID=fedora\nVERSION_ID=41\nPRETTY_NAME=Fedora\n", ""]

        facts = discover_host(runner).to_dict()

        self.assertFalse(facts["os"]["supported"])
        self.assertEqual(facts["refusal"]["code"], "unsupported_distribution")
        self.assertIn("Debian and Ubuntu", facts["refusal"]["action"])

    def test_amd_unknown_and_inaccessible_render_devices_are_not_quicksync_usable(self) -> None:
        cases = (
            ([0, "0x1002\n", ""], [0, "", ""], [0, "", ""]),
            ([1, "", "private sysfs failure"], [0, "", ""], [0, "", ""]),
            ([0, "0x8086\n", ""], [0, "", ""], [1, "", "permission denied"]),
        )
        for vendor, readable, writable in cases:
            with self.subTest(vendor=vendor, writable=writable):
                runner = FixtureRunner("discovery-debian.json")
                runner.commands["cat /sys/class/drm/renderD128/device/vendor"] = vendor
                runner.commands["test -r /dev/dri/renderD128"] = readable
                runner.commands["test -w /dev/dri/renderD128"] = writable
                graphics = discover_host(runner).to_dict()["graphics"]
                self.assertFalse(graphics["quicksync_usable"])
                self.assertFalse(graphics["devices"][0]["quicksync_usable"])

    def test_service_dns_unresolved_despite_healthy_resolver_is_structured(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["getent ahosts jellyseerr.local"] = [2, "", ""]
        runner.commands["getent ahosts radarr.local"] = [0, "192.0.2.22 STREAM radarr.local\n", ""]
        runner.commands["getent ahosts sonarr.local"] = [2, "", ""]
        result = discover_host(runner).to_dict()
        self.assertEqual(result["host_dns"]["status"], "ok")
        self.assertEqual(result["lan_dns"]["status"], "unresolved")
        self.assertEqual(
            [item["status"] for item in result["lan_dns"]["services"]],
            ["unresolved", "resolved", "unresolved"],
        )

    def test_service_dns_probe_error_is_distinct_from_unresolved(self) -> None:
        runner = TimeoutRunner("discovery-debian.json", ("getent", "ahosts", "sonarr.example.test"))
        runner.commands["getent ahosts jellyseerr.example.test"] = [0, "192.0.2.21 STREAM jellyseerr.example.test\n", ""]
        runner.commands["getent ahosts radarr.example.test"] = [0, "192.0.2.22 STREAM radarr.example.test\n", ""]
        facts = discover_host(runner, domain="example.test").to_dict()
        self.assertEqual(facts["lan_dns"]["domain"], "example.test")
        self.assertEqual(facts["lan_dns"]["status"], "error")
        self.assertEqual(facts["lan_dns"]["services"][2]["status"], "error")

    def test_failed_graphics_probe_is_not_reported_as_confirmed_absence(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["find /dev/dri -maxdepth 1 -name renderD* -type c -print"] = [
            127,
            "",
            "find: command not found\n",
        ]

        graphics = discover_host(runner).to_dict()["graphics"]

        self.assertEqual(graphics["status"], "error")
        self.assertIsNone(graphics["available"])
        self.assertEqual(graphics["render_devices"], [])
        self.assertEqual(graphics["reason"], "graphics probe command is unavailable")

    def test_failed_listening_port_probe_has_structured_error(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["ss -H -lntu"] = [124, "", "probe timed out"]

        ports = discover_host(runner).to_dict()["listening_ports"]

        self.assertEqual(
            ports,
            {"status": "error", "ports": [], "reason": "listening-port probe timed out"},
        )

    def test_failed_and_malformed_mount_probes_have_structured_errors(self) -> None:
        cases = (
            ([127, "", "findmnt missing"], "mount probe command is unavailable"),
            ([0, "not-json", ""], "mount probe returned invalid data"),
        )
        for response, reason in cases:
            with self.subTest(reason=reason):
                runner = FixtureRunner("discovery-debian.json")
                runner.commands[
                    "findmnt --list --json --bytes --output TARGET,SOURCE,FSTYPE,AVAIL"
                ] = response

                mounts = discover_host(runner).to_dict()["mounts"]

                self.assertEqual(mounts, {"status": "error", "items": [], "reason": reason})

    def test_existing_homeflix_proxy_network_is_discovered_for_idempotent_setup(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands[
            "docker network inspect homeflix_traefik-network --format {{json .IPAM.Config}}"
        ] = [0, '[{"Subnet":"172.30.0.0/24","Gateway":"172.30.0.1"}]\n', ""]

        proxy_network = discover_host(runner).proxy_network

        self.assertEqual(proxy_network.status, "ok")
        self.assertEqual(proxy_network.cidr, "172.30.0.0/24")

    def test_lan_network_is_derived_from_the_default_route_interface(self) -> None:
        runner = FixtureRunner("discovery-debian.json")

        lan_network = discover_host(runner).to_dict()["lan_network"]

        self.assertEqual(
            lan_network, {"interface": "enp1s0", "cidr": "192.168.1.0/24", "status": "ok"}
        )

    def test_lan_network_records_non_default_routed_cidrs_for_proxy_selection(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["ip -j -4 route show table all"] = [
            0,
            json.dumps([
                {"dst": "default", "gateway": "192.168.1.1", "dev": "enp1s0"},
                {"dst": "192.168.1.0/24", "dev": "enp1s0"},
                {"dst": "172.30.0.0/24", "dev": "br-existing"},
                {"dst": "unreachable", "dev": "ignored"},
            ]),
            "",
        ]

        lan_network = discover_host(runner).lan_network

        self.assertEqual(
            lan_network.routed_cidrs, ("172.30.0.0/24", "192.168.1.0/24")
        )

    def test_lan_network_uses_effective_route_and_preferred_source(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["ip -j -4 route show default"] = [
            0,
            json.dumps([
                {"dst": "default", "dev": "tailscale0", "prefsrc": "100.64.0.5", "metric": 100},
                {"dst": "default", "gateway": "192.168.50.1", "dev": "enp2s0", "prefsrc": "192.168.50.23", "metric": 10},
            ]),
            "",
        ]
        runner.commands["ip -j -4 addr show dev enp2s0"] = [
            0,
            json.dumps([{
                "ifname": "enp2s0",
                "addr_info": [
                    {"family": "inet", "local": "169.254.10.4", "prefixlen": 16},
                    {"family": "inet", "local": "192.168.50.23", "prefixlen": 24},
                ],
            }]),
            "",
        ]

        lan_network = discover_host(runner).to_dict()["lan_network"]

        self.assertEqual(
            lan_network, {"interface": "enp2s0", "cidr": "192.168.50.0/24", "status": "ok"}
        )

    def test_lan_network_uses_gateway_when_default_route_has_no_preferred_source(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["ip -j -4 route show default"] = [
            0,
            json.dumps([{
                "dst": "default",
                "gateway": "192.168.50.1",
                "dev": "enp2s0",
                "metric": 10,
            }]),
            "",
        ]
        runner.commands["ip -j -4 addr show dev enp2s0"] = [
            0,
            json.dumps([{
                "ifname": "enp2s0",
                "addr_info": [
                    {"family": "inet", "local": "192.168.60.23", "prefixlen": 24},
                    {"family": "inet", "local": "192.168.50.23", "prefixlen": 24},
                ],
            }]),
            "",
        ]

        lan_network = discover_host(runner).to_dict()["lan_network"]

        self.assertEqual(lan_network["cidr"], "192.168.50.0/24")

    def test_lan_network_rejects_public_prefixes_from_the_vpn_bypass(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["ip -j -4 route show default"] = [
            0,
            json.dumps([{
                "dst": "default",
                "gateway": "93.184.216.1",
                "dev": "enp2s0",
                "prefsrc": "93.184.216.23",
                "metric": 10,
            }]),
            "",
        ]
        runner.commands["ip -j -4 addr show dev enp2s0"] = [
            0,
            json.dumps([{
                "ifname": "enp2s0",
                "addr_info": [{
                    "family": "inet",
                    "local": "93.184.216.23",
                    "prefixlen": 24,
                    "scope": "global",
                }],
            }]),
            "",
        ]

        lan_network = discover_host(runner).to_dict()["lan_network"]

        self.assertEqual(lan_network["status"], "unresolved")
        self.assertIsNone(lan_network["cidr"])
        self.assertIn("no allowed local IPv4 subnet", lan_network["reason"])

    def test_lan_network_probe_failures_are_structured_and_never_guess(self) -> None:
        addresses = json.dumps([{
            "ifname": "enp1s0",
            "addr_info": [{"family": "inet", "local": "100.64.0.5", "prefixlen": 32}],
        }])
        cases = (
            ({"ip -j -4 route show default": [1, "", "private route detail"]},
             "error", "default-route probe exited with status 1"),
            ({"ip -j -4 route show default": [0, "[]", ""]},
             "unresolved", "no IPv4 default route was found"),
            ({"ip -j -4 addr show dev enp1s0": [0, addresses, ""]},
             "unresolved", "no allowed local IPv4 subnet is configured on enp1s0"),
            ({"ip -j -4 route show table all": [1, "", "private route inventory"]},
             "error", "route-inventory probe exited with status 1"),
        )
        for overrides, status, reason in cases:
            with self.subTest(status=status, reason=reason):
                runner = FixtureRunner("discovery-debian.json")
                runner.commands.update(overrides)

                lan_network = discover_host(runner).to_dict()["lan_network"]

                self.assertEqual(lan_network["status"], status)
                self.assertEqual(lan_network["reason"], reason)
                self.assertIsNone(lan_network["cidr"])
                self.assertNotIn("private route detail", json.dumps(lan_network))

    def test_failed_dns_probe_has_structured_error_without_leaking_stderr(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["cat /etc/resolv.conf"] = [1, "", "private resolver detail"]

        dns = discover_host(runner).to_dict()["host_dns"]

        self.assertEqual(dns["status"], "error")
        self.assertEqual(dns["nameservers"], [])
        self.assertEqual(dns["search"], [])
        self.assertEqual(dns["reason"], "host-DNS probe exited with status 1")
        self.assertNotIn("private", json.dumps(dns))

    def test_successful_empty_probes_are_distinct_from_probe_errors(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["find /dev/dri -maxdepth 1 -name renderD* -type c -print"] = [0, "", ""]
        runner.commands["ss -H -lntu"] = [0, "", ""]
        runner.commands["findmnt --list --json --bytes --output TARGET,SOURCE,FSTYPE,AVAIL"] = [
            0,
            '{"filesystems": []}\n',
            "",
        ]
        runner.commands["cat /etc/resolv.conf"] = [0, "", ""]

        result = discover_host(runner).to_dict()

        self.assertEqual(
            result["graphics"], {"status": "ok", "render_devices": [], "available": False, "quicksync_usable": False, "devices": []}
        )
        self.assertEqual(result["listening_ports"], {"status": "ok", "ports": []})
        self.assertEqual(result["mounts"], {"status": "ok", "items": []})
        self.assertEqual(
            result["host_dns"], {"status": "ok", "nameservers": [], "search": []}
        )

    def test_text_cli_summarizes_probe_errors_without_traceback(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["ss -H -lntu"] = [127, "", "ss missing"]
        facts = discover_host(runner)
        with mock.patch("scripts.homeflix_setup.cli.discover_host", return_value=facts):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(("discover",), repository_root=Path("."))

        self.assertEqual(return_code, 0, stderr.getvalue())
        self.assertIn("listening ports: unavailable", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_docker_present_without_compose_plugin_reports_installable_missing_gap(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["docker compose version"] = [
            1,
            "",
            "docker: 'compose' is not a docker command.\nSee 'docker --help'\n",
        ]

        result = discover_host(runner).to_dict()

        self.assertTrue(result["docker"]["present"])
        self.assertFalse(result["docker"]["compose_present"])
        self.assertEqual(result["docker"]["compose_status"], "missing")
        self.assertEqual(
            result["docker"]["compose_reason"], "Docker Compose plugin is not available"
        )
        gaps = {gap["code"]: gap for gap in result["capability_gaps"]}
        self.assertIn("compose_missing", gaps)
        self.assertIn("Install", gaps["compose_missing"]["action"])
        self.assertNotIn("compose_probe_error", gaps)

    def test_unrelated_compose_exit_one_and_timeout_remain_probe_errors(self) -> None:
        runners = (
            FixtureRunner("discovery-debian.json"),
            TimeoutRunner(
                "discovery-debian.json", ("docker", "compose", "version")
            ),
        )
        runners[0].commands["docker compose version"] = [
            1,
            "",
            "private unrelated execution failure",
        ]

        for runner in runners:
            with self.subTest(runner=type(runner).__name__):
                result = discover_host(runner).to_dict()
                self.assertIsNone(result["docker"]["compose_present"])
                self.assertEqual(result["docker"]["compose_status"], "error")
                gap_codes = {gap["code"] for gap in result["capability_gaps"]}
                self.assertIn("compose_probe_error", gap_codes)
                self.assertNotIn("compose_missing", gap_codes)
                self.assertNotIn("private", json.dumps(result))
                self.assertNotIn("poison", json.dumps(result))

    def test_docker_timeout_is_retryable_error_not_missing_install_gap(self) -> None:
        runner = TimeoutRunner("discovery-debian.json", ("docker", "--version"))

        result = discover_host(runner).to_dict()

        self.assertIsNone(result["docker"]["present"])
        self.assertEqual(result["docker"]["cli_status"], "error")
        self.assertEqual(result["docker"]["cli_reason"], "Docker CLI probe timed out")
        gaps = {gap["code"]: gap for gap in result["capability_gaps"]}
        self.assertNotIn("docker_missing", gaps)
        self.assertEqual(gaps["docker_probe_error"]["action"], "Retry Docker discovery")
        self.assertNotIn("Install", gaps["docker_probe_error"]["action"])
        self.assertNotIn("poison", json.dumps(result))

    def test_nonzero_docker_probe_is_error_not_missing(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["docker --version"] = [1, "", "private execution error"]

        result = discover_host(runner).to_dict()

        self.assertIsNone(result["docker"]["present"])
        self.assertEqual(result["docker"]["cli_status"], "error")
        gap_codes = {gap["code"] for gap in result["capability_gaps"]}
        self.assertIn("docker_probe_error", gap_codes)
        self.assertNotIn("docker_missing", gap_codes)
        self.assertNotIn("private", json.dumps(result))

    def test_failed_scalar_probes_are_reported_and_poison_stdout_is_ignored(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        failures = {
            "id -u": [1, "9999\n", ""],
            "id -g": [124, "9999\n", ""],
            "timedatectl show --property=Timezone --value": [1, "Private/Timezone\n", ""],
            "cat /proc/meminfo": [1, "MemTotal: 999999 kB\n", ""],
            "uname -m": [127, "poison-architecture\n", ""],
            "cat /proc/cpuinfo": [1, "model name : Poison CPU\n", ""],
        }
        runner.commands.update(failures)

        result = discover_host(runner).to_dict()

        self.assertEqual(result["identity"], {"uid": None, "gid": None, "user": "homeflix", "groups": ["homeflix", "sudo", "docker"], "groups_status": "ok", "session_groups": ["homeflix", "sudo", "docker"], "session_groups_status": "ok", "privilege_escalation": "sudo_noninteractive"})
        self.assertIsNone(result["timezone"])
        self.assertIsNone(result["memory_bytes"])
        self.assertEqual(result["cpu"], {"architecture": None, "model": None})
        self.assertEqual(
            set(result["probe_errors"]),
            {"uid", "gid", "timezone", "memory", "architecture", "cpu_model"},
        )
        self.assertEqual(result["probe_errors"]["gid"], "GID probe timed out")
        self.assertNotIn("Poison", json.dumps(result))
        self.assertNotIn("9999", json.dumps(result))

    def test_successful_empty_scalar_values_remain_legitimately_unavailable(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["timedatectl show --property=Timezone --value"] = [0, "", ""]
        runner.commands["cat /proc/cpuinfo"] = [0, "processor : 0\n", ""]

        result = discover_host(runner).to_dict()

        self.assertIsNone(result["timezone"])
        self.assertIsNone(result["cpu"]["model"])
        self.assertNotIn("timezone", result["probe_errors"])
        self.assertNotIn("cpu_model", result["probe_errors"])

    def test_malformed_or_missing_docker_output_does_not_crash_parser(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["docker --version"] = [127, "", ""]
        runner.commands["docker compose version"] = [127, "", ""]
        runner.commands["docker info --format {{json .ServerVersion}}"] = [127, "", ""]

        facts = discover_host(runner).to_dict()

        self.assertFalse(facts["docker"]["present"])
        self.assertTrue(any(gap["code"] == "docker_missing" for gap in facts["capability_gaps"]))

    def test_json_cli_does_not_persist_private_discovery_facts(self) -> None:
        facts = discover_host(FixtureRunner("discovery-debian.json"))
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "scripts.homeflix_setup.cli.discover_host", return_value=facts
        ):
            root = Path(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(("--json", "discover"), repository_root=root)

            self.assertEqual(return_code, 0, stderr.getvalue())
            self.assertEqual(parse_single_json(stdout.getvalue())["identity"]["uid"], 1000)
            self.assertFalse((root / ".homeflix" / "setup.json").exists())

    def test_unsupported_distribution_cli_returns_nonzero_without_traceback(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["cat /etc/os-release"] = [0, "ID=fedora\nVERSION_ID=41\n", ""]
        facts = discover_host(runner)
        with mock.patch("scripts.homeflix_setup.cli.discover_host", return_value=facts):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(("discover",), repository_root=Path("."))

            self.assertEqual(return_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("not support", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(("--json", "discover"), repository_root=Path("."))

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(parse_single_json(stdout.getvalue())["refusal"]["code"], "unsupported_distribution")


if __name__ == "__main__":
    unittest.main()
