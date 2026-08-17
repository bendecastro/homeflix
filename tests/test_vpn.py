from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from datetime import datetime, timezone

from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.compose import GLUETUN_SERVICES
from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.preflight import CheckResult, PreflightReport
from scripts.homeflix_setup.state import SetupState
from scripts.homeflix_setup.vpn import (
    GATED_SERVICES,
    VPN_BLOCKED_EGRESS_TIMEOUT,
    VPN_RESTORE_BUDGET,
    verify_vpn,
    verify_vpn_fail_closed,
    vpn_config_digest,
    vpn_evidence_is_current,
)
from tests.helpers import parse_single_json


HOST_EGRESS = "203.0.113.10"
TUNNEL_EGRESS = "198.51.100.20"
VPN_SECRET = "vpn-secret-value"
FIXTURE_IPS = (HOST_EGRESS, TUNNEL_EGRESS)


def run_main(*args: str, repository_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = main(args, repository_root=repository_root)
    return return_code, stdout.getvalue(), stderr.getvalue()


def passing_contract(_rendered=None):
    return {"schema_version": 1, "status": "pass", "passed": True, "findings": []}


def passing_preflight(*_args, **_kwargs):
    return PreflightReport("acquisition", (CheckResult("fixture", "pass", "passed"),))


def failing_contract(_root=None):
    return {
        "schema_version": 1,
        "status": "fail",
        "passed": False,
        "findings": [{"code": "vpn_namespace", "service": "qbittorrent", "message": "fixture"}],
    }


def failing_preflight(*_args, **_kwargs):
    return PreflightReport("acquisition", (CheckResult("vpn_provider", "fail", "unsupported"),))


def write_env(root: Path) -> Path:
    path = root / ".env"
    path.write_text(
        "COMPOSE_PROJECT_NAME=homeflix\n"
        "VPN_SERVICE_PROVIDER=protonvpn\n"
        "VPN_TYPE=openvpn\n"
        f"VPN_USER=fixture-user\nVPN_PASSWORD={VPN_SECRET}\n"
        "VPN_HEALTH_TARGET=cloudflare.com:443\n"
        "GLUETUN_TAG=latest\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


SAFE_LINKS = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
    "3: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500\n"
)
DEFAULT_ROUTE_ETH = "default via 172.30.0.1 dev eth0\n"
DEFAULT_ROUTE_TUN = "default via 10.2.0.1 dev tun0\n"


class FakeVpnRunner:
    def __init__(
        self,
        *,
        inventory: list[dict[str, str]] | None = None,
        health: str = "healthy",
        tun_ok: bool = True,
        dns_ok: bool = True,
        host_ip: str | None = HOST_EGRESS,
        tunnel_ip: str | None = TUNNEL_EGRESS,
        image_id: str = "sha256:fixturegluetunimage",
        namespace_mode: str = "container:gluetun",
        up_returncode: int = 0,
        links: str = SAFE_LINKS,
        default_route: str = DEFAULT_ROUTE_ETH,
        fail_at: str | None = None,
        cleanup_fail: bool = False,
        raise_at: str | None = None,
        clock: FakeClock | None = None,
        hang_blocked_egress: bool = False,
    ) -> None:
        self.inventory = inventory if inventory is not None else []
        self.health = health
        self.tun_ok = tun_ok
        self.dns_ok = dns_ok
        self.host_ip = host_ip
        self.tunnel_ip = tunnel_ip
        self.image_id = image_id
        self.namespace_mode = namespace_mode
        self.up_returncode = up_returncode
        self.links = links
        self.default_route = default_route
        self.fail_at = fail_at
        self.cleanup_fail = cleanup_fail
        self.raise_at = raise_at
        self.clock = clock
        self.hang_blocked_egress = hang_blocked_egress
        self.disrupted = False
        self.egress_probes = 0
        self.commands: list[tuple[str, ...]] = []
        self.blocked_egress_timeouts: list[float] = []

    def run(self, argv, **kwargs):
        command = tuple(argv)
        self.commands.append(command)
        if command[:2] == ("docker", "--version") or command[:3] == ("docker", "compose", "version") or command[:2] == ("docker", "info"):
            return subprocess.CompletedProcess(command, 0, "fixture", "")
        if "config" in command and "--quiet" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command and command[0] == "findmnt":
            return subprocess.CompletedProcess(command, 1, "", "not mounted")
        if "up" in command:
            return subprocess.CompletedProcess(command, self.up_returncode, "", "")
        if "ps" in command and "--all" in command:
            payload = list(self.inventory)
            if not any(item.get("service") == "gluetun" for item in payload):
                payload.append(
                    {
                        "Service": "gluetun",
                        "State": "running",
                        "Health": self.health,
                        "Project": "homeflix",
                    }
                )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "ps" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [{"Service": "gluetun", "State": "running", "Health": self.health}]
                ),
                "",
            )
        if command[:3] == ("docker", "exec", "gluetun") and "test" in command:
            return subprocess.CompletedProcess(command, 0 if self.tun_ok else 1, "", "")
        if command[:3] == ("docker", "exec", "gluetun") and "getent" in command:
            stdout = "198.51.100.1 cloudflare.com\n" if self.dns_ok else ""
            return subprocess.CompletedProcess(command, 0 if self.dns_ok else 1, stdout, "")
        if command[:3] == ("docker", "exec", "gluetun") and "wget" in command:
            if self.disrupted:
                timeout = kwargs.get("timeout")
                if timeout is not None:
                    self.blocked_egress_timeouts.append(float(timeout))
                if self.hang_blocked_egress:
                    granted = float(timeout) if timeout is not None else 0.0
                    if self.clock is not None:
                        self.clock.now += granted
                    raise subprocess.TimeoutExpired(command, granted or 1)
                if self.fail_at == "blocked_egress":
                    return subprocess.CompletedProcess(command, 0, (self.tunnel_ip or HOST_EGRESS) + "\n", "")
                return subprocess.CompletedProcess(command, 1, "", "egress unavailable")
            self.egress_probes += 1
            if self.tunnel_ip is None or (self.fail_at == "pre_egress" and self.egress_probes == 1):
                return subprocess.CompletedProcess(command, 1, "", "egress unavailable")
            if self.fail_at == "post_egress" and self.egress_probes >= 2:
                return subprocess.CompletedProcess(command, 0, (self.host_ip or HOST_EGRESS) + "\n", "")
            return subprocess.CompletedProcess(command, 0, self.tunnel_ip + "\n", "")
        if command and command[0] == "curl":
            if self.host_ip is None:
                return subprocess.CompletedProcess(command, 1, "", "egress unavailable")
            return subprocess.CompletedProcess(command, 0, self.host_ip + "\n", "")
        if command[:2] == ("docker", "inspect"):
            if "{{.HostConfig.NetworkMode}}" in command:
                if self.namespace_mode is None:
                    return subprocess.CompletedProcess(command, 1, "", "no such container")
                return subprocess.CompletedProcess(command, 0, self.namespace_mode + "\n", "")
            return subprocess.CompletedProcess(command, 0, self.image_id + "\n", "")
        if command[:3] == ("docker", "exec", "gluetun") and command[3:5] == ("ip", "-o"):
            if "route" in command:
                if self.raise_at == "classify":
                    raise RuntimeError("interface listing interrupted")
                if self.fail_at == "classify":
                    return subprocess.CompletedProcess(command, 1, "", "route unavailable")
                return subprocess.CompletedProcess(command, 0, self.default_route, "")
            if "link" in command and "set" not in command:
                if self.fail_at == "classify":
                    return subprocess.CompletedProcess(command, 1, "", "link listing unavailable")
                return subprocess.CompletedProcess(command, 0, self.links, "")
        if command[:3] == ("docker", "exec", "gluetun") and command[3:5] == ("ip", "link") and "set" in command:
            if self.raise_at == "disrupt":
                self.disrupted = True
                raise KeyboardInterrupt
            if self.fail_at == "disrupt":
                return subprocess.CompletedProcess(command, 1, "", "unable to disable tunnel")
            self.disrupted = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if "restart" in command:
            if self.raise_at == "restore":
                raise TimeoutError("restore deadline exhausted")
            if self.cleanup_fail or self.fail_at in {"restore_gluetun", "restore_dependents"}:
                if self.cleanup_fail or (
                    self.fail_at == "restore_gluetun" and "gluetun" in command
                ) or (
                    self.fail_at == "restore_dependents" and any(service in command for service in GATED_SERVICES)
                ):
                    return subprocess.CompletedProcess(command, 1, "", "restore failed")
            self.disrupted = False
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _verify(root: Path, runner: FakeVpnRunner, **kwargs):
    clock = kwargs.pop("clock", None) or FakeClock()
    sleep = kwargs.pop("sleep", clock.sleep if isinstance(clock, FakeClock) else (lambda _seconds: None))
    return verify_vpn(
        root,
        runner=runner,
        contract_evaluator=lambda _root: passing_contract(),
        preflight=passing_preflight,
        clock=clock,
        sleep=sleep,
        readiness_timeout=kwargs.pop("readiness_timeout", 5.0),
        **kwargs,
    )


class VpnVerifyDryRunTests(unittest.TestCase):
    def test_dry_run_json_plans_only_gluetun_mutation_and_redacts_secrets(self) -> None:
        from scripts.homeflix_setup.vpn import verify_vpn

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            result = verify_vpn(
                root,
                dry_run=True,
                contract_evaluator=lambda _root: passing_contract(),
                preflight=passing_preflight,
            )
            with patch("scripts.homeflix_setup.cli.verify_vpn", return_value=result):
                code, stdout, stderr = run_main(
                    "--json", "vpn", "verify", "--dry-run", repository_root=root
                )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["services"], ["gluetun"])
        self.assertEqual(GLUETUN_SERVICES, ("gluetun",))
        mutations = result["mutation_commands"]
        self.assertTrue(mutations)
        for command in mutations:
            rendered = " ".join(command)
            self.assertIn("gluetun", rendered)
            self.assertIn("--no-deps", rendered)
            for forbidden in ("qbittorrent", "nzbget", "prowlarr"):
                self.assertNotIn(forbidden, rendered)
            self.assertNotIn(VPN_SECRET, rendered)
        self.assertFalse(result.get("state_written", True))
        payload = json.dumps(result)
        self.assertNotIn(VPN_SECRET, payload)
        for address in FIXTURE_IPS:
            self.assertNotIn(address, payload)

        self.assertEqual(code, 0, stderr)
        planned = parse_single_json(stdout)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["services"], ["gluetun"])
        self.assertNotIn(VPN_SECRET, stdout + stderr)

    def test_dry_run_invokes_contract_and_acquisition_preflight(self) -> None:
        contract_roots: list[Path] = []
        preflight_phases: list[str] = []

        def record_contract(evaluated_root):
            contract_roots.append(evaluated_root)
            return passing_contract()

        def record_preflight(config, phase, *_args, **_kwargs):
            preflight_phases.append(phase)
            return passing_preflight()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            result = verify_vpn(
                root,
                dry_run=True,
                contract_evaluator=record_contract,
                preflight=record_preflight,
            )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(contract_roots, [root])
        self.assertEqual(preflight_phases, ["acquisition"])
        self.assertFalse(result.get("state_written", True))

    def test_dry_run_failed_contract_is_not_planned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            result = verify_vpn(
                root,
                dry_run=True,
                contract_evaluator=failing_contract,
                preflight=passing_preflight,
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])
        self.assertNotIn("mutation_commands", result)
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["stack_contract"]["status"], "failure")

    def test_dry_run_failed_acquisition_preflight_is_not_planned(self) -> None:
        phases: list[str] = []

        def record_failing_preflight(config, phase, *_args, **_kwargs):
            phases.append(phase)
            return failing_preflight()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            result = verify_vpn(
                root,
                dry_run=True,
                contract_evaluator=lambda _root: passing_contract(),
                preflight=record_failing_preflight,
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])
        self.assertEqual(phases, ["acquisition"])
        self.assertNotIn("mutation_commands", result)


class VpnVerifyGateTests(unittest.TestCase):
    def test_failed_stack_contract_refuses_gluetun_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            result = verify_vpn(
                root,
                runner=runner,
                contract_evaluator=failing_contract,
                preflight=passing_preflight,
            )

        self.assertFalse(result["passed"], result)
        self.assertEqual(result["status"], "failed")
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["stack_contract"]["status"], "failure")
        self.assertFalse(any("up" in command for command in runner.commands))
        self.assertFalse((root / ".homeflix" / "setup.json").exists())

    def test_default_gate_uses_real_stack_contract_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            with patch(
                "scripts.homeflix_setup.vpn.render_compose_config",
                return_value={"services": {}},
            ):
                result = verify_vpn(root, runner=runner, preflight=passing_preflight)

        self.assertFalse(result["passed"], result)
        self.assertEqual(result["status"], "failed")
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["stack_contract"]["status"], "failure")
        self.assertFalse(any("up" in command for command in runner.commands))

    def test_failed_acquisition_preflight_refuses_gluetun_start(self) -> None:
        phases: list[str] = []

        def record_failing_preflight(config, phase, *_args, **_kwargs):
            phases.append(phase)
            return failing_preflight()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            result = verify_vpn(
                root,
                runner=runner,
                contract_evaluator=lambda _root: passing_contract(),
                preflight=record_failing_preflight,
            )

        self.assertFalse(result["passed"], result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(phases, ["acquisition"])
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["preflight"]["status"], "failure")
        self.assertFalse(any("up" in command for command in runner.commands))
        self.assertFalse((root / ".homeflix" / "setup.json").exists())

    def test_default_gate_uses_real_acquisition_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            result = verify_vpn(
                root,
                runner=runner,
                contract_evaluator=lambda _root: passing_contract(),
            )

        self.assertFalse(result["passed"], result)
        self.assertEqual(result["status"], "failed")
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertIn(checks["preflight"]["status"], {"failure", "unknown"})
        self.assertFalse(any("up" in command for command in runner.commands))

    def test_successful_gate_starts_only_gluetun_and_stores_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            result = _verify(root, runner)
            digest = vpn_config_digest(EnvDocument.load(root / ".env"))
            state = SetupState.load(root / ".homeflix" / "setup.json")

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["status"], "verified")
        domains = {item["domain"]: item for item in result["checks"]}
        for domain in (
            "stack_contract",
            "preflight",
            "gated_services",
            "service:gluetun",
            "tunnel_device",
            "namespace_dns",
            "egress",
        ):
            self.assertEqual(domains[domain]["status"], "pass", domain)
            self.assertIn(domains[domain]["status"], {"pass", "warning", "failure", "not-applicable", "unknown"})
        mutations = [command for command in runner.commands if "up" in command]
        self.assertEqual(len(mutations), 1)
        self.assertIn("gluetun", mutations[0])
        for forbidden in GATED_SERVICES:
            self.assertNotIn(forbidden, " ".join(mutations[0]))
        evidence = result["evidence"]
        self.assertTrue(evidence["current"])
        self.assertTrue(evidence["tunnel_healthy"])
        self.assertTrue(evidence["egress_distinct"])
        self.assertRegex(evidence["recorded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        rendered = json.dumps(result)
        self.assertNotIn(VPN_SECRET, rendered)
        for address in FIXTURE_IPS:
            self.assertNotIn(address, rendered)
        self.assertNotIn("198.51.100.1", rendered)
        self.assertTrue(vpn_evidence_is_current(
            state.evidence,
            image_id="sha256:fixturegluetunimage",
            config_digest=digest,
        ))

    def test_gate_succeeds_on_running_stack_when_clients_share_the_gluetun_namespace(self) -> None:
        inventory = [
            {"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"}
            for service in ("qbittorrent", "prowlarr")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner(inventory=inventory)
            result = _verify(root, runner)
            digest = vpn_config_digest(EnvDocument.load(root / ".env"))
            state = SetupState.load(root / ".homeflix" / "setup.json")

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["status"], "verified")
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["gated_services"]["status"], "pass")
        self.assertIn("namespace", checks["gated_services"]["reason"])
        mutations = [command for command in runner.commands if "up" in command]
        self.assertEqual(len(mutations), 1)
        self.assertIn("gluetun", mutations[0])
        for forbidden in GATED_SERVICES:
            self.assertNotIn(forbidden, " ".join(mutations[0]))
        self.assertTrue(vpn_evidence_is_current(
            state.evidence,
            image_id="sha256:fixturegluetunimage",
            config_digest=digest,
        ))
        self.assertIsNot(state.evidence.get("fail_closed"), True)
        rendered = json.dumps(result)
        self.assertNotIn(VPN_SECRET, rendered)
        for address in FIXTURE_IPS:
            self.assertNotIn(address, rendered)

    def test_successful_gate_invokes_contract_and_acquisition_preflight(self) -> None:
        contract_roots: list[Path] = []
        preflight_phases: list[str] = []

        def record_contract(evaluated_root):
            contract_roots.append(evaluated_root)
            return passing_contract()

        def record_preflight(config, phase, *_args, **_kwargs):
            preflight_phases.append(phase)
            return passing_preflight()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            result = verify_vpn(
                root,
                runner=runner,
                contract_evaluator=record_contract,
                preflight=record_preflight,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )

        self.assertTrue(result["passed"], result)
        self.assertEqual(contract_roots, [root])
        self.assertEqual(preflight_phases, ["acquisition"])
        self.assertTrue(any("up" in command and "gluetun" in command for command in runner.commands))

    def test_refuses_equal_unknown_unhealthy_missing_tun_dns_or_ungated_running_clients(self) -> None:
        cases = (
            {"tunnel_ip": HOST_EGRESS, "domain": "egress"},
            {"host_ip": None, "domain": "egress"},
            {"health": "unhealthy", "domain": "service:gluetun"},
            {"tun_ok": False, "domain": "tunnel_device"},
            {"dns_ok": False, "domain": "namespace_dns"},
            {
                "inventory": [
                    {
                        "Service": "qbittorrent",
                        "State": "running",
                        "Health": "healthy",
                        "Project": "homeflix",
                    }
                ],
                "namespace_mode": "bridge",
                "domain": "gated_services",
            },
            {
                "inventory": [
                    {
                        "Service": "qbittorrent",
                        "State": "running",
                        "Health": "healthy",
                        "Project": "homeflix",
                    }
                ],
                "namespace_mode": None,
                "domain": "gated_services",
            },
        )
        for kwargs in cases:
            domain = kwargs.pop("domain")
            with self.subTest(domain=domain, kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_env(root)
                runner = FakeVpnRunner(**kwargs)
                result = _verify(root, runner)
                self.assertFalse(result["passed"], result)
                self.assertEqual(result["status"], "failed")
                checks = {item["domain"]: item for item in result["checks"]}
                self.assertIn(checks[domain]["status"], {"failure", "unknown"})
                if domain != "gated_services":
                    self.assertTrue(any("up" in command and "gluetun" in command for command in runner.commands) or domain == "gated_services")
                else:
                    self.assertFalse(any("up" in command for command in runner.commands))
                rendered = json.dumps(result)
                self.assertNotIn(VPN_SECRET, rendered)
                for address in FIXTURE_IPS:
                    self.assertNotIn(address, rendered)
                self.assertFalse((root / ".homeflix" / "setup.json").exists() or vpn_evidence_is_current(
                    SetupState.load(root / ".homeflix" / "setup.json").evidence if (root / ".homeflix" / "setup.json").exists() else None,
                    image_id="sha256:fixturegluetunimage",
                    config_digest="unused",
                ))

    def test_evidence_expires_after_a_day_or_image_or_config_change(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        fresh = {
            "recorded_at": "2026-08-14T11:00:00Z",
            "image_id": "sha256:aaa",
            "config_digest": "abc",
            "tunnel_healthy": True,
            "tunnel_device": True,
            "namespace_dns": True,
            "egress_distinct": True,
        }
        self.assertTrue(vpn_evidence_is_current(fresh, image_id="sha256:aaa", config_digest="abc", now=now))
        stale = dict(fresh, recorded_at="2026-08-13T11:59:00Z")
        self.assertFalse(vpn_evidence_is_current(stale, image_id="sha256:aaa", config_digest="abc", now=now))
        self.assertFalse(vpn_evidence_is_current(fresh, image_id="sha256:bbb", config_digest="abc", now=now))
        self.assertFalse(vpn_evidence_is_current(fresh, image_id="sha256:aaa", config_digest="changed", now=now))
        self.assertFalse(vpn_evidence_is_current(None, image_id="sha256:aaa", config_digest="abc", now=now))
        self.assertFalse(
            vpn_evidence_is_current(
                {**fresh, "host_ip": HOST_EGRESS},
                image_id="sha256:aaa",
                config_digest="abc",
                now=now,
            )
        )
        self.assertTrue(
            vpn_evidence_is_current(
                {**fresh, "fail_closed": True},
                image_id="sha256:aaa",
                config_digest="abc",
                now=now,
            )
        )
        self.assertTrue(
            vpn_evidence_is_current(
                {**fresh, "fail_closed": False},
                image_id="sha256:aaa",
                config_digest="abc",
                now=now,
            )
        )
        self.assertFalse(
            vpn_evidence_is_current(
                {**fresh, "fail_closed": "yes"},
                image_id="sha256:aaa",
                config_digest="abc",
                now=now,
            )
        )

    def test_deadline_exhaustion_fails_closed_without_starting_gated_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            runner = FakeVpnRunner()
            result = _verify(root, runner, deadline=0.0, clock=lambda: 1.0)
        self.assertFalse(result["passed"])
        self.assertFalse(any(service in " ".join(command) for command in runner.commands for service in GATED_SERVICES))
        rendered = json.dumps(result)
        self.assertNotIn(VPN_SECRET, rendered)


def write_current_evidence(root: Path, image_id: str = "sha256:fixturegluetunimage") -> None:
    digest = vpn_config_digest(EnvDocument.load(root / ".env"))
    state = SetupState()
    state.evidence = {
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "image_id": image_id,
        "config_digest": digest,
        "tunnel_healthy": True,
        "tunnel_device": True,
        "namespace_dns": True,
        "egress_distinct": True,
    }
    state.save(root / ".homeflix" / "setup.json")


def _assert_bounded(payload: object) -> None:
    rendered = json.dumps(payload)
    assert VPN_SECRET not in rendered
    for address in FIXTURE_IPS:
        assert address not in rendered
    for leaked in ("tun0", "wg0", "eth0", "docker0", "br-", "198.51.100.", "203.0.113.", "10.2.0.", "172.30."):
        assert leaked not in rendered, leaked


class VpnFailClosedGateTests(unittest.TestCase):
    def test_refuses_without_explicit_disrupt_flag_and_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner()
            result = verify_vpn_fail_closed(root, runner=runner)

        self.assertFalse(result["passed"], result)
        self.assertEqual(result["status"], "failed")
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["intent"]["status"], "failure")
        self.assertFalse(result.get("disrupted", False))
        self.assertFalse(any("link" in command or "restart" in command for command in runner.commands))
        _assert_bounded(result)

    def test_cli_verify_vpn_without_disrupt_refuses_and_vpn_verify_stays_non_disruptive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            refused = {
                "status": "failed",
                "passed": False,
                "disrupted": False,
                "checks": [{"domain": "intent", "status": "failure", "reason": "disruptive verification requires --disrupt"}],
            }
            with patch("scripts.homeflix_setup.cli.verify_vpn_fail_closed", return_value=refused) as fail_closed:
                code, stdout, stderr = run_main("--json", "verify", "vpn", repository_root=root)
            self.assertEqual(code, 1, stderr)
            payload = parse_single_json(stdout)
            self.assertFalse(payload["passed"])
            self.assertEqual(payload["checks"][0]["domain"], "intent")
            fail_closed.assert_called_once()
            self.assertFalse(fail_closed.call_args.kwargs.get("disrupt", False))

            planned = {
                "status": "planned",
                "passed": True,
                "services": ["gluetun"],
                "mutation_commands": [["docker", "compose", "up", "--detach", "--no-deps", "gluetun"]],
                "state_written": False,
                "checks": [],
            }
            with patch("scripts.homeflix_setup.cli.verify_vpn", return_value=planned) as gate:
                with patch("scripts.homeflix_setup.cli.verify_vpn_fail_closed", side_effect=AssertionError("routine vpn verify must not disrupt")):
                    code, stdout, stderr = run_main("--json", "vpn", "verify", "--dry-run", repository_root=root)
            self.assertEqual(code, 0, stderr)
            gate.assert_called_once()
            self.assertTrue(gate.call_args.kwargs.get("dry_run"))
            _assert_bounded(payload)

    def test_cli_disrupt_drivers_require_flag_and_share_fail_closed_transaction(self) -> None:
        verified = {
            "status": "verified",
            "passed": True,
            "disrupted": True,
            "restored": True,
            "snapshot_services": ["qbittorrent"],
            "restored_services": ["gluetun", "qbittorrent"],
            "checks": [{"domain": "blocked_egress", "status": "pass", "reason": "external access is blocked"}],
        }
        for argv in (
            ("--json", "verify", "vpn", "--disrupt"),
            ("--json", "vpn", "verify", "--disrupt"),
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_env(root)
                with patch("scripts.homeflix_setup.cli.verify_vpn_fail_closed", return_value=verified) as fail_closed:
                    with patch("scripts.homeflix_setup.cli.verify_vpn", side_effect=AssertionError("routine vpn verify must not run")):
                        code, stdout, stderr = run_main(*argv, repository_root=root)
                self.assertEqual(code, 0, stderr)
                payload = parse_single_json(stdout)
                self.assertTrue(payload["passed"])
                self.assertEqual(payload["restored_services"], ["gluetun", "qbittorrent"])
                fail_closed.assert_called_once()
                self.assertTrue(fail_closed.call_args.kwargs.get("disrupt"))
                _assert_bounded(payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, stdout, stderr = run_main("--json", "vpn", "verify", "--disrupt", "--dry-run", repository_root=root)
        self.assertEqual(code, 1, stdout + stderr)
        combined = stdout + stderr
        self.assertNotIn(VPN_SECRET, combined)

    def test_refuses_without_current_vpn_gate_evidence_and_does_not_mutate(self) -> None:
        cases = (
            "missing",
            "stale",
            "image_mismatch",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_env(root)
                if case != "missing":
                    write_current_evidence(root, image_id="sha256:fixturegluetunimage")
                    if case == "stale":
                        state = SetupState.load(root / ".homeflix" / "setup.json")
                        state.evidence["recorded_at"] = "2020-01-01T00:00:00Z"
                        state.save(root / ".homeflix" / "setup.json")
                runner = FakeVpnRunner(
                    image_id="sha256:otherimage" if case == "image_mismatch" else "sha256:fixturegluetunimage"
                )
                result = verify_vpn_fail_closed(root, runner=runner, disrupt=True)
                self.assertFalse(result["passed"], result)
                self.assertEqual(result["status"], "failed")
                checks = {item["domain"]: item for item in result["checks"]}
                self.assertIn(checks["vpn_evidence"]["status"], {"failure", "unknown"})
                self.assertFalse(result.get("disrupted", False))
                self.assertFalse(
                    any(
                        "link" in command or "restart" in command or "up" in command
                        for command in runner.commands
                    )
                )
                _assert_bounded(result)

    def test_refuses_loopback_ethernet_docker_bridge_default_route_and_unknown(self) -> None:
        cases = (
            (
                "loopback",
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n",
                DEFAULT_ROUTE_ETH,
            ),
            (
                "ethernet",
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n",
                DEFAULT_ROUTE_ETH,
            ),
            (
                "docker-bridge",
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n2: docker0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n",
                "default via 172.17.0.1 dev docker0\n",
            ),
            (
                "default-route",
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n2: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n",
                "default via 192.0.2.1 dev wlan0\n",
            ),
            (
                "unknown",
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n2: dummy0: <BROADCAST,NOARP,UP,LOWER_UP> mtu 1500\n",
                "default via 192.0.2.1 dev missing0\n",
            ),
        )
        for kind, links, route in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_env(root)
                write_current_evidence(root)
                runner = FakeVpnRunner(links=links, default_route=route)
                result = verify_vpn_fail_closed(root, runner=runner, disrupt=True)
                self.assertFalse(result["passed"], result)
                checks = {item["domain"]: item for item in result["checks"]}
                self.assertEqual(checks["tunnel_interface"]["status"], "failure")
                self.assertIn(kind, checks["tunnel_interface"]["reason"])
                self.assertFalse(result.get("disrupted", False))
                self.assertFalse(any("set" in command and "down" in command for command in runner.commands))
                self.assertFalse(any("restart" in command for command in runner.commands))
                _assert_bounded(result)


def _running(service: str) -> dict[str, str]:
    return {"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"}


class VpnFailClosedTransactionTests(unittest.TestCase):
    def test_proves_fail_closed_and_restores_only_previously_running_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner(inventory=[_running("qbittorrent"), {"Service": "nzbget", "State": "exited", "Health": "", "Project": "homeflix"}])
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            stored = SetupState.load(root / ".homeflix" / "setup.json").evidence
            digest = vpn_config_digest(EnvDocument.load(root / ".env"))

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["disrupted"])
        self.assertTrue(result["restored"])
        self.assertEqual(result["snapshot_services"], ["qbittorrent"])
        self.assertEqual(result["restored_services"], ["gluetun", "qbittorrent"])
        domains = {item["domain"]: item for item in result["checks"]}
        for domain in (
            "intent",
            "vpn_evidence",
            "tunnel_interface",
            "pre_egress",
            "disruption",
            "blocked_egress",
            "restore:gluetun",
            "restore:qbittorrent",
            "post_health",
            "post_egress",
        ):
            self.assertEqual(domains[domain]["status"], "pass", domain)
        self.assertNotIn("restore:nzbget", domains)
        self.assertNotIn("restore:prowlarr", domains)
        rendered_commands = [" ".join(command) for command in runner.commands]
        self.assertTrue(any("ip link set" in text and "down" in text for text in rendered_commands))
        self.assertTrue(any("restart" in text and "gluetun" in text for text in rendered_commands))
        self.assertTrue(any("restart" in text and "qbittorrent" in text for text in rendered_commands))
        self.assertFalse(any("nzbget" in text and "restart" in text for text in rendered_commands))
        self.assertFalse(any("prowlarr" in text and ("restart" in text or "up" in text) for text in rendered_commands))
        _assert_bounded(result)
        self.assertTrue(stored.get("fail_closed") is True)
        self.assertTrue(vpn_evidence_is_current(
            stored,
            image_id="sha256:fixturegluetunimage",
            config_digest=digest,
        ))

    def test_failed_or_interrupted_disrupt_does_not_set_fail_closed(self) -> None:
        cases = (
            {"fail_at": "disrupt"},
            {"raise_at": "disrupt"},
            {"fail_at": "blocked_egress"},
        )
        for kwargs in cases:
            with self.subTest(**kwargs), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_env(root)
                write_current_evidence(root)
                before = SetupState.load(root / ".homeflix" / "setup.json").evidence
                self.assertNotIn("fail_closed", before)
                runner = FakeVpnRunner(inventory=[_running("qbittorrent")], **kwargs)
                result = verify_vpn_fail_closed(
                    root,
                    runner=runner,
                    disrupt=True,
                    clock=FakeClock(),
                    sleep=lambda _seconds: None,
                    readiness_timeout=5.0,
                )
                self.assertFalse(result["passed"], result)
                stored = SetupState.load(root / ".homeflix" / "setup.json").evidence
                self.assertNotEqual(stored.get("fail_closed"), True)
                self.assertTrue(vpn_evidence_is_current(
                    stored,
                    image_id="sha256:fixturegluetunimage",
                    config_digest=vpn_config_digest(EnvDocument.load(root / ".env")),
                ))
                _assert_bounded(result)

    def test_default_route_through_the_tunnel_is_still_classified_as_tunnel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner(inventory=[_running("qbittorrent")], default_route=DEFAULT_ROUTE_TUN)
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )

        self.assertTrue(result["passed"], result)
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["tunnel_interface"]["status"], "pass")
        self.assertNotIn("default-route", checks["tunnel_interface"]["reason"])
        _assert_bounded(result)

    def test_snapshots_every_running_namespace_dependent_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner(inventory=[_running(name) for name in GATED_SERVICES])
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["snapshot_services"], list(GATED_SERVICES))
        self.assertEqual(result["restored_services"], ["gluetun", *GATED_SERVICES])
        rendered_commands = [" ".join(command) for command in runner.commands]
        for service in GATED_SERVICES:
            self.assertTrue(any("restart" in text and service in text for text in rendered_commands), service)
        _assert_bounded(result)

    def test_mutation_stage_failures_restore_and_cannot_succeed(self) -> None:
        stages = (
            ("pre_egress", "pre_egress", False),
            ("disrupt", "disruption", True),
            ("blocked_egress", "blocked_egress", True),
            ("restore_gluetun", "restore:gluetun", True),
            ("restore_dependents", "restore:qbittorrent", True),
            ("post_egress", "post_egress", True),
        )
        for fail_at, domain, should_restore in stages:
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_env(root)
                write_current_evidence(root)
                runner = FakeVpnRunner(inventory=[_running("qbittorrent")], fail_at=fail_at)
                result = verify_vpn_fail_closed(
                    root,
                    runner=runner,
                    disrupt=True,
                    clock=FakeClock(),
                    sleep=lambda _seconds: None,
                    readiness_timeout=5.0,
                )
                self.assertFalse(result["passed"], result)
                checks = {item["domain"]: item for item in result["checks"]}
                self.assertIn(checks[domain]["status"], {"failure", "unknown"})
                restarted = any("restart" in command for command in runner.commands)
                self.assertEqual(restarted, should_restore)
                if should_restore:
                    self.assertTrue(any("gluetun" in command and "restart" in command for command in runner.commands))
                self.assertFalse(result.get("passed"))
                _assert_bounded(result)

    def test_deadline_and_interrupt_after_disruption_still_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner(inventory=[_running("qbittorrent")], raise_at="disrupt")
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            self.assertFalse(result["passed"], result)
            checks = {item["domain"]: item for item in result["checks"]}
            self.assertIn(checks["transaction"]["status"], {"failure", "unknown"})
            self.assertTrue(result["disrupted"] or any("set" in command and "down" in command for command in runner.commands))
            self.assertTrue(any("restart" in command and "gluetun" in command for command in runner.commands))
            _assert_bounded(result)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            clock = FakeClock()
            deadline = 4.0
            runner = FakeVpnRunner(
                inventory=[_running("qbittorrent")],
                clock=clock,
                hang_blocked_egress=True,
            )
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                deadline=deadline,
                clock=clock,
                sleep=clock.sleep,
                readiness_timeout=deadline,
            )
            self.assertFalse(result["passed"], result)
            self.assertGreaterEqual(clock(), deadline)
            self.assertTrue(
                result["disrupted"]
                or any("set" in command and "down" in command for command in runner.commands)
            )
            restarted = [" ".join(command) for command in runner.commands]
            self.assertTrue(any("restart" in text and "gluetun" in text for text in restarted), restarted)
            self.assertTrue(any("restart" in text and "qbittorrent" in text for text in restarted), restarted)
            _assert_bounded(result)

    def test_blocked_egress_probe_cannot_consume_restore_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            clock = FakeClock()
            runner = FakeVpnRunner(
                inventory=[_running("qbittorrent")],
                clock=clock,
                hang_blocked_egress=True,
            )
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                deadline=120.0,
                clock=clock,
                sleep=clock.sleep,
                readiness_timeout=120.0,
            )

        self.assertFalse(result["passed"], result)
        self.assertTrue(runner.blocked_egress_timeouts)
        self.assertLessEqual(max(runner.blocked_egress_timeouts), VPN_BLOCKED_EGRESS_TIMEOUT)
        self.assertLess(clock(), VPN_RESTORE_BUDGET)
        restarted = [" ".join(command) for command in runner.commands]
        self.assertTrue(any("restart" in text and "gluetun" in text for text in restarted), restarted)
        self.assertTrue(any("restart" in text and "qbittorrent" in text for text in restarted), restarted)
        _assert_bounded(result)

    def test_primary_failure_is_preserved_when_cleanup_also_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner(
                inventory=[_running("qbittorrent")],
                fail_at="blocked_egress",
                cleanup_fail=True,
            )
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )

        self.assertFalse(result["passed"], result)
        self.assertFalse(result["restored"])
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["blocked_egress"]["status"], "failure")
        self.assertEqual(checks["blocked_egress"]["reason"], "external access still succeeded")
        self.assertIn(checks["restore:gluetun"]["status"], {"failure", "unknown"})
        self.assertLess(
            [item["domain"] for item in result["checks"]].index("blocked_egress"),
            [item["domain"] for item in result["checks"]].index("restore:gluetun"),
        )
        _assert_bounded(result)

    def test_incomplete_restore_cannot_succeed_even_when_probes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_env(root)
            write_current_evidence(root)
            runner = FakeVpnRunner(inventory=[_running("qbittorrent")], fail_at="restore_dependents")
            result = verify_vpn_fail_closed(
                root,
                runner=runner,
                disrupt=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )

        self.assertFalse(result["passed"], result)
        self.assertFalse(result["restored"])
        self.assertIn("gluetun", result["restored_services"])
        self.assertNotIn("qbittorrent", result["restored_services"])
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["blocked_egress"]["status"], "pass")
        self.assertEqual(checks["restore:gluetun"]["status"], "pass")
        self.assertIn(checks["restore:qbittorrent"]["status"], {"failure", "unknown"})
        _assert_bounded(result)
