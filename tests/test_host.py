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


class RecordingRunner(FixtureRunner):
    def __init__(self, fixture_name: str) -> None:
        super().__init__(fixture_name)
        self.mutations: list[tuple[tuple[str, ...], str | None]] = []

    def run(self, argv, *, input_text=None, timeout=None, **kwargs):
        key = " ".join(argv)
        if key in self.commands:
            return super().run(argv, timeout=timeout, **kwargs)
        self.mutations.append((tuple(argv), input_text))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


class HostPreparationTests(unittest.TestCase):
    def facts(self, fixture="discovery-debian.json", **changes):
        facts = discover_host(FixtureRunner(fixture))
        return dataclasses.replace(facts, **changes)

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
        self.assertTrue(result["requires_apply"])

    def test_ubuntu_without_docker_uses_ubuntu_repository(self):
        plan = plan_host_preparation(self.facts("discovery-ubuntu.json"))
        self.assertEqual(plan.repository_url, "https://download.docker.com/linux/ubuntu")
        self.assertEqual(plan.repository_codename, "noble")
        self.assertEqual(plan.repository_architecture, "arm64")

    def test_compose_absent_installs_only_plugin(self):
        facts = self.facts(compose_present=False, compose_status="missing")
        plan = plan_host_preparation(facts)
        self.assertEqual(plan.packages, ("docker-compose-plugin",))
        self.assertNotIn("service", {item["kind"] for item in plan.mutations})

    def test_stopped_daemon_plans_enable_and_start(self):
        plan = plan_host_preparation(self.facts(docker_daemon_reachable=False, docker_daemon_status="error"))
        self.assertIn({"kind": "service", "service": "docker", "action": "enable_and_start"}, plan.mutations)

    def test_missing_sudo_is_structured_refusal(self):
        plan = plan_host_preparation(self.facts("discovery-ubuntu.json", privilege_escalation="missing"))
        self.assertEqual(plan.refusal["code"], "privilege_escalation_unavailable")
        self.assertEqual(plan.mutations, ())

    def test_user_outside_docker_group_plans_group_and_reconnect(self):
        plan = plan_host_preparation(self.facts(user_groups=("homeflix", "sudo")))
        self.assertIn({"kind": "group", "group": "docker", "user": "homeflix", "action": "add_user"}, plan.mutations)
        self.assertTrue(plan.reconnect_required)
        self.assertEqual(plan.verification[-1]["via"], "sudo")

    def test_ready_host_is_idempotent(self):
        plan = plan_host_preparation(self.facts())
        self.assertEqual(plan.mutations, ())
        self.assertFalse(plan.requires_apply)
        self.assertIsNone(plan.refusal)

    def test_configured_group_without_active_session_only_requires_reconnect(self):
        plan = plan_host_preparation(self.facts(session_groups=("homeflix", "sudo")))
        self.assertEqual(plan.mutations, ())
        self.assertTrue(plan.reconnect_required)
        self.assertEqual(plan.verification[-1]["via"], "sudo")

    def test_cli_prepare_defaults_to_read_only_json_plan(self):
        facts = self.facts("discovery-ubuntu.json")
        with mock.patch("scripts.homeflix_setup.cli.discover_host", return_value=facts), mock.patch(
            "scripts.homeflix_setup.cli.apply_host_preparation"
        ) as apply:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = main(("--json", "host", "prepare"), repository_root=Path("."))
        self.assertEqual(return_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["requires_apply"])
        apply.assert_not_called()

    def test_apply_revalidates_then_executes_argv_commands_and_sudo_verification(self):
        source = FixtureRunner("discovery-ubuntu.json")
        facts = discover_host(source)
        plan = plan_host_preparation(facts)
        runner = RecordingRunner("discovery-ubuntu.json")
        result = apply_host_preparation(plan, runner)
        calls = [argv for argv, _ in runner.mutations]
        self.assertIn(("sudo", "apt-get", "install", "-y", "ca-certificates", "curl"), calls)
        self.assertIn(("sudo", "apt-get", "install", "-y", *plan.packages), calls)
        self.assertIn(("sudo", "systemctl", "enable", "--now", "docker"), calls)
        self.assertFalse(any(argv[0] in {"sh", "bash"} or "|" in argv for argv in calls))
        source_input = dict(runner.mutations)[("sudo", "tee", "/etc/apt/sources.list.d/docker.list")]
        self.assertIn("signed-by=/etc/apt/keyrings/docker.asc", source_input)
        self.assertIn(("sudo", "usermod", "-aG", "docker", "ubuntu"), calls)
        self.assertIn(("sudo", "docker", "compose", "version"), calls)
        self.assertTrue(result.reconnect_required)

    def test_apply_refuses_changed_independently_rediscovered_identity(self):
        plan = plan_host_preparation(self.facts("discovery-ubuntu.json"))
        runner = RecordingRunner("discovery-ubuntu.json")
        runner.commands["id -un"] = [0, "different\n", ""]
        runner.commands["id -nG different"] = [0, "different sudo\n", ""]
        result = apply_host_preparation(plan, runner)
        self.assertEqual(result.refusal["code"], "host_identity_changed")
        self.assertEqual(runner.mutations, [])


if __name__ == "__main__":
    unittest.main()
