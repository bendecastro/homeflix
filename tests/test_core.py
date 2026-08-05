from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.compose import CORE_SERVICES
from scripts.homeflix_setup.core import (
    ReadinessResult,
    _readiness_targets,
    capture_deployment_snapshot,
    deploy_core,
    wait_for_container,
    wait_for_http,
)
from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.preflight import CheckResult, PreflightReport


class FakeRunner:
    def __init__(
        self,
        states: list[dict[str, object]],
        up_returncode: int = 0,
        up_error: BaseException | None = None,
    ) -> None:
        self.states = states
        self.up_returncode = up_returncode
        self.up_error = up_error
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, **kwargs):
        command = tuple(argv)
        self.commands.append(command)
        if "ps" in command:
            payload = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return subprocess.CompletedProcess(command, 0, json.dumps(list(payload.values())), "")
        if "up" in command:
            if self.up_error is not None:
                raise self.up_error
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ReadinessHelperTests(unittest.TestCase):
    def test_targets_use_direct_safe_endpoints_and_expected_host_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("DOMAIN=homeflix.test\n", encoding="utf-8")
            targets = _readiness_targets(EnvDocument.load(env))
        self.assertEqual(targets["traefik"], ("http://127.0.0.1:8080/api/rawdata", {}))
        self.assertEqual(targets["jellyfin"], ("http://127.0.0.1:8096/System/Info/Public", {}))
        self.assertEqual(targets["jellyseerr"][1], {"Host": "jellyseerr.homeflix.test"})
        self.assertEqual(targets["radarr"], ("http://127.0.0.1/ping", {"Host": "radarr.homeflix.test"}))
        self.assertEqual(targets["sonarr"], ("http://127.0.0.1/ping", {"Host": "sonarr.homeflix.test"}))

    def test_snapshot_hashes_artifacts_mount_identity_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            compose = root / "docker-compose.yml"
            env.write_text(f"DATA_ROOT={root}\n", encoding="utf-8")
            compose.write_text("services: {}\n", encoding="utf-8")
            first = capture_deployment_snapshot(root, EnvDocument.load(env))
            self.assertIsNotNone(first.data_root_identity)
            self.assertIsNotNone(first.data_mount_record)
            self.assertIsNone(first.override)
            compose.write_text("services:\n  fixture: {}\n", encoding="utf-8")
            (root / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
            second = capture_deployment_snapshot(root, EnvDocument.load(env))
            self.assertNotEqual(first.compose.sha256, second.compose.sha256)
            self.assertIsNotNone(second.override)
            compose.unlink()
            compose.symlink_to(root / "docker-compose.override.yml")
            with self.assertRaises(ValueError):
                capture_deployment_snapshot(root, EnvDocument.load(env))

    def test_wait_for_http_retries_and_obeys_one_deadline(self) -> None:
        clock = FakeClock()
        attempts: list[float] = []
        def eventual(url, headers, timeout):
            attempts.append(timeout)
            return len(attempts) == 3
        result = wait_for_http(
            "http://fixture", timeout=5, interval=1, probe=eventual,
            sleep=clock.sleep, clock=clock,
        )
        self.assertTrue(result.ready)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(clock.now, 2)

        clock = FakeClock()
        result = wait_for_http(
            "http://fixture", timeout=3, interval=2,
            probe=lambda *args: False, sleep=clock.sleep, clock=clock,
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "HTTP readiness timed out")
        self.assertEqual(clock.now, 3)

    def test_wait_for_container_retries_and_reports_unhealthy_at_deadline(self) -> None:
        clock = FakeClock()
        states = iter((
            {"radarr": {"state": "restarting", "health": ""}},
            {"radarr": {"state": "running", "health": "healthy"}},
        ))
        result = wait_for_container(
            "radarr", lambda timeout: next(states), timeout=4, interval=1,
            sleep=clock.sleep, clock=clock,
        )
        self.assertTrue(result.ready)
        self.assertEqual(clock.now, 1)

        clock = FakeClock()
        result = wait_for_container(
            "radarr", lambda timeout: {"radarr": {"state": "running", "health": "unhealthy"}},
            timeout=3, interval=2, sleep=clock.sleep, clock=clock,
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "container reported unhealthy")
        self.assertEqual(clock.now, 3)


class CoreDeploymentTests(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / ".env").write_text(
            "COMPOSE_PROJECT_NAME=homeflix\nDOMAIN=homeflix.test\n", encoding="utf-8"
        )
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        return temporary, root

    def test_deploy_invokes_only_immutable_core_allowlist_and_waits_for_both_conditions(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}, ready_records()])
        container_calls: list[str] = []
        http_calls: list[str] = []

        def containers(service, probe, **kwargs):
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
        self.assertEqual(up[-8:], ("up", "--detach", "--no-deps", *CORE_SERVICES))
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

    def test_malformed_compose_identity_never_reaches_preflight_or_compose_up(self) -> None:
        malformed_services = (
            "radarré", "radarrK", "ſonarr", ".radarr", "-radarr", "radarr service",
        )
        for malformed_service in malformed_services:
            with self.subTest(service=malformed_service):
                temporary, root = self.make_root()
                try:
                    malformed = {"bad": {
                        "Service": malformed_service,
                        "State": "running",
                        "Health": "healthy",
                    }}
                    runner = FakeRunner([malformed])
                    result = deploy_core(
                        root, runner=runner,
                        preflight_runner=lambda *args: self.fail("preflight must not run"),
                    )
                    self.assertEqual(result["status"], "live_state_failed")
                    self.assertFalse(any("up" in command for command in runner.commands))
                    self.assertFalse((root / ".homeflix" / "setup.json").exists())
                finally:
                    temporary.cleanup()

    def test_malformed_whitespace_state_never_reaches_preflight_or_compose_up(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        malformed = {"radarr": {"Service": " radarr ", "State": "   ", "Health": "healthy"}}
        runner = FakeRunner([malformed])
        result = deploy_core(
            root, runner=runner,
            preflight_runner=lambda *args: self.fail("preflight must not run"),
        )
        self.assertEqual(result["status"], "live_state_failed")
        self.assertFalse(any("up" in command for command in runner.commands))
        self.assertFalse((root / ".homeflix" / "setup.json").exists())

    def test_startup_process_failures_reconcile_safely_without_checkpoint(self) -> None:
        errors = (
            subprocess.TimeoutExpired(
                ["docker", "compose", "--env-file", "/private/.env"],
                300,
                output="10.0.0.8 password=hunter2",
            ),
            FileNotFoundError("docker missing at /private/path"),
            OSError("private 192.168.1.10 error"),
        )
        for startup_error in errors:
            with self.subTest(error=type(startup_error).__name__):
                temporary, root = self.make_root()
                try:
                    partial = records(**{
                        name: ("running", "healthy")
                        for name in CORE_SERVICES
                        if name != "radarr"
                    })
                    runner = FakeRunner([{}, partial], up_error=startup_error)
                    result = deploy_core(
                        root, runner=runner, preflight_runner=passing_preflight,
                        container_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                        http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                    )
                    self.assertEqual(result["status"], "startup_failed")
                    self.assertFalse(result["checkpoint_recorded"])
                    self.assertEqual(len(result["services"]), len(CORE_SERVICES))
                    services = {item["service"]: item for item in result["services"]}
                    self.assertTrue(services["jellyfin"]["ready"])
                    self.assertFalse(services["radarr"]["ready"])
                    self.assertFalse((root / ".homeflix" / "setup.json").exists())
                    rendered = json.dumps(result).casefold()
                    for forbidden in ("hunter2", "password", "10.0.0.8", "192.168.1.10", "/private", ".env"):
                        self.assertNotIn(forbidden, rendered)
                finally:
                    temporary.cleanup()

    def test_cli_sanitizes_expected_orchestration_exception(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        stdout, stderr = io.StringIO(), io.StringIO()
        failure = subprocess.TimeoutExpired(
            ["docker", "compose", "--env-file", "/private/.env"],
            300,
            output="password=hunter2 10.0.0.8",
        )
        with patch("scripts.homeflix_setup.cli.deploy_core", side_effect=failure):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(("--json", "deploy", "core"), repository_root=root)
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "deployment_refused")
        rendered = json.dumps(payload).casefold()
        for forbidden in ("hunter2", "password", "10.0.0.8", "/private", ".env"):
            self.assertNotIn(forbidden, rendered)

    def test_one_readiness_budget_prevents_per_service_timeout_accumulation(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        clock = FakeClock()
        runner = FakeRunner([ready_records()])

        def consume_http(url, **kwargs):
            clock.advance(kwargs["timeout"])
            return ReadinessResult(True, "ready")

        result = deploy_core(
            root, runner=runner, readiness_timeout=25, clock=clock,
            http_waiter=consume_http,
            preflight_runner=lambda *args: self.fail("healthy no-op must not preflight"),
        )
        self.assertEqual(result["status"], "already_ready")
        self.assertLessEqual(clock.now, 25.000001)
        self.assertFalse(any("up" in command for command in runner.commands))

        temporary2, root2 = self.make_root()
        self.addCleanup(temporary2.cleanup)
        clock2 = FakeClock()

        class BudgetRunner(FakeRunner):
            def __init__(self):
                super().__init__([{}])
                self.ps_timeouts: list[float] = []
                self.ps_calls = 0
            def run(self, argv, **kwargs):
                command = tuple(argv)
                if "ps" in command:
                    self.commands.append(command)
                    self.ps_calls += 1
                    requested = kwargs["timeout"]
                    self.ps_timeouts.append(requested)
                    clock2.advance(1 if self.ps_calls == 1 else requested)
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                return super().run(argv, **kwargs)

        runner2 = BudgetRunner()
        def bounded_container(service, probe, **kwargs):
            return wait_for_container(
                service, probe, timeout=kwargs["timeout"], interval=1,
                sleep=clock2.sleep, clock=clock2,
            )

        result2 = deploy_core(
            root2, runner=runner2, readiness_timeout=25, clock=clock2,
            preflight_runner=passing_preflight, container_waiter=bounded_container,
        )
        self.assertEqual(result2["status"], "partial_failure")
        self.assertLessEqual(clock2.now, 25.000001)
        self.assertGreater(len(runner2.ps_timeouts), 1)
        self.assertTrue(all(timeout <= 25 for timeout in runner2.ps_timeouts))

    def test_env_change_before_first_snapshot_refuses_before_preflight_or_up(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}])
        def mutate_before_snapshot(repository_root, config):
            (root / ".env").write_text(
                "COMPOSE_PROJECT_NAME=other\nDOMAIN=changed.test\n", encoding="utf-8"
            )
            return capture_deployment_snapshot(repository_root, config)
        result = deploy_core(
            root,
            runner=runner,
            snapshotter=mutate_before_snapshot,
            preflight_runner=lambda *args: self.fail("config drift must precede preflight"),
        )
        self.assertEqual(result["status"], "config_drift")
        self.assertEqual(runner.commands, [])
        self.assertFalse((root / ".homeflix" / "setup.json").exists())

    def test_preflight_drift_in_each_snapshot_field_prevents_compose_up(self) -> None:
        fields = ("environment", "compose", "override", "data_root_identity", "data_mount_record")
        for field in fields:
            with self.subTest(field=field):
                temporary, root = self.make_root()
                try:
                    runner = FakeRunner([{}])
                    config = EnvDocument.load(root / ".env")
                    baseline = capture_deployment_snapshot(root, config)
                    if field == "environment":
                        changed = replace(
                            baseline,
                            environment=replace(baseline.environment, sha256="b" * 64),
                        )
                    elif field == "compose":
                        changed = replace(
                            baseline, compose=replace(baseline.compose, inode=baseline.compose.inode + 1)
                        )
                    elif field == "override":
                        changed = replace(baseline, override=baseline.compose)
                    elif field == "data_root_identity":
                        changed = replace(baseline, data_root_identity=(4, 6))
                    else:
                        changed = replace(
                            baseline,
                            data_mount_record=("4:1", "/data", "ext4", "/dev/b"),
                        )
                    snapshots = iter((baseline, changed))
                    result = deploy_core(
                        root, runner=runner, preflight_runner=passing_preflight,
                        snapshotter=lambda *args: next(snapshots),
                    )
                    self.assertEqual(result["status"], "deployment_drift")
                    self.assertFalse(any("up" in command for command in runner.commands))
                finally:
                    temporary.cleanup()

        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        runner = FakeRunner([{}])
        baseline = capture_deployment_snapshot(root, EnvDocument.load(root / ".env"))
        calls = 0
        def disappearing(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("private vanished path")
            return baseline
        result = deploy_core(
            root, runner=runner, preflight_runner=passing_preflight,
            snapshotter=disappearing,
        )
        self.assertEqual(result["status"], "deployment_drift")
        self.assertNotIn("private", json.dumps(result))
        self.assertFalse(any("up" in command for command in runner.commands))

    def test_final_state_failure_preserves_last_known_diagnostics(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)

        class FinalFailureRunner(FakeRunner):
            def __init__(self):
                super().__init__([{}])
                self.ps_calls = 0
            def run(self, argv, **kwargs):
                command = tuple(argv)
                if "ps" in command:
                    self.commands.append(command)
                    self.ps_calls += 1
                    if self.ps_calls == 7:
                        raise OSError("private final state failure 10.0.0.8")
                    payload = [] if self.ps_calls == 1 else list(ready_records().values())
                    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
                return super().run(argv, **kwargs)

        runner = FinalFailureRunner()
        def container(service, probe, **kwargs):
            probe(kwargs["timeout"])
            return ReadinessResult(True, "ready")
        result = deploy_core(
            root, runner=runner, preflight_runner=passing_preflight,
            container_waiter=container,
            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
        )
        self.assertEqual(result["status"], "state_verification_failed")
        services = {item["service"]: item for item in result["services"]}
        self.assertEqual(services["jellyfin"]["current_state"], "running")
        self.assertTrue(services["jellyfin"]["ready"])
        self.assertFalse(result["checkpoint_recorded"])
        self.assertNotIn("10.0.0.8", json.dumps(result))

    def test_checkpoint_save_failures_return_verified_diagnostics(self) -> None:
        for initially_ready in (True, False):
            with self.subTest(initially_ready=initially_ready):
                temporary, root = self.make_root()
                try:
                    states = [ready_records()] if initially_ready else [{}, ready_records()]
                    runner = FakeRunner(states)
                    with patch("scripts.homeflix_setup.core.SetupState.save", side_effect=OSError("private path")):
                        result = deploy_core(
                            root, runner=runner, preflight_runner=passing_preflight,
                            container_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                        )
                    self.assertEqual(result["status"], "checkpoint_failed")
                    self.assertFalse(result["checkpoint_recorded"])
                    self.assertTrue(all(item["ready"] for item in result["services"]))
                    self.assertNotIn("private", json.dumps(result))
                finally:
                    temporary.cleanup()

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
        self.assertEqual(
            result["commands"][-1][-8:], ["up", "--detach", "--no-deps", *CORE_SERVICES]
        )
        self.assertFalse((root / ".homeflix" / "setup.json").exists())


if __name__ == "__main__":
    unittest.main()
