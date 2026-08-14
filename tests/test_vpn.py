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
    verify_vpn,
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
        up_returncode: int = 0,
    ) -> None:
        self.inventory = inventory if inventory is not None else []
        self.health = health
        self.tun_ok = tun_ok
        self.dns_ok = dns_ok
        self.host_ip = host_ip
        self.tunnel_ip = tunnel_ip
        self.image_id = image_id
        self.up_returncode = up_returncode
        self.commands: list[tuple[str, ...]] = []

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
            if self.tunnel_ip is None:
                return subprocess.CompletedProcess(command, 1, "", "egress unavailable")
            return subprocess.CompletedProcess(command, 0, self.tunnel_ip + "\n", "")
        if command and command[0] == "curl":
            if self.host_ip is None:
                return subprocess.CompletedProcess(command, 1, "", "egress unavailable")
            return subprocess.CompletedProcess(command, 0, self.host_ip + "\n", "")
        if command[:2] == ("docker", "inspect"):
            return subprocess.CompletedProcess(command, 0, self.image_id + "\n", "")
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

    def test_refuses_equal_unknown_unhealthy_missing_tun_dns_or_running_gated_services(self) -> None:
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
