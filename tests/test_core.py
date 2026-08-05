from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.homeflix_setup.compose import CORE_SERVICES
from scripts.homeflix_setup.core import ReadinessResult, deploy_core
from scripts.homeflix_setup.preflight import CheckResult, PreflightReport


class FakeRunner:
    def __init__(self, states: list[dict[str, object]], up_returncode: int = 0) -> None:
        self.states = states
        self.up_returncode = up_returncode
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, **kwargs):
        command = tuple(argv)
        self.commands.append(command)
        if "ps" in command:
            payload = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return subprocess.CompletedProcess(command, 0, json.dumps(list(payload.values())), "")
        if "up" in command:
            return subprocess.CompletedProcess(command, self.up_returncode, "10.0.0.8 password=hunter2", "")
        raise AssertionError(f"unexpected command: {command}")


def records(**services: tuple[str, str]) -> dict[str, object]:
    return {
        name: {"Service": name, "State": state, "Health": health}
        for name, (state, health) in services.items()
    }


def ready_records() -> dict[str, object]:
    return records(**{name: ("running", "healthy") for name in CORE_SERVICES})


def passing_preflight(config, phase, runner):
    return PreflightReport("core", (CheckResult("fixture", "pass", "passed"),))


class CoreDeploymentTests(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / ".env").write_text(
            "COMPOSE_PROJECT_NAME=homeflix\nDOMAIN=homeflix.test\n", encoding="utf-8"
        )
        return temporary, root

    def test_deploy_invokes_only_immutable_core_allowlist_and_waits_for_both_conditions(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}, ready_records()])
        container_calls: list[str] = []
        http_calls: list[str] = []

        def containers(service, probe):
            container_calls.append(service)
            return ReadinessResult(True, "ready")

        def http(url, **kwargs):
            http_calls.append(url)
            return ReadinessResult(True, "ready")

        result = deploy_core(
            root, runner=runner, preflight_runner=passing_preflight,
            container_waiter=containers, http_waiter=http,
        )
        up = next(command for command in runner.commands if "up" in command)
        self.assertEqual(up[-7:], ("up", "--detach", *CORE_SERVICES))
        self.assertEqual(container_calls, list(CORE_SERVICES))
        self.assertEqual(len(http_calls), len(CORE_SERVICES))
        self.assertEqual(result["status"], "ready")
        self.assertTrue((root / ".homeflix" / "setup.json").exists())
        rendered = " ".join(up).casefold()
        for forbidden in ("gluetun", "qbittorrent", "nzbget", "prowlarr", "lidarr", "bazarr", "glances", "watchtower"):
            self.assertNotIn(forbidden, rendered)

    def test_healthy_live_services_are_noop_regardless_of_checkpoint(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([ready_records()])
        result = deploy_core(
            root, runner=runner,
            preflight_runner=lambda *args: self.fail("preflight must not run for a no-op"),
            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
        )
        self.assertEqual(result["status"], "already_ready")
        self.assertFalse(result["changed"])
        self.assertFalse(any("up" in command for command in runner.commands))
        checkpoint = json.loads((root / ".homeflix" / "setup.json").read_text(encoding="utf-8"))
        self.assertTrue(checkpoint["checkpoints"]["core_containers_started"])

    def test_unknown_live_state_fails_closed_before_preflight_or_mutation(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}])
        runner.states = [{"bad": {"State": "running"}}]
        result = deploy_core(
            root, runner=runner,
            preflight_runner=lambda *args: self.fail("preflight must not run with unknown state"),
        )
        self.assertEqual(result["status"], "live_state_failed")
        self.assertFalse(any("up" in command for command in runner.commands))
        self.assertFalse((root / ".homeflix" / "setup.json").exists())

    def test_partial_failure_is_sanitized_resumable_and_does_not_checkpoint(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}, ready_records()], up_returncode=1)
        result = deploy_core(
            root, runner=runner, preflight_runner=passing_preflight,
            container_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
            http_waiter=lambda url, **kwargs: ReadinessResult(
                "radarr" not in kwargs.get("headers", {}).get("Host", ""),
                "ready" if "radarr" not in kwargs.get("headers", {}).get("Host", "") else "HTTP readiness timed out",
            ),
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertFalse(result["checkpoint_recorded"])
        self.assertFalse((root / ".homeflix" / "setup.json").exists())
        serialized = json.dumps(result)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("10.0.0.8", serialized)
        self.assertNotIn("password", serialized.casefold())
        self.assertTrue(any(item["service"] == "radarr" and not item["ready"] for item in result["services"]))

    def test_dry_run_executes_no_commands_preflight_or_state_write(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}])
        result = deploy_core(
            root, runner=runner, dry_run=True,
            preflight_runner=lambda *args: self.fail("dry-run must not preflight"),
        )
        self.assertEqual(runner.commands, [])
        self.assertEqual(result["services"], list(CORE_SERVICES))
        self.assertEqual(result["commands"][-1][-7:], ["up", "--detach", *CORE_SERVICES])
        self.assertFalse((root / ".homeflix" / "setup.json").exists())


if __name__ == "__main__":
    unittest.main()
