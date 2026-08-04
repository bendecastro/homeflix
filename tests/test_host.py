from __future__ import annotations

from contextlib import redirect_stdout
import dataclasses
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.discover import discover_host
from scripts.homeflix_setup.host import apply_host_preparation, plan_host_preparation
from tests.test_discover import FixtureRunner


def unwrapped(argv: tuple[str, ...]) -> tuple[str, ...]:
    marker = "--kill-after=10s"
    if marker in argv:
        return argv[argv.index(marker) + 2 :]
    return argv


class RecordingRunner(FixtureRunner):
    def __init__(self, fixture_name: str) -> None:
        super().__init__(fixture_name)
        self.mutations: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.failure: tuple[str, ...] | None = None
        self.fail_mutation_index: int | None = None
        self.exception: BaseException | None = None

    def run(self, argv, *, input_text=None, timeout=None, **kwargs):
        key = " ".join(argv)
        if key in self.commands:
            return super().run(argv, timeout=timeout, **kwargs)
        command = tuple(argv)
        self.mutations.append((command, input_text, timeout))
        actual = unwrapped(command)
        should_fail = (
            self.fail_mutation_index is not None
            and len(self.mutations) - 1 == self.fail_mutation_index
        ) or (self.failure is not None and actual[: len(self.failure)] == self.failure)
        if should_fail:
            if self.exception is not None:
                raise self.exception
            return subprocess.CompletedProcess(list(argv), 9, "private stdout", "private stderr")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


class HostPreparationTests(unittest.TestCase):
    def facts(self, fixture="discovery-debian.json", **changes):
        facts = discover_host(FixtureRunner(fixture))
        return dataclasses.replace(facts, **changes)

    def install_plan(self):
        return plan_host_preparation(self.facts("discovery-ubuntu.json"))

    def test_debian_without_docker_plans_signed_repository_and_full_engine(self):
        facts = self.facts("discovery-ubuntu.json", os_id="debian", os_version_id="12", os_codename="bookworm", architecture="x86_64", deployment_user="media")
        plan = plan_host_preparation(facts)
        result = plan.to_dict()
        self.assertEqual(result["repository"]["url"], "https://download.docker.com/linux/debian")
        self.assertEqual(result["repository"]["key_url"], "https://download.docker.com/linux/debian/gpg")
        self.assertEqual(result["repository"]["architecture"], "amd64")
        self.assertEqual(result["repository"]["codename"], "bookworm")
        self.assertEqual(result["packages"], ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
        self.assertIn("repository", {item["kind"] for item in result["mutations"]})
        self.assertRegex(result["plan_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertTrue(result["requires_apply"])

    def test_plan_fingerprint_is_deterministic_and_covers_mutation_relevant_state(self):
        first = self.install_plan()
        second = self.install_plan()
        changed = dataclasses.replace(first, deployment_user="other")
        group_changed = dataclasses.replace(first, configured_groups=first.configured_groups + ("extra",))
        self.assertEqual(first.plan_fingerprint, second.plan_fingerprint)
        self.assertNotEqual(first.plan_fingerprint, changed.plan_fingerprint)
        self.assertNotEqual(first.plan_fingerprint, group_changed.plan_fingerprint)

    def test_ubuntu_without_docker_uses_ubuntu_repository(self):
        plan = self.install_plan()
        self.assertEqual(plan.repository_url, "https://download.docker.com/linux/ubuntu")
        self.assertEqual(plan.repository_codename, "noble")
        self.assertEqual(plan.repository_architecture, "arm64")

    def test_compose_absent_installs_only_plugin(self):
        plan = plan_host_preparation(self.facts(compose_present=False, compose_status="missing"))
        self.assertEqual(plan.packages, ("docker-compose-plugin",))
        self.assertNotIn("service", {item["kind"] for item in plan.mutations})

    def test_stopped_or_running_disabled_daemon_plans_enable_and_start(self):
        cases = (
            {"docker_daemon_reachable": False, "docker_daemon_status": "error"},
            {"docker_service_enabled": False, "docker_service_status": "disabled"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                plan = plan_host_preparation(self.facts(**changes))
                self.assertIn({"kind": "service", "service": "docker", "action": "enable_and_start"}, plan.mutations)

    def test_uncertain_service_or_identity_and_group_probes_refuse(self):
        cases = (
            ({"docker_service_enabled": None, "docker_service_status": "error"}, "docker_service_state_unknown"),
            ({"uid": None}, "identity_incomplete"),
            ({"gid": None}, "identity_incomplete"),
            ({"deployment_user": None}, "identity_incomplete"),
            ({"user_groups": (), "configured_groups_status": "error"}, "identity_incomplete"),
            ({"session_groups": (), "session_groups_status": "error"}, "identity_incomplete"),
        )
        for changes, code in cases:
            with self.subTest(changes=changes):
                plan = plan_host_preparation(self.facts(**changes))
                self.assertEqual(plan.refusal["code"], code)
                self.assertEqual(plan.mutations, ())

    def test_conflicts_or_unknown_conflict_probe_refuse_repository_transition(self):
        cases = (
            ({"conflicting_packages": ("docker.io", "runc"), "conflicting_packages_status": "ok"}, "conflicting_packages_installed"),
            ({"conflicting_packages": (), "conflicting_packages_status": "error"}, "conflicting_packages_unknown"),
        )
        for changes, code in cases:
            with self.subTest(changes=changes):
                plan = plan_host_preparation(self.facts("discovery-ubuntu.json", **changes))
                self.assertEqual(plan.refusal["code"], code)
                self.assertEqual(plan.mutations, ())
                expected = list(changes["conflicting_packages"]) if changes["conflicting_packages_status"] == "ok" else None
                self.assertEqual(plan.to_dict()["conflicting_packages"], expected)

    def test_missing_sudo_is_structured_refusal(self):
        plan = plan_host_preparation(self.facts("discovery-ubuntu.json", privilege_escalation="missing"))
        self.assertEqual(plan.refusal["code"], "privilege_escalation_unavailable")
        self.assertEqual(plan.mutations, ())

    def test_group_reconnect_and_ready_idempotence(self):
        plan = plan_host_preparation(self.facts(user_groups=("homeflix", "sudo")))
        self.assertIn({"kind": "group", "group": "docker", "user": "homeflix", "action": "add_user"}, plan.mutations)
        self.assertTrue(plan.reconnect_required)
        self.assertEqual(plan.verification[-1]["via"], "sudo")
        ready = plan_host_preparation(self.facts())
        self.assertEqual(ready.mutations, ())
        self.assertFalse(ready.requires_apply)

    def test_cli_defaults_read_only_and_apply_requires_confirmation(self):
        facts = self.facts("discovery-ubuntu.json")
        with mock.patch("scripts.homeflix_setup.cli.discover_host", return_value=facts), mock.patch(
            "scripts.homeflix_setup.cli.apply_host_preparation"
        ) as apply:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = main(("--json", "host", "prepare"), repository_root=Path("."))
            self.assertEqual(return_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["requires_apply"])
            self.assertIn("plan_fingerprint", payload)
            apply.assert_not_called()

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = main(("--json", "host", "prepare", "--apply"), repository_root=Path("."))
            self.assertEqual(return_code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["refusal"]["code"], "plan_confirmation_required")
            apply.assert_not_called()

    def test_apply_rebuilds_plan_and_stale_ready_race_executes_nothing(self):
        plan = self.install_plan()
        runner = RecordingRunner("discovery-ubuntu.json")
        runner.commands.update({
            "docker --version": [0, "Docker version fixture\n", ""],
            "docker compose version": [0, "Docker Compose fixture\n", ""],
            "docker info --format {{json .ServerVersion}}": [0, "\"fixture\"\n", ""],
            "systemctl is-enabled docker": [0, "enabled\n", ""],
            "id -nG": [0, "ubuntu sudo docker\n", ""],
            "id -nG ubuntu": [0, "ubuntu sudo docker\n", ""],
        })
        result = apply_host_preparation(plan, runner, confirm_plan=plan.plan_fingerprint)
        self.assertEqual(result.refusal["code"], "plan_changed")
        self.assertEqual(runner.mutations, [])

    def test_wrong_confirmation_refuses_before_rediscovery(self):
        plan = self.install_plan()
        runner = RecordingRunner("discovery-ubuntu.json")
        result = apply_host_preparation(plan, runner, confirm_plan="0" * 64)
        self.assertEqual(result.refusal["code"], "plan_confirmation_mismatch")
        self.assertEqual(runner.calls, [])
        self.assertEqual(runner.mutations, [])

    def test_apply_uses_bounded_commands_atomic_repository_files_and_sudo_verification(self):
        plan = self.install_plan()
        runner = RecordingRunner("discovery-ubuntu.json")
        result = apply_host_preparation(plan, runner, confirm_plan=plan.plan_fingerprint)
        calls = [(unwrapped(argv), input_text, timeout) for argv, input_text, timeout in runner.mutations]
        actual = [argv for argv, _, _ in calls]
        self.assertTrue(result.applied, result.refusal)
        self.assertTrue(all(timeout is not None and timeout > 0 for _, _, timeout in calls))
        self.assertTrue(all("--kill-after=10s" in argv for argv, _, _ in runner.mutations))
        privileged = [argv for argv, _, _ in runner.mutations if argv[0] == "sudo"]
        self.assertTrue(privileged and all(argv[:2] == ("sudo", "-n") for argv in privileged))
        self.assertFalse(any(argv[0] in {"sh", "bash"} or "|" in argv for argv in actual))
        curl = next(argv for argv in actual if argv and argv[0] == "curl")
        self.assertNotIn("/etc/apt/keyrings/docker.asc", curl[-1])
        tee = next(argv for argv in actual if argv and argv[0] == "tee")
        self.assertNotEqual(tee[-1], "/etc/apt/sources.list.d/docker.list")
        self.assertIn(("mv", "-f", "/etc/apt/keyrings/.docker.asc.homeflix.tmp", "/etc/apt/keyrings/docker.asc"), actual)
        self.assertIn(("mv", "-f", "/etc/apt/sources.list.d/.docker.list.homeflix.tmp", "/etc/apt/sources.list.d/docker.list"), actual)
        cleanup = [argv for argv in actual if argv[:2] == ("rm", "-f")]
        self.assertGreaterEqual(len(cleanup), 2)
        self.assertIn(("docker", "compose", "version"), actual)

    def test_each_mutation_stage_failure_is_structured_and_cleanup_runs(self):
        plan = self.install_plan()
        successful_runner = RecordingRunner("discovery-ubuntu.json")
        successful = apply_host_preparation(
            plan, successful_runner, confirm_plan=plan.plan_fingerprint
        )
        self.assertTrue(successful.applied)
        operation_count = successful.commands_completed
        failed_operations: set[str] = set()
        for index in range(operation_count):
            with self.subTest(operation_index=index):
                runner = RecordingRunner("discovery-ubuntu.json")
                runner.fail_mutation_index = index
                result = apply_host_preparation(
                    plan, runner, confirm_plan=plan.plan_fingerprint
                )
                self.assertEqual(result.refusal["code"], "host_preparation_operation_failed")
                self.assertRegex(result.refusal["operation"], r"^[a-z0-9_]+$")
                self.assertEqual(result.commands_completed, index)
                self.assertNotIn("private", json.dumps(result.to_dict()))
                actual = [unwrapped(argv) for argv, _, _ in runner.mutations]
                self.assertEqual(actual[-1][:2], ("rm", "-f"))
                failed_operations.add(result.refusal["operation"])
        self.assertEqual(len(failed_operations), operation_count)

    def test_timeout_and_process_exceptions_are_structured(self):
        exceptions = (
            subprocess.TimeoutExpired(["sudo"], 1, output="private"),
            FileNotFoundError("private missing command"),
            OSError("private process error"),
        )
        for error in exceptions:
            with self.subTest(error=type(error).__name__):
                plan = self.install_plan()
                runner = RecordingRunner("discovery-ubuntu.json")
                runner.failure = ("apt-get", "update")
                runner.exception = error
                result = apply_host_preparation(plan, runner, confirm_plan=plan.plan_fingerprint)
                self.assertEqual(result.refusal["code"], "host_preparation_operation_failed")
                self.assertNotIn("private", json.dumps(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
