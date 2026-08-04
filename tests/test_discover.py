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


class HostDiscoveryTests(unittest.TestCase):
    def test_debian_discovers_supported_host_and_private_runtime_facts(self) -> None:
        runner = FixtureRunner("discovery-debian.json")

        facts = discover_host(runner)
        result = facts.to_dict()

        self.assertEqual(result["os"], {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)", "supported": True})
        self.assertEqual(result["identity"], {"uid": 1000, "gid": 1001})
        self.assertEqual(result["timezone"], "Europe/London")
        self.assertEqual(result["memory_bytes"], 16_777_216_000)
        self.assertEqual(result["cpu"], {"architecture": "x86_64", "model": "Fixture Intel CPU"})
        self.assertEqual(result["graphics"], {"render_devices": ["/dev/dri/renderD128"], "available": True})
        self.assertEqual(result["listening_ports"], [80, 8096])
        self.assertEqual(result["mounts"][1], {"target": "/srv/media", "source": "/dev/mapper/media", "filesystem": "ext4", "free_bytes": 1099511627776})
        self.assertTrue(result["docker"]["present"])
        self.assertTrue(result["docker"]["compose_present"])
        self.assertTrue(result["docker"]["daemon_reachable"])
        self.assertEqual(result["host_dns"], {"nameservers": ["192.0.2.53"], "search": ["lan.example"]})
        self.assertEqual(result["docker_dns"]["status"], "not_tested")
        self.assertIn("non-mutating", result["docker_dns"]["reason"])
        self.assertEqual(result["execution_context"], {"ssh": True})
        self.assertTrue(all(timeout is not None and timeout > 0 for _, timeout in runner.calls))

    def test_ubuntu_without_docker_reports_actionable_capability_gaps(self) -> None:
        facts = discover_host(FixtureRunner("discovery-ubuntu.json")).to_dict()

        self.assertTrue(facts["os"]["supported"])
        self.assertEqual(facts["cpu"]["architecture"], "aarch64")
        self.assertFalse(facts["graphics"]["available"])
        self.assertEqual(facts["listening_ports"], [53])
        self.assertFalse(facts["docker"]["present"])
        self.assertFalse(facts["docker"]["compose_present"])
        self.assertFalse(facts["docker"]["daemon_reachable"])
        self.assertEqual(facts["docker_dns"], {"status": "not_tested", "reason": "Docker daemon is not reachable"})
        gap_codes = {gap["code"] for gap in facts["capability_gaps"]}
        self.assertEqual(gap_codes, {"docker_missing", "compose_missing"})
        self.assertIn("Install Docker", facts["capability_gaps"][0]["action"])

    def test_unsupported_distribution_is_structured_refusal(self) -> None:
        runner = FixtureRunner("discovery-debian.json")
        runner.commands["cat /etc/os-release"] = [0, "ID=fedora\nVERSION_ID=41\nPRETTY_NAME=Fedora\n", ""]

        facts = discover_host(runner).to_dict()

        self.assertFalse(facts["os"]["supported"])
        self.assertEqual(facts["refusal"]["code"], "unsupported_distribution")
        self.assertIn("Debian and Ubuntu", facts["refusal"]["action"])

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
