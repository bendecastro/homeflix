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
from urllib.parse import parse_qs, urlsplit

from scripts.homeflix_setup.api import ApiError, HttpResponse
from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.compose import CORE_SERVICES
from scripts.homeflix_setup.core import (
    ReadinessResult,
    _inspect_quicksync,
    _readiness_targets,
    capture_deployment_snapshot,
    configure_core,
    deploy_core,
    reconcile_core,
    verify_core,
    wait_for_container,
    wait_for_http,
)
from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.preflight import CheckResult, PreflightReport
from scripts.homeflix_setup.state import SetupState
from tests.test_contract import safe_mapping


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


# Jellyfin creates libraries that write artwork/NFO/trickplay beside the media files.
DEFAULT_LIBRARY_OPTIONS = {"SaveLocalMetadata": True, "MetadataSavers": ["Nfo"], "SaveTrickplayWithMedia": True}
COMPLIANT_LIBRARY_OPTIONS = {"SaveLocalMetadata": False, "MetadataSavers": [], "SaveTrickplayWithMedia": False}


class StatefulCoreFixture:
    key = "FIXTURE_API_KEY_1234567890ABCDE"

    def __init__(self, missing: str, *, clean: bool = False) -> None:
        if clean:
            self.containers = set()
            self.startup = False
            self.admin = False
            self.libraries = {}
            self.library_options = {}
            self.roots = {"radarr": [], "sonarr": []}
            self.media_ok = {"radarr": False, "sonarr": False}
            self.naming_ok = {"radarr": False, "sonarr": False}
            self.completed_ok = {"radarr": False, "sonarr": False}
            self.servers = {"radarr": [], "sonarr": []}
            self.initialized = False
            self.jellyfin_connected = False
            self.application_keys = []
            self.notifications = {"radarr": [], "sonarr": []}
        else:
            self.containers = set(CORE_SERVICES)
            if missing == "container": self.containers.remove("radarr")
            self.startup = missing != "jellyfin_startup"
            self.admin = self.startup
            self.libraries = {
                "Movies": ("movies", "/data/media/movies"),
                "Shows": ("tvshows", "/data/media/tv"),
                "Music": ("music", "/data/media/music"),
            }
            if missing == "jellyfin_library": self.libraries.pop("Music")
            self.library_options = {name: dict(COMPLIANT_LIBRARY_OPTIONS) for name in self.libraries}
            self.roots = {"radarr": ["/data/media/movies"], "sonarr": ["/data/media/tv"]}
            if missing == "arr_root": self.roots["radarr"] = []
            self.media_ok = {"radarr": missing != "arr_settings", "sonarr": True}
            self.naming_ok = {"radarr": missing != "arr_settings", "sonarr": True}
            self.completed_ok = {"radarr": True, "sonarr": True}
            self.servers = {"radarr": [self._server("radarr")], "sonarr": [self._server("sonarr")]}
            if missing == "jellyseerr_server": self.servers["sonarr"] = []
            self.initialized = True
            self.jellyfin_connected = True
            discovery_ready = missing != "jellyfin_startup"
            self.application_keys = [self._application_key()] if discovery_ready else []
            self.notifications = {
                "radarr": list(self._notifications("radarr")) if discovery_ready else [],
                "sonarr": list(self._notifications("sonarr")) if discovery_ready else [],
            }
        self.fail_next_sonarr_root = False
        self.commands = []
        self.api_calls = []
        self.configuration_mutations = []
        self.repository_root = None
        self.creations = {"account": 0, "library": 0, "library_options": 0, "root": 0, "settings": 0, "server": 0, "application_key": 0, "notification": 0}
        self.updates = {
            "media": {"radarr": 0, "sonarr": 0},
            "naming": {"radarr": 0, "sonarr": 0},
            "completed": {"radarr": 0, "sonarr": 0},
            "server": {"radarr": 0, "sonarr": 0},
        }

    @staticmethod
    def _application_key():
        return {
            "Id": "fixture-key-id",
            "AccessToken": StatefulCoreFixture.key,
            "AppName": "Radarr and Sonarr",
            "Name": "Radarr and Sonarr",
        }

    @classmethod
    def _notifications(cls, service):
        events = {
            "onGrab": False,
            "onDownload": service == "radarr",
            "onUpgrade": service == "radarr",
            "onRename": True,
            "onHealthIssue": False,
            "includeHealthWarnings": False,
            "onHealthRestored": False,
            "onApplicationUpdate": False,
            "onManualInteractionRequired": False,
        }
        if service == "radarr":
            events.update({
                "onMovieAdded": False, "onMovieDelete": False,
                "onMovieFileDelete": False, "onMovieFileDeleteForUpgrade": False,
            })
        else:
            events.update({
                "onImportComplete": True, "onSeriesAdd": False, "onSeriesDelete": False,
                "onEpisodeFileDelete": False, "onEpisodeFileDeleteForUpgrade": False,
            })
        return (
            {
                "id": 21,
                "implementation": "MediaBrowser",
                "configContract": "MediaBrowserSettings",
                "name": "Jellyfin",
                "fields": [
                    {"name": "host", "value": "jellyfin"},
                    {"name": "port", "value": 8096},
                    {"name": "useSsl", "value": False},
                    {"name": "urlBase", "value": ""},
                    {"name": "apiKey", "value": cls.key},
                    {"name": "notify", "value": False},
                    {"name": "updateLibrary", "value": True},
                ],
                **events,
            },
            {
                "id": 22,
                "implementation": "Webhook",
                "configContract": "WebhookSettings",
                "name": "Jellyfin library scan",
                "fields": [
                    {"name": "url", "value": "http://jellyfin:8096/Library/Refresh"},
                    {"name": "method", "value": 1},
                    {"name": "headers", "value": [{"key": "X-Emby-Token", "value": cls.key}]},
                ],
                **events,
            },
        )

    @staticmethod
    def _server(service):
        root = "/data/media/movies" if service == "radarr" else "/data/media/tv"
        result = {
            "id": 3 if service == "radarr" else 4, "name": service.capitalize(),
            "hostname": service, "port": 7878 if service == "radarr" else 8989,
            "apiKey": StatefulCoreFixture.key, "useSsl": False, "baseUrl": "",
            "activeProfileId": 19, "activeProfileName": "Fixture HD", "activeDirectory": root,
            "is4k": False, "minimumAvailability": "released", "isDefault": True,
            "externalUrl": "", "syncEnabled": True, "preventSearch": False, "tags": [],
        }
        if service == "sonarr": result.update({"enableSeasonFolders": True, "animeTags": []})
        return result

    def run(self, argv, **kwargs):
        command = tuple(argv)
        if command[:2] == ("docker", "info"):
            return subprocess.CompletedProcess(command, 0, "Server Version: fixture\n", "")
        if command[:2] == ("docker", "inspect"):
            data_root = ""
            if self.repository_root is not None:
                data_root = EnvDocument.load(self.repository_root / ".env").get("DATA_ROOT") or ""
            if any("Devices" in argument for argument in command):
                payload = "[]|true"
            else:
                payload = json.dumps([{"Type": "bind", "Source": data_root, "Destination": "/data"}])
            return subprocess.CompletedProcess(command, 0, payload, "")
        if len(command) < 10 or command[:3] != ("docker", "compose", "--project-directory"):
            raise AssertionError(f"unexpected fixture command {command}")
        root = Path(command[3]).resolve()
        expected_prefix = (
            "docker", "compose", "--project-directory", str(root),
            "--env-file", str(root / ".env"), "--project-name", "homeflix",
        )
        if command[:8] != expected_prefix or not (root / ".env").is_file() or not (root / "docker-compose.yml").is_file():
            raise AssertionError(f"unexpected fixture Compose context {command}")
        override = root / "docker-compose.override.yml"
        if override.exists() and not override.is_file():
            raise AssertionError("fixture Compose override is not a regular file")
        if self.repository_root is None:
            self.repository_root = root
        elif self.repository_root != root:
            raise AssertionError("fixture command changed repository root")
        operation = command[8:]
        self.commands.append(command)
        if operation == ("up", "--detach", "--no-deps", *CORE_SERVICES):
            self.containers.update(CORE_SERVICES)
            return subprocess.CompletedProcess(command, 0, "", "")
        if operation == ("config", "--format", "json"):
            return subprocess.CompletedProcess(command, 0, json.dumps(safe_mapping()), "")
        if operation[:2] == ("ps", "--quiet") and len(operation) == 3:
            return subprocess.CompletedProcess(command, 0, "a" * 64, "")
        if operation in {("ps", "--format", "json"), ("ps", "--all", "--format", "json")}:
            include_project = operation[1] == "--all"
            records = [
                {"Service": service, "State": "running", "Health": "healthy", **({"Project": "homeflix"} if include_project else {})}
                for service in sorted(self.containers)
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(records), "")
        raise AssertionError(f"unexpected fixture Compose operation {operation}")

    @staticmethod
    def _response(status, payload):
        return HttpResponse(status, json.dumps(payload).encode())

    def jellyfin(self, outgoing, timeout):
        split = urlsplit(outgoing.full_url)
        path = split.path
        call = ("jellyfin", outgoing.method, path)
        self.api_calls.append(call)
        if outgoing.method == "GET" and path == "/System/Info/Public" and not split.query:
            return self._response(200, {"StartupWizardCompleted": self.startup})
        if outgoing.method == "POST" and path == "/Users/AuthenticateByName" and not split.query:
            return self._response(200, {"AccessToken": "MEMORY_ONLY_TOKEN"}) if self.admin else self._response(401, {})
        if outgoing.method == "POST" and path == "/Startup/Configuration" and not split.query:
            self.configuration_mutations.append(call)
            return self._response(204, {})
        if outgoing.method == "POST" and path == "/Startup/User" and not split.query:
            self.admin = True; self.creations["account"] += 1
            self.configuration_mutations.append(call)
            return self._response(204, {})
        if outgoing.method == "POST" and path == "/Startup/RemoteAccess" and not split.query:
            self.configuration_mutations.append(call)
            return self._response(204, {})
        if outgoing.method == "POST" and path == "/Startup/Complete" and not split.query:
            self.startup = True
            self.configuration_mutations.append(call)
            return self._response(204, {})
        if outgoing.method == "POST" and path == "/Sessions/Logout" and not split.query:
            return self._response(204, {})
        if outgoing.method == "GET" and path == "/Library/VirtualFolders" and not split.query:
            payload = [
                {"Name": name, "CollectionType": kind, "Locations": [location],
                 "ItemId": f"fixture-{name.lower()}", "LibraryOptions": dict(self.library_options[name])}
                for name, (kind, location) in self.libraries.items()
            ]
            return self._response(200, payload)
        if outgoing.method == "POST" and path == "/Library/VirtualFolders/LibraryOptions" and not split.query:
            body = json.loads(outgoing.data)
            names = [name for name in self.libraries if f"fixture-{name.lower()}" == body["Id"]]
            if len(names) != 1:
                raise AssertionError("unexpected Jellyfin library options target")
            self.library_options[names[0]] = dict(body["LibraryOptions"])
            self.creations["library_options"] += 1
            self.configuration_mutations.append(call)
            return self._response(204, {})
        if outgoing.method == "POST" and path == "/Library/VirtualFolders":
            query = parse_qs(split.query)
            if set(query) != {"name", "collectionType", "paths", "refreshLibrary"} or query["refreshLibrary"] != ["false"]:
                raise AssertionError("unexpected Jellyfin library query")
            self.libraries[query["name"][0]] = (query["collectionType"][0], query["paths"][0]); self.creations["library"] += 1
            self.library_options[query["name"][0]] = dict(DEFAULT_LIBRARY_OPTIONS)
            self.configuration_mutations.append(call)
            return self._response(204, {})
        if outgoing.method == "GET" and path == "/Auth/Keys" and not split.query:
            return self._response(200, {"Items": list(self.application_keys), "TotalRecordCount": len(self.application_keys)})
        if outgoing.method == "POST" and path == "/Auth/Keys":
            query = parse_qs(split.query)
            if query.get("app") != ["Radarr and Sonarr"]:
                raise AssertionError("unexpected Jellyfin application key name")
            created = self._application_key()
            self.application_keys.append(created)
            self.creations["application_key"] += 1
            self.configuration_mutations.append(call)
            return self._response(204, {})
        raise AssertionError(f"unexpected Jellyfin fixture request {outgoing.method} {outgoing.full_url}")

    def arr(self, service):
        if service not in {"radarr", "sonarr"}:
            raise AssertionError("unexpected Arr fixture service")
        def transport(outgoing, timeout):
            split = urlsplit(outgoing.full_url)
            path = split.path
            call = (service, outgoing.method, path)
            self.api_calls.append(call)
            if split.query:
                raise AssertionError("unexpected Arr fixture query")
            root = "/data/media/movies" if service == "radarr" else "/data/media/tv"
            rename = "renameMovies" if service == "radarr" else "renameEpisodes"
            if outgoing.method == "GET" and path == "/api/v3/qualityprofile":
                return self._response(200, [{"id": 19, "name": "Fixture HD"}])
            if outgoing.method == "GET" and path == "/api/v3/rootfolder":
                return self._response(200, [{"id": index + 1, "path": value} for index, value in enumerate(self.roots[service])])
            if outgoing.method == "POST" and path == "/api/v3/rootfolder":
                if service == "sonarr" and self.fail_next_sonarr_root:
                    self.fail_next_sonarr_root = False
                    raise OSError("bounded sonarr root fixture interruption")
                self.roots[service].append(root); self.creations["root"] += 1
                self.configuration_mutations.append(call)
                return self._response(200, {"id": 8, "path": root})
            # The rename flag lives in config/naming; config/mediamanagement does
            # not carry it and silently drops it when sent there.
            if outgoing.method == "GET" and path == "/api/v3/config/naming":
                return self._response(200, {"id": 1, rename: self.naming_ok[service]})
            if outgoing.method == "PUT" and path == "/api/v3/config/naming":
                body = json.loads(outgoing.data)
                if body.get(rename) is not True:
                    raise AssertionError("naming update must enable renaming")
                self.naming_ok[service] = True
                self.updates["naming"][service] += 1
                self.configuration_mutations.append(call)
                return self._response(200, {})
            if outgoing.method == "GET" and path == "/api/v3/config/mediamanagement":
                return self._response(200, {"id": 1, "copyUsingHardlinks": self.media_ok[service]})
            if outgoing.method == "PUT" and path == "/api/v3/config/mediamanagement":
                if rename in json.loads(outgoing.data):
                    raise AssertionError("rename flag must not be sent to mediamanagement")
                self.media_ok[service] = True; self.creations["settings"] += 1
                self.updates["media"][service] += 1
                self.configuration_mutations.append(call)
                return self._response(200, {})
            if outgoing.method == "GET" and path == "/api/v3/config/downloadclient":
                return self._response(200, {"id": 2, "enableCompletedDownloadHandling": self.completed_ok[service]})
            if outgoing.method == "PUT" and path == "/api/v3/config/downloadclient":
                self.completed_ok[service] = True
                self.updates["completed"][service] += 1
                self.configuration_mutations.append(call)
                return self._response(200, {})
            if outgoing.method == "GET" and path == "/api/v3/notification":
                return self._response(200, list(self.notifications[service]))
            if outgoing.method == "POST" and path == "/api/v3/notification":
                payload = json.loads(outgoing.data)
                payload["id"] = 20 + len(self.notifications[service])
                self.notifications[service].append(payload)
                self.creations["notification"] += 1
                self.configuration_mutations.append(call)
                return self._response(200, payload)
            raise AssertionError(f"unexpected {service} fixture request {outgoing.method} {outgoing.full_url}")
        return transport

    def jellyseerr(self, outgoing, timeout):
        split = urlsplit(outgoing.full_url)
        path = split.path
        call = ("jellyseerr", outgoing.method, path)
        self.api_calls.append(call)
        if split.query:
            raise AssertionError("unexpected Jellyseerr fixture query")
        if outgoing.method == "GET" and path == "/api/v1/settings/public":
            return self._response(200, {"initialized": self.initialized})
        if outgoing.method == "POST" and path == "/api/v1/auth/jellyfin":
            self.jellyfin_connected = True
            self.configuration_mutations.append(call)
            return self._response(200, {})
        if outgoing.method == "GET" and path == "/api/v1/settings/jellyfin":
            if not self.jellyfin_connected:
                return self._response(200, {})
            return self._response(200, {"hostname": "jellyfin", "port": 8096, "useSsl": False, "urlBase": "", "serverType": 2})
        if outgoing.method == "POST" and path == "/api/v1/settings/initialize":
            self.initialized = True
            self.configuration_mutations.append(call)
            return self._response(200, {})
        for service in ("radarr", "sonarr"):
            base = f"/api/v1/settings/{service}"
            if outgoing.method == "POST" and path == base + "/test":
                return self._response(200, {"success": True})
            if outgoing.method == "GET" and path == base:
                return self._response(200, self.servers[service])
            if outgoing.method == "POST" and path == base:
                payload = json.loads(outgoing.data); payload["id"] = 9
                self.servers[service].append(payload); self.creations["server"] += 1
                self.configuration_mutations.append(call)
                return self._response(200, payload)
            expected_updates = {f"{base}/{server['id']}" for server in self.servers[service]}
            if outgoing.method == "PUT" and path in expected_updates:
                payload = json.loads(outgoing.data); self.servers[service] = [payload]
                self.updates["server"][service] += 1
                self.configuration_mutations.append(call)
                return self._response(200, payload)
        raise AssertionError(f"unexpected Jellyseerr fixture request {outgoing.method} {outgoing.full_url}")


class StatefulFixtureStrictnessTests(unittest.TestCase):
    def test_rejects_inexact_commands_and_unknown_api_routes(self):
        from urllib.request import Request

        fixture = StatefulCoreFixture("", clean=True)
        with self.assertRaises(AssertionError):
            fixture.run(("docker", "compose", "ps", "--format", "json"))
        for transport, request in (
            (fixture.jellyfin, Request("http://127.0.0.1/unknown", method="GET")),
            (fixture.arr("radarr"), Request("http://127.0.0.1/api/v3/rootfolder/extra", method="POST", data=b"{}")),
            (fixture.jellyseerr, Request("http://127.0.0.1/api/v1/settings/radarr/extra/bad", method="PUT", data=b"{}")),
        ):
            with self.subTest(url=request.full_url), self.assertRaises(AssertionError):
                transport(request, 1)


class CoreVerificationAndResumeTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        data = root / "data"
        (data / "torrents").mkdir(parents=True)
        (data / "media").mkdir(parents=True)
        (root / ".env").write_text(
            "COMPOSE_PROJECT_NAME=homeflix\nDOMAIN=fixture.test\n"
            "JELLYFIN_ADMIN_USER=fixture\nJELLYFIN_ADMIN_PASSWORD=NOT_REAL\n"
            "CONFIG_ROOT=/fixture/config\nPUID=1000\nQUALITY_PROFILE=Fixture HD\n"
            f"DATA_ROOT={data}\n",
            encoding="utf-8",
        )
        (root / ".env").chmod(0o600)
        return temporary, root

    def test_verify_uses_live_project_readiness_and_exact_api_inspection_without_state_write(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        state_path = root / ".homeflix" / "setup.json"
        SetupState(checkpoints={"core_verified": False}).save(state_path)
        before = state_path.read_bytes()

        class Runner:
            def run(self, argv, **kwargs):
                records = [
                    {"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"}
                    for service in CORE_SERVICES
                ]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
        class Jellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, username, password): return {"initialized": True, "libraries_exact": True}
        class Arr:
            def __init__(self, service, *args, **kwargs): self.service = service
            def inspect(self, profile, path):
                return {"profile_exact": True, "root_exact": True, "media_settings": True,
                        "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 4, "name": profile},
                        "runtime_root": {"id": 8, "path": path}}
        class Seerr:
            def __init__(self, **kwargs): pass
            def authorize(self, key): pass
            def inspect(self, runtime): return {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}
        with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
            result = verify_core(
                root, runner=Runner(), api_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                settings_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                quicksync_inspector=lambda *args: None,
                contract_evaluator=lambda _root: {"passed": True, "findings": []},
                mount_inspector=lambda *args, **kwargs: True,
                hardlink_prober=lambda *args, **kwargs: True,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(state_path.read_bytes(), before)
        self.assertNotIn("findings", result)
        self.assertEqual({item["domain"] for item in result["checks"]}, {
            "docker", "stack_contract", "compose_project", "service:traefik", "service:jellyfin",
            "service:jellyseerr", "service:radarr", "service:sonarr", "acquisition_absent",
            "mount", "hardlink_outcome", "jellyfin", "radarr", "sonarr", "jellyseerr", "quicksync",
        })
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "quicksync")["status"], "not-applicable")

    def test_quicksync_not_applicable_does_not_wash_mandatory_unknown(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        class Runner:
            def run(self, argv, **kwargs):
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
        with patch("scripts.homeflix_setup.core.JellyfinClient", side_effect=ValueError("state unavailable")):
            result = verify_core(
                root, runner=Runner(),
                http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                quicksync_inspector=lambda *args: None,
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "quicksync")["status"], "not-applicable")
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "jellyfin")["status"], "unknown")

    def test_verify_requires_rendered_and_live_quicksync_mapping_when_selected(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        class Runner:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                if "config" in argv:
                    payload = {"services": {"jellyfin": {"devices": [{"source": "/dev/dri", "target": "/dev/dri"}]}}}
                elif "--quiet" in argv:
                    return subprocess.CompletedProcess(argv, 0, "a" * 64 + "\n", "")
                elif argv[:2] == ("docker", "inspect"):
                    payload = [{"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri", "CgroupPermissions": "rwm"}]
                    return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + "|true", "")
                else:
                    payload = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        runner = Runner()
        class Jellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, *args): return {"initialized": True, "libraries_exact": True}
        class Arr:
            def __init__(self, service, *args, **kwargs): self.service = service
            def inspect(self, profile, path): return {"profile_exact": True, "root_exact": True, "media_settings": True, "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile}, "runtime_root": {"id": 2, "path": path}}
        class Seerr:
            def __init__(self, **kwargs): pass
            def authorize(self, key): pass
            def inspect(self, runtime): return {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}
        with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
            result = verify_core(root, runner=runner, api_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE", settings_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE", http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"))
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "quicksync")["status"], "pass")
        self.assertTrue(any("config" in command and "json" in command for command in runner.commands))
        self.assertTrue(any(command[:2] == ("docker", "inspect") for command in runner.commands))

    def test_quicksync_rendered_selection_is_structured_and_fails_closed(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        cases = (
            ([], None),
            ([{"source": "/dev/dri", "target": "/dev/dri"}], True),
            (["/dev/dri:/dev/dri"], True),
            ([{"source": "/dev/dri/renderD128", "target": "/dev/dri/renderD128"}], False),
            ("malformed", "error"),
        )
        for devices, expected in cases:
            with self.subTest(devices=devices):
                class Runner:
                    def run(self, argv, **kwargs):
                        if "config" in argv:
                            return subprocess.CompletedProcess(argv, 0, json.dumps({"services": {"jellyfin": {"devices": devices}}}), "")
                        if "--quiet" in argv:
                            return subprocess.CompletedProcess(argv, 0, "a" * 64, "")
                        live = [{"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri"}]
                        return subprocess.CompletedProcess(argv, 0, json.dumps(live) + "|true", "")
                if expected == "error":
                    with self.assertRaises(ValueError):
                        _inspect_quicksync(root, Runner(), "homeflix")
                else:
                    self.assertIs(_inspect_quicksync(root, Runner(), "homeflix"), expected)
        class LiveRunner:
            def __init__(self, live): self.live = live
            def run(self, argv, **kwargs):
                if "config" in argv:
                    devices = [{"source": "/dev/dri", "target": "/dev/dri"}]
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"services": {"jellyfin": {"devices": devices}}}), "")
                if "--quiet" in argv: return subprocess.CompletedProcess(argv, 0, "a" * 64, "")
                return subprocess.CompletedProcess(argv, 0, self.live, "")
        self.assertFalse(_inspect_quicksync(root, LiveRunner("[]|true"), "homeflix"))
        wrong = [{"PathOnHost": "/dev/dri/renderD128", "PathInContainer": "/dev/dri/renderD128"}]
        self.assertFalse(_inspect_quicksync(root, LiveRunner(json.dumps(wrong) + "|true"), "homeflix"))
        self.assertFalse(_inspect_quicksync(root, LiveRunner(json.dumps([{"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri"}]) + "|false"), "homeflix"))
        with self.assertRaises(ValueError):
            _inspect_quicksync(root, LiveRunner("malformed"), "homeflix")

    def test_hardlink_outcome_requires_shared_inode_and_cleans_up(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        data = root / "data"
        torrents = data / "torrents"
        media = data / "media"
        before = {path.name for path in torrents.iterdir()} | {path.name for path in media.iterdir()}

        class Runner:
            def run(self, argv, **kwargs):
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
        class Jellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, *args): return {"initialized": True, "libraries_exact": True}
        class Arr:
            def __init__(self, service, *args, **kwargs): self.service = service
            def inspect(self, profile, path):
                return {"profile_exact": True, "root_exact": True, "media_settings": True, "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile}, "runtime_root": {"id": 2, "path": path}}
        class Seerr:
            def __init__(self, **kwargs): pass
            def authorize(self, key): pass
            def inspect(self, runtime): return {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}

        def verify(**overrides):
            with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
                return verify_core(
                    root, runner=Runner(),
                    api_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                    settings_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                    http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                    quicksync_inspector=lambda *args: None,
                    contract_evaluator=lambda _root: {"passed": True, "findings": []},
                    mount_inspector=lambda *args, **kwargs: True,
                    **overrides,
                )

        result = verify()
        check = next(item for item in result["checks"] if item["domain"] == "hardlink_outcome")
        self.assertEqual(check["status"], "pass")
        after = {path.name for path in torrents.iterdir()} | {path.name for path in media.iterdir()}
        self.assertEqual(after, before)
        self.assertFalse(any(name.startswith(".homeflix-") for name in after))
        self.assertNotIn(str(data), json.dumps(result))

        insufficient = verify(hardlink_prober=lambda *args, **kwargs: False)
        self.assertFalse(insufficient["passed"])
        self.assertEqual(next(item for item in insufficient["checks"] if item["domain"] == "hardlink_outcome")["status"], "failure")

        unknown = verify(hardlink_prober=lambda *args, **kwargs: None)
        self.assertFalse(unknown["passed"])
        self.assertEqual(next(item for item in unknown["checks"] if item["domain"] == "hardlink_outcome")["status"], "unknown")

    def test_mount_domain_requires_live_data_root_identity(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        data = root / "data"
        class Runner:
            def run(self, argv, **kwargs):
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
        class Jellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, *args): return {"initialized": True, "libraries_exact": True}
        class Arr:
            def __init__(self, service, *args, **kwargs): self.service = service
            def inspect(self, profile, path):
                return {"profile_exact": True, "root_exact": True, "media_settings": True, "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile}, "runtime_root": {"id": 2, "path": path}}
        class Seerr:
            def __init__(self, **kwargs): pass
            def authorize(self, key): pass
            def inspect(self, runtime): return {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}

        def verify(**overrides):
            with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
                return verify_core(
                    root, runner=Runner(),
                    api_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                    settings_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                    http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                    quicksync_inspector=lambda *args: None,
                    contract_evaluator=lambda _root: {"passed": True, "findings": []},
                    **overrides,
                )

        missing = verify(mount_inspector=lambda *args, **kwargs: None)
        self.assertFalse(missing["passed"])
        self.assertIn(next(item for item in missing["checks"] if item["domain"] == "mount")["status"], {"failure", "unknown"})

        disagree = verify(mount_inspector=lambda *args, **kwargs: False)
        self.assertFalse(disagree["passed"])
        self.assertEqual(next(item for item in disagree["checks"] if item["domain"] == "mount")["status"], "failure")

        agree = verify(mount_inspector=lambda *args, **kwargs: True)
        self.assertEqual(next(item for item in agree["checks"] if item["domain"] == "mount")["status"], "pass")
        self.assertNotIn("127.0.0.1", json.dumps(agree))
        self.assertNotIn(str(data), json.dumps(agree))

    def test_injected_stack_contract_findings_fail_runtime_verify(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        class Runner:
            def run(self, argv, **kwargs):
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
        class Jellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, *args): return {"initialized": True, "libraries_exact": True}
        class Arr:
            def __init__(self, service, *args, **kwargs): self.service = service
            def inspect(self, profile, path):
                return {"profile_exact": True, "root_exact": True, "media_settings": True, "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile}, "runtime_root": {"id": 2, "path": path}}
        class Seerr:
            def __init__(self, **kwargs): pass
            def authorize(self, key): pass
            def inspect(self, runtime): return {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}

        def evaluator(_root):
            from scripts.homeflix_setup.contract import evaluate_stack_contract
            return evaluate_stack_contract({"services": {"prowlarr": {"network_mode": "bridge"}}})

        with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
            result = verify_core(
                root, runner=Runner(),
                api_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                settings_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                quicksync_inspector=lambda *args: None,
                contract_evaluator=evaluator,
            )
        self.assertFalse(result["passed"])
        self.assertNotIn("findings", result)
        contract = next(item for item in result["checks"] if item["domain"] == "stack_contract")
        self.assertEqual(contract["status"], "failure")
        self.assertIn("vpn_namespace", contract["reason"])
        rendered = json.dumps(result)
        for private in ("127.0.0.1", "/root", "FIXTURE_API_KEY_1234567890ABCDE"):
            self.assertNotIn(private, rendered)

    def test_docker_daemon_failure_is_distinct_from_empty_inventory(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)

        class DaemonDown:
            def run(self, argv, **kwargs):
                if tuple(argv)[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
                raise RuntimeError("Compose inventory is unavailable")

        result = verify_core(
            root, runner=DaemonDown(),
            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
            quicksync_inspector=lambda *args: None,
        )
        self.assertFalse(result["passed"])
        docker = next(item for item in result["checks"] if item["domain"] == "docker")
        self.assertIn(docker["status"], {"failure", "unknown"})
        acquisition = next(item for item in result["checks"] if item["domain"] == "acquisition_absent")
        self.assertEqual(acquisition["status"], "unknown")
        self.assertNotEqual(acquisition["status"], "pass")
        rendered = json.dumps(result)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn("/root", rendered)

        class EmptyWhenHealthy:
            def run(self, argv, **kwargs):
                if tuple(argv)[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                return subprocess.CompletedProcess(argv, 0, "[]", "")

        empty = verify_core(
            root, runner=EmptyWhenHealthy(),
            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
            quicksync_inspector=lambda *args: None,
        )
        self.assertFalse(empty["passed"])
        self.assertEqual(next(item for item in empty["checks"] if item["domain"] == "docker")["status"], "pass")
        self.assertEqual(next(item for item in empty["checks"] if item["domain"] == "acquisition_absent")["status"], "pass")
        self.assertEqual(next(item for item in empty["checks"] if item["domain"] == "compose_project")["status"], "failure")

    def test_verify_warns_for_classified_non_core_without_failing(self):
        for extra in ("gluetun", "qbittorrent", "nzbget", "prowlarr", "lidarr", "bazarr"):
            with self.subTest(service=extra):
                temporary, root = self.make_root()
                try:
                    class Runner:
                        def run(self, argv, **kwargs):
                            records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in (*CORE_SERVICES, extra)]
                            return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
                    class Jellyfin:
                        def __init__(self, **kwargs): pass
                        def inspect(self, *args): return {"initialized": True, "libraries_exact": True}
                    class Arr:
                        def __init__(self, service, *args, **kwargs): self.service = service
                        def inspect(self, profile, path):
                            return {"profile_exact": True, "root_exact": True, "media_settings": True, "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile}, "runtime_root": {"id": 2, "path": path}}
                    class Seerr:
                        def __init__(self, **kwargs): pass
                        def authorize(self, key): pass
                        def inspect(self, runtime): return {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}
                    with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
                        result = verify_core(
                            root, runner=Runner(),
                            api_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                            settings_key_reader=lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
                            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
                            quicksync_inspector=lambda *args: None,
                            contract_evaluator=lambda _root: {"passed": True, "findings": []},
                            mount_inspector=lambda *args, **kwargs: True,
                            hardlink_prober=lambda *args, **kwargs: True,
                        )
                    check = next(item for item in result["checks"] if item["domain"] == "acquisition_absent")
                    self.assertEqual(check["status"], "warning")
                    self.assertIn(extra, check["reason"])
                    self.assertTrue(result["passed"], result)
                finally:
                    temporary.cleanup()

    def test_verify_uses_one_deadline_and_skips_later_calls_when_exhausted(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        clock = FakeClock(); timeouts = []; http_calls = []
        class Runner:
            def run(self, argv, **kwargs):
                timeouts.append(kwargs["timeout"])
                clock.advance(kwargs["timeout"])
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
        with patch("scripts.homeflix_setup.core.JellyfinClient", side_effect=AssertionError("API must be skipped")):
            result = verify_core(
                root, runner=Runner(), readiness_timeout=5, clock=clock,
                http_waiter=lambda *args, **kwargs: http_calls.append(args) or ReadinessResult(True, "ready"),
                quicksync_inspector=lambda *args: self.fail("QuickSync must be skipped"),
            )
        self.assertFalse(result["passed"])
        self.assertEqual(clock.now, 5)
        self.assertEqual(timeouts, [5])
        self.assertEqual(http_calls, [])
        jellyfin = next(item for item in result["checks"] if item["domain"] == "jellyfin")
        self.assertEqual(jellyfin["status"], "unknown")
        self.assertIn("time budget exhausted", jellyfin["reason"])
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "quicksync")["status"], "unknown")

    def test_reconcile_deadline_does_not_reset_between_phases(self):
        clock = FakeClock()
        def consume(*args, **kwargs):
            self.assertEqual(kwargs["deadline"], 5)
            clock.advance(5)
            return {"status": "already_ready"}
        with patch("scripts.homeflix_setup.core.deploy_core", side_effect=consume), patch("scripts.homeflix_setup.core.configure_core", side_effect=AssertionError("API configuration must be skipped")):
            result = reconcile_core(".", readiness_timeout=5, clock=clock)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(clock.now, 5)

    def test_reconcile_maps_only_outer_deadline_api_error_to_timeout(self):
        for error_code, expected in (("deadline_exhausted", "timeout"), ("transport_error", "raise")):
            with self.subTest(error_code=error_code), patch("scripts.homeflix_setup.core.deploy_core", return_value={"status": "already_ready"}), patch("scripts.homeflix_setup.core.configure_core", side_effect=ApiError("jellyfin", "initialize", None, error_code)):
                if expected == "raise":
                    with self.assertRaises(ApiError) as raised:
                        reconcile_core(".")
                    self.assertEqual(raised.exception.code, "transport_error")
                else:
                    self.assertEqual(reconcile_core(".")["status"], "timeout")

    def test_reconcile_continues_live_api_work_after_checkpoint_write_failure(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        with (root / ".env").open("a", encoding="utf-8") as environment:
            environment.write(f"DATA_ROOT={root}\n")
        (root / "torrents").mkdir()
        (root / "media").mkdir()
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        fixture = StatefulCoreFixture("jellyfin_library")
        with patch("scripts.homeflix_setup.core.SetupState.save", side_effect=OSError("private checkpoint path")):
            result = reconcile_core(
                root, runner=fixture,
                transports={"jellyfin": fixture.jellyfin, "radarr": fixture.arr("radarr"), "sonarr": fixture.arr("sonarr"), "jellyseerr": fixture.jellyseerr},
                api_key_reader=lambda *args: fixture.key, settings_key_reader=lambda *args: fixture.key,
                preflight_runner=passing_preflight,
                http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
            )
        self.assertEqual(result["status"], "checkpoint_failed")
        self.assertEqual(set(fixture.libraries), {"Movies", "Shows", "Music"})
        self.assertIn("verify", result)
        self.assertNotIn("private", json.dumps(result))

    def test_verify_fail_closed_aggregation_is_table_driven_and_sanitized(self):
        scenarios = (
            ("wrong_project", "compose_project", "failure"),
            ("missing_service", "service:sonarr", "failure"),
            ("unhealthy_service", "service:radarr", "failure"),
            ("malformed_inventory", "compose_project", "unknown"),
            ("jellyfin_false", "jellyfin", "failure"),
            ("jellyfin_wrong", "jellyfin", "unknown"),
            ("radarr_false", "radarr", "failure"),
            ("radarr_discovery", "radarr", "failure"),
            ("sonarr_wrong", "sonarr", "unknown"),
            ("jellyseerr_false", "jellyseerr", "failure"),
            ("jellyseerr_wrong", "jellyseerr", "unknown"),
            ("acquisition", "acquisition_absent", "warning"),
            ("acquisition_malformed", "acquisition_absent", "unknown"),
            ("quicksync_invalid", "quicksync", "failure"),
            ("quicksync_unknown", "quicksync", "unknown"),
        )
        for scenario, domain, expected_status in scenarios:
            with self.subTest(scenario=scenario):
                temporary, root = self.make_root()
                try:
                    class Runner:
                        def run(self, argv, **kwargs):
                            project = "other" if scenario == "wrong_project" else "homeflix"
                            records = []
                            for service in CORE_SERVICES:
                                if scenario == "missing_service" and service == "sonarr": continue
                                state = "broken" if scenario == "malformed_inventory" and service == "radarr" else "running"
                                health = "unhealthy" if scenario == "unhealthy_service" and service == "radarr" else "healthy"
                                records.append({"Service": service, "State": state, "Health": health, "Project": project})
                            if scenario == "acquisition": records.append({"Service": "bazarr", "State": "created", "Health": "", "Project": "homeflix"})
                            if scenario == "acquisition_malformed": records.append({"Service": "bad service", "State": "created", "Health": "", "Project": "homeflix"})
                            return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")
                    class Jellyfin:
                        def __init__(self, **kwargs): pass
                        def inspect(self, *args):
                            if scenario == "jellyfin_false": return {"initialized": False, "libraries_exact": True}
                            if scenario == "jellyfin_wrong": return {"initialized": "yes", "libraries_exact": True}
                            return {"initialized": True, "libraries_exact": True}
                    class Arr:
                        def __init__(self, service, *args, **kwargs): self.service = service
                        def inspect(self, profile, path):
                            result = {"profile_exact": True, "root_exact": True, "media_settings": True, "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile}, "runtime_root": {"id": 2, "path": path}}
                            if scenario == "radarr_false" and self.service == "radarr": result["root_exact"] = False
                            if scenario == "radarr_discovery" and self.service == "radarr": result["refresh_connection_exact"] = False
                            if scenario == "sonarr_wrong" and self.service == "sonarr": result.pop("media_settings")
                            return result
                    class Seerr:
                        def __init__(self, **kwargs): pass
                        def authorize(self, key): pass
                        def inspect(self, runtime):
                            if scenario == "jellyseerr_wrong": return {"initialized": "yes"}
                            return {"initialized": scenario != "jellyseerr_false", "jellyfin": True, "radarr": True, "sonarr": True}
                    def quick(*args):
                        if scenario == "quicksync_unknown": raise ValueError("private /root 10.0.0.1")
                        return False if scenario == "quicksync_invalid" else None
                    with patch("scripts.homeflix_setup.core.JellyfinClient", Jellyfin), patch("scripts.homeflix_setup.core.ArrClient", Arr), patch("scripts.homeflix_setup.core.JellyseerrClient", Seerr):
                        result = verify_core(root, runner=Runner(), api_key_reader=lambda *args: StatefulCoreFixture.key, settings_key_reader=lambda *args: StatefulCoreFixture.key, http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"), quicksync_inspector=quick)
                    check = next(item for item in result["checks"] if item["domain"] == domain)
                    self.assertEqual(check["status"], expected_status)
                    if scenario != "acquisition":
                        self.assertFalse(result["passed"])
                    rendered = json.dumps(result)
                    for private in ("/root", "10.0.0.1"):
                        self.assertNotIn(private, rendered)
                finally:
                    temporary.cleanup()

    def test_reconcile_repairs_live_drift_from_every_checkpoint_without_duplicates(self):
        cases = (
            ({}, "container", {}),
            ({"configured": True}, "jellyfin_startup", {"account": 1, "application_key": 1, "notification": 4}),
            ({"core_containers_started": True}, "jellyfin_library", {"library": 1, "library_options": 1}),
            ({"core_api_configured": True}, "arr_root", {"root": 1}),
            ({"core_verified": True}, "arr_settings", {"settings": 1}),
            ({"configured": True, "core_verified": True}, "jellyseerr_server", {"server": 1}),
        )
        for initial, missing, created_kinds in cases:
            with self.subTest(initial=initial, missing=missing):
                temporary, root = self.make_root()
                try:
                    with (root / ".env").open("a", encoding="utf-8") as environment:
                        environment.write(f"DATA_ROOT={root}\n")
                    (root / "torrents").mkdir(exist_ok=True)
                    (root / "media").mkdir(exist_ok=True)
                    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
                    state_path = root / ".homeflix" / "setup.json"
                    if missing == "arr_settings":
                        state_path.parent.mkdir(parents=True)
                        state_path.write_text("corrupt checkpoint state", encoding="utf-8")
                    else:
                        SetupState(checkpoints=dict(initial)).save(state_path)
                    fixture = StatefulCoreFixture(missing)
                    kwargs = {
                        "runner": fixture,
                        "transports": {"jellyfin": fixture.jellyfin, "radarr": fixture.arr("radarr"), "sonarr": fixture.arr("sonarr"), "jellyseerr": fixture.jellyseerr},
                        "api_key_reader": lambda *args: fixture.key,
                        "settings_key_reader": lambda *args: fixture.key,
                        "preflight_runner": passing_preflight,
                        "container_waiter": lambda *args, **kwargs: ReadinessResult(True, "ready"),
                        "http_waiter": lambda *args, **kwargs: ReadinessResult(True, "ready"),
                    }
                    first = reconcile_core(root, **kwargs)
                    counts_after_first = dict(fixture.creations)
                    second = reconcile_core(root, **kwargs)
                    self.assertEqual(first["status"], "verified")
                    self.assertEqual(second["status"], "verified")
                    expected = {name: 0 for name in fixture.creations}
                    expected.update(created_kinds)
                    self.assertEqual(counts_after_first, expected)
                    self.assertEqual(fixture.creations, expected)
                    self.assertEqual(set(fixture.containers), set(CORE_SERVICES))
                    self.assertEqual(set(fixture.libraries), {"Movies", "Shows", "Music"})
                    self.assertEqual(fixture.roots["radarr"], ["/data/media/movies"])
                    self.assertEqual(fixture.roots["sonarr"], ["/data/media/tv"])
                    self.assertEqual(len(fixture.servers["radarr"]), 1)
                    self.assertEqual(len(fixture.servers["sonarr"]), 1)
                    self.assertEqual(len(fixture.notifications["radarr"]), 2)
                    self.assertEqual(len(fixture.notifications["sonarr"]), 2)
                    up_calls = sum("up" in command for command in fixture.commands)
                    self.assertEqual(up_calls, 1 if missing == "container" else 0)
                finally:
                    temporary.cleanup()

    def test_initialize_accepts_classified_extras_and_does_not_start_them(self):
        temporary, root = self.make_root(); self.addCleanup(temporary.cleanup)
        with (root / ".env").open("a", encoding="utf-8") as environment:
            environment.write(f"DATA_ROOT={root}\n")
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        fixture = StatefulCoreFixture(None)
        fixture.containers.update({"gluetun", "qbittorrent", "deunhealth"})
        result = configure_core(
            root,
            runner=fixture,
            transports={
                "jellyfin": fixture.jellyfin,
                "radarr": fixture.arr("radarr"),
                "sonarr": fixture.arr("sonarr"),
                "jellyseerr": fixture.jellyseerr,
            },
            api_key_reader=lambda *args: fixture.key,
            settings_key_reader=lambda *args: fixture.key,
            http_waiter=lambda *args, **kwargs: ReadinessResult(True, "ready"),
        )
        self.assertIn(result.get("status"), {"configured", "unchanged", None})
        self.assertTrue(any(key.endswith("_changed") or key in {"jellyfin", "radarr", "sonarr"} for key in result))
        rendered = json.dumps(result)
        self.assertNotIn("gluetun", " ".join(" ".join(command) for command in fixture.commands if "up" in command))
        for command in fixture.commands:
            if "up" in command:
                self.assertNotIn("gluetun", command)
                self.assertNotIn("qbittorrent", command)
                self.assertNotIn("deunhealth", command)
        self.assertNotIn("NOT_REAL", rendered)


class FakeRuntimeVerificationTests(unittest.TestCase):
    """Read-only verify_core journeys through an injectable fake runner."""

    MANDATORY = {
        "docker", "stack_contract", "compose_project",
        "service:traefik", "service:jellyfin", "service:jellyseerr", "service:radarr", "service:sonarr",
        "acquisition_absent", "mount", "hardlink_outcome",
        "jellyfin", "radarr", "sonarr", "jellyseerr",
    }
    PRIVATE = ("127.0.0.1", "FIXTURE_API_KEY_1234567890ABCDE", "NOT_REAL", "/root")

    def make_root(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        data = root / "data"
        (data / "torrents").mkdir(parents=True)
        (data / "media").mkdir(parents=True)
        (root / ".env").write_text(
            "COMPOSE_PROJECT_NAME=homeflix\nDOMAIN=fixture.test\n"
            "JELLYFIN_ADMIN_USER=fixture\nJELLYFIN_ADMIN_PASSWORD=NOT_REAL\n"
            "CONFIG_ROOT=/fixture/config\nPUID=1000\nQUALITY_PROFILE=Fixture HD\n"
            f"DATA_ROOT={data}\n",
            encoding="utf-8",
        )
        (root / ".env").chmod(0o600)
        return temporary, root, data

    def clients(self, *, jellyfin=None, arr=None, seerr=None):
        class Jellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, *args):
                return jellyfin or {"initialized": True, "libraries_exact": True}
        class Arr:
            def __init__(self, service, *args, **kwargs): self.service = service
            def inspect(self, profile, path):
                payload = arr or {
                    "profile_exact": True, "root_exact": True, "media_settings": True,
                    "completed_handling": True, "targeted_connection_exact": True, "refresh_connection_exact": True, "runtime_profile": {"id": 1, "name": profile},
                    "runtime_root": {"id": 2, "path": path},
                }
                return dict(payload)
        class Seerr:
            def __init__(self, **kwargs): pass
            def authorize(self, key): pass
            def inspect(self, runtime):
                return seerr or {"initialized": True, "jellyfin": True, "radarr": True, "sonarr": True}
        return Jellyfin, Arr, Seerr

    def verify(self, root, runner, **overrides):
        jellyfin, arr, seerr = self.clients()
        kwargs = {
            "runner": runner,
            "api_key_reader": lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
            "settings_key_reader": lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
            "http_waiter": lambda *args, **kwargs: ReadinessResult(True, "ready"),
            "quicksync_inspector": lambda *args: None,
            "contract_evaluator": lambda _root: {"passed": True, "findings": []},
            "mount_inspector": lambda *args, **kwargs: True,
            "hardlink_prober": lambda *args, **kwargs: True,
        }
        kwargs.update(overrides)
        with patch("scripts.homeflix_setup.core.JellyfinClient", jellyfin), patch("scripts.homeflix_setup.core.ArrClient", arr), patch("scripts.homeflix_setup.core.JellyseerrClient", seerr):
            return verify_core(root, **kwargs)

    def assert_secret_free(self, result, extra=()):
        rendered = json.dumps(result)
        for private in (*self.PRIVATE, *extra):
            self.assertNotIn(private, rendered)

    def test_all_skip_unknown_fails_and_is_non_destructive(self):
        temporary, root, data = self.make_root(); self.addCleanup(temporary.cleanup)
        state_path = root / ".homeflix" / "setup.json"
        SetupState(checkpoints={"core_verified": False}).save(state_path)
        before = state_path.read_bytes()
        probe_before = {path.name for path in (data / "torrents").iterdir()} | {path.name for path in (data / "media").iterdir()}

        class Skipper:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                raise RuntimeError("observation skipped")

        result = self.verify(
            root, Skipper(),
            docker_inspector=lambda *args, **kwargs: None,
            contract_evaluator=lambda _root: None,
            mount_inspector=lambda *args, **kwargs: None,
            hardlink_prober=lambda *args, **kwargs: None,
            http_waiter=lambda *args, **kwargs: ReadinessResult(False, "skipped"),
            quicksync_inspector=lambda *args: None,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "failed")
        by_domain = {item["domain"]: item["status"] for item in result["checks"]}
        self.assertTrue(self.MANDATORY.issubset(by_domain))
        for domain in self.MANDATORY:
            self.assertEqual(by_domain[domain], "unknown", domain)
        self.assertIn(by_domain.get("quicksync"), {"unknown", "not-applicable"})
        self.assertEqual(state_path.read_bytes(), before)
        after = {path.name for path in (data / "torrents").iterdir()} | {path.name for path in (data / "media").iterdir()}
        self.assertEqual(after, probe_before)
        self.assert_secret_free(result)

    def test_partial_observation_fails_closed(self):
        temporary, root, _data = self.make_root(); self.addCleanup(temporary.cleanup)

        class Partial:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                if tuple(argv)[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        runner = Partial()
        result = self.verify(
            root, runner,
            mount_inspector=lambda *args, **kwargs: True,
            hardlink_prober=lambda *args, **kwargs: None,
        )
        self.assertFalse(result["passed"])
        by_domain = {item["domain"]: item["status"] for item in result["checks"]}
        self.assertEqual(by_domain["docker"], "pass")
        self.assertEqual(by_domain["mount"], "pass")
        self.assertEqual(by_domain["hardlink_outcome"], "unknown")
        self.assertEqual(by_domain["quicksync"], "not-applicable")
        self.assertTrue(all("up" not in command and "down" not in command and "restart" not in command for command in runner.commands))
        self.assert_secret_free(result)

    def test_timeout_skips_later_mandatory_domains(self):
        temporary, root, _data = self.make_root(); self.addCleanup(temporary.cleanup)
        clock = FakeClock()

        class Slow:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                clock.advance(kwargs.get("timeout") or 0)
                if tuple(argv)[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        result = self.verify(root, Slow(), readiness_timeout=5, clock=clock)
        self.assertFalse(result["passed"])
        self.assertEqual(clock.now, 5)
        by_domain = {item["domain"]: item["status"] for item in result["checks"]}
        self.assertEqual(by_domain["docker"], "pass")
        self.assertEqual(by_domain["jellyfin"], "unknown")
        self.assertIn("time budget exhausted", next(item["reason"] for item in result["checks"] if item["domain"] == "jellyfin"))
        self.assertTrue(self.MANDATORY.issubset(by_domain))
        self.assert_secret_free(result)

    def test_malformed_state_is_unknown_and_fails(self):
        temporary, root, _data = self.make_root(); self.addCleanup(temporary.cleanup)

        class Malformed:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                if tuple(argv)[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"Service": "radarr", "State": "broken", "Health": "healthy", "Project": "homeflix"}]), "")

        runner = Malformed()
        result = self.verify(root, runner)
        self.assertFalse(result["passed"])
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "compose_project")["status"], "unknown")
        self.assertTrue(all("up" not in command and "down" not in command and "restart" not in command for command in runner.commands))
        self.assert_secret_free(result)
        for item in result["checks"]:
            self.assertNotIn("up", item["reason"])

    def test_no_change_success_is_read_only(self):
        temporary, root, data = self.make_root(); self.addCleanup(temporary.cleanup)
        state_path = root / ".homeflix" / "setup.json"
        SetupState(checkpoints={"core_verified": False}).save(state_path)
        before = state_path.read_bytes()
        env_before = (root / ".env").read_bytes()

        class Healthy:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                command = tuple(argv)
                if command[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                if "up" in command or "down" in command or "restart" in command:
                    raise AssertionError(f"verify must not mutate compose: {command}")
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        runner = Healthy()
        result = self.verify(root, runner)
        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("findings", result)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual((root / ".env").read_bytes(), env_before)
        self.assertTrue(all(
            "up" not in command and "down" not in command and "restart" not in command
            for command in runner.commands
        ))
        leftover = [path.name for path in (data / "torrents").iterdir()] + [path.name for path in (data / "media").iterdir()]
        self.assertFalse(any(name.startswith(".homeflix-") for name in leftover))
        by_domain = {item["domain"]: item["status"] for item in result["checks"]}
        self.assertTrue(self.MANDATORY.issubset(by_domain))
        self.assertEqual(by_domain["quicksync"], "not-applicable")
        self.assertTrue(all(status in {"pass", "not-applicable"} for status in by_domain.values()))
        self.assert_secret_free(result, extra=(str(data),))

    def test_existing_stack_with_classified_extras_verifies(self):
        temporary, root, _data = self.make_root(); self.addCleanup(temporary.cleanup)
        extras = ("gluetun", "qbittorrent", "prowlarr", "bazarr", "deunhealth", "glances", "watchtower")

        class Existing:
            def __init__(self): self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(tuple(argv))
                command = tuple(argv)
                if command[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                if "up" in command or "down" in command or "restart" in command:
                    raise AssertionError(f"verify must not mutate compose: {command}")
                records = [
                    {"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"}
                    for service in (*CORE_SERVICES, *extras)
                ]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        result = self.verify(root, Existing())
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["status"], "verified")
        acquisition = next(item for item in result["checks"] if item["domain"] == "acquisition_absent")
        self.assertEqual(acquisition["status"], "warning")
        self.assertTrue(any(name in acquisition["reason"] for name in extras))
        self.assert_secret_free(result)

    def test_discover_probe_observes_refresh_and_removes_only_the_probe(self):
        temporary, root, data = self.make_root(); self.addCleanup(temporary.cleanup)
        movies = data / "media" / "movies"
        movies.mkdir(parents=True)
        sibling = movies / "keep-me"
        sibling.mkdir()
        seen: dict[str, object] = {"tokens": [], "paths": []}

        class Healthy:
            def run(self, argv, **kwargs):
                command = tuple(argv)
                if command[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                records = [
                    {"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"}
                    for service in CORE_SERVICES
                ]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        class ProbeJellyfin:
            def __init__(self, **kwargs): pass
            def inspect(self, *args):
                return {"initialized": True, "libraries_exact": True}
            def prove_unconditional_discovery(self, username, password, token):
                seen["tokens"].append(token)
                matches = [path for path in movies.iterdir() if path.name == token]
                seen["paths"].append([path.name for path in movies.iterdir()])
                return bool(matches)

        jellyfin, arr, seerr = self.clients()
        kwargs = {
            "runner": Healthy(),
            "api_key_reader": lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
            "settings_key_reader": lambda *args: "FIXTURE_API_KEY_1234567890ABCDE",
            "http_waiter": lambda *args, **kwargs: ReadinessResult(True, "ready"),
            "quicksync_inspector": lambda *args: None,
            "contract_evaluator": lambda _root: {"passed": True, "findings": []},
            "mount_inspector": lambda *args, **kwargs: True,
            "hardlink_prober": lambda *args, **kwargs: True,
            "discover_probe": True,
        }
        with patch("scripts.homeflix_setup.core.JellyfinClient", ProbeJellyfin), patch("scripts.homeflix_setup.core.ArrClient", arr), patch("scripts.homeflix_setup.core.JellyseerrClient", seerr):
            result = verify_core(root, **kwargs)
        self.assertTrue(result["passed"], result)
        probe = next(item for item in result["checks"] if item["domain"] == "discovery_probe")
        self.assertEqual(probe["status"], "pass")
        self.assertEqual(seen["tokens"], [seen["tokens"][0]])
        self.assertTrue(str(seen["tokens"][0]).startswith("HomeflixDiscoveryProbe-"))
        self.assertTrue(sibling.exists())
        leftover = [path.name for path in movies.iterdir()]
        self.assertEqual(leftover, ["keep-me"])
        rendered = json.dumps(result)
        self.assertNotIn(seen["tokens"][0], rendered)
        self.assertNotIn(str(data), rendered)
        self.assertNotIn("keep-me", rendered)

    def test_unknown_project_service_still_fails_verify(self):
        temporary, root, _data = self.make_root(); self.addCleanup(temporary.cleanup)

        class Unexpected:
            def run(self, argv, **kwargs):
                command = tuple(argv)
                if command[:2] == ("docker", "info"):
                    return subprocess.CompletedProcess(argv, 0, "Server Version: fixture\n", "")
                records = [
                    {"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"}
                    for service in (*CORE_SERVICES, "not-a-homeflix-service")
                ]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        result = self.verify(root, Unexpected())
        self.assertFalse(result["passed"])
        self.assertEqual(next(item for item in result["checks"] if item["domain"] == "compose_project")["status"], "unknown")
        self.assert_secret_free(result)

    def test_running_healthy_but_waiter_not_ready_is_failure(self):
        temporary, root, _data = self.make_root(); self.addCleanup(temporary.cleanup)

        class Healthy:
            def run(self, argv, **kwargs):
                records = [{"Service": service, "State": "running", "Health": "healthy", "Project": "homeflix"} for service in CORE_SERVICES]
                return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        result = self.verify(
            root, Healthy(),
            http_waiter=lambda *args, **kwargs: ReadinessResult(False, "HTTP readiness timed out"),
        )
        self.assertFalse(result["passed"])
        for service in CORE_SERVICES:
            check = next(item for item in result["checks"] if item["domain"] == f"service:{service}")
            self.assertEqual(check["status"], "failure")
        self.assert_secret_free(result)


if __name__ == "__main__":
    unittest.main()
