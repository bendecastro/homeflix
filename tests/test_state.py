from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tests.helpers import TemporaryDirectoryTestCase

from scripts.homeflix_setup.command import CommandRunner
from scripts.homeflix_setup.state import CURRENT_SCHEMA_VERSION, SetupState


class SetupStateTests(TemporaryDirectoryTestCase, unittest.TestCase):
    def test_load_of_absent_path_returns_default_without_creating_file(self) -> None:
        path = self.temp_path / ".homeflix" / "setup.json"

        state = SetupState.load(path)

        self.assertEqual(state.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(state.checkpoints, {})
        self.assertEqual(state.host_facts, {})
        self.assertFalse(path.exists())

    def test_save_is_atomic_and_round_trips(self) -> None:
        path = self.temp_path / ".homeflix" / "setup.json"
        state = SetupState(checkpoints={"configured": True}, host_facts={"os_id": "debian"})

        state.save(path)

        self.assertEqual(SetupState.load(path), state)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_rejects_unknown_future_schema(self) -> None:
        path = self.temp_path / "setup.json"
        path.write_text('{"schema_version": 2, "checkpoints": {}, "host_facts": {}}', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "schema version"):
            SetupState.load(path)

    def test_schema_version_requires_exact_integer_on_load_and_save(self) -> None:
        path = self.temp_path / "setup.json"
        for version in (True, 1.0, "1"):
            with self.subTest(version=version, operation="load"):
                payload = {"schema_version": version, "checkpoints": {}, "host_facts": {}}
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "schema version"):
                    SetupState.load(path)

            with self.subTest(version=version, operation="save"):
                with self.assertRaisesRegex(ValueError, "schema version"):
                    SetupState(schema_version=version).save(path)  # type: ignore[arg-type]

    @mock.patch("scripts.homeflix_setup.state.os.replace")
    @mock.patch("scripts.homeflix_setup.state.os.fsync", side_effect=OSError("fsync failed"))
    def test_atomic_save_failure_preserves_existing_state_and_cleans_temp(
        self, fsync: mock.Mock, replace: mock.Mock
    ) -> None:
        path = self.temp_path / ".homeflix" / "setup.json"
        path.parent.mkdir()
        original = b'{"existing": "state"}\n'
        path.write_bytes(original)

        with self.assertRaisesRegex(OSError, "fsync failed"):
            SetupState(checkpoints={"configured": True}).save(path)

        fsync.assert_called_once()
        replace.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_rejects_secret_and_command_output_fields(self) -> None:
        path = self.temp_path / "setup.json"
        for facts in ({"api_key": "secret"}, {"stdout": "command result"}, {"environment": {"TZ": "UTC"}}):
            with self.subTest(facts=facts):
                with self.assertRaisesRegex(ValueError, "not permitted"):
                    SetupState(host_facts=facts).save(path)

    def test_save_rejects_nested_plural_environment_and_output_fields(self) -> None:
        path = self.temp_path / "setup.json"
        forbidden_keys = (
            "environment_values",
            "environment-values",
            "environmentValues",
            "ENVIRONMENT_VALUES",
            "command_outputs",
            "command-outputs",
            "commandOutputs",
            "COMMAND_OUTPUTS",
            "captured.command.outputs",
        )

        for key in forbidden_keys:
            with self.subTest(key=key):
                state = SetupState(host_facts={"nested": {key: "raw value"}})
                with self.assertRaisesRegex(ValueError, "not permitted"):
                    state.save(path)

    def test_load_rejects_nested_plural_environment_and_output_fields(self) -> None:
        path = self.temp_path / "setup.json"
        for key in ("environment_values", "command_outputs", "savedCommandOutputs"):
            with self.subTest(key=key):
                payload = {
                    "schema_version": 1,
                    "checkpoints": {},
                    "host_facts": {"nested": {key: "raw value"}},
                }
                path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "not permitted"):
                    SetupState.load(path)

    def test_rejects_reviewer_bypass_fields_on_save_and_load(self) -> None:
        path = self.temp_path / "setup.json"
        for key in ("env_values", "environment_variables", "command_results", "stdout_lines"):
            with self.subTest(key=key, operation="save"):
                with self.assertRaisesRegex(ValueError, "not permitted"):
                    SetupState(host_facts={key: "raw value"}).save(path)

            with self.subTest(key=key, operation="load"):
                payload = {
                    "schema_version": 1,
                    "checkpoints": {},
                    "host_facts": {key: "raw value"},
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "not permitted"):
                    SetupState.load(path)

    def test_allowlisted_scalar_host_facts_round_trip(self) -> None:
        path = self.temp_path / "setup.json"
        state = SetupState(
            host_facts={
                "os_id": "debian",
                "os_version_id": "12",
                "architecture": "x86_64",
                "uid": 1000,
                "gid": 1000,
                "timezone": "Etc/UTC",
                "memory_bytes": 8_000_000_000,
                "cpu_model": "Fixture CPU",
                "docker_present": True,
                "compose_present": True,
                "docker_daemon_reachable": False,
                "ssh_context": False,
            }
        )

        state.save(path)

        self.assertEqual(SetupState.load(path), state)

    def test_string_host_facts_are_field_bounded_and_printable(self) -> None:
        path = self.temp_path / "setup.json"
        for value in ("x" * 257, "debian\nsecret", "cpu\x1b[31mred", "bad\x7fvalue"):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, "unsafe string"):
                    SetupState(host_facts={"cpu_model": value}).save(path)
        self.assertFalse(path.exists())

    def test_rejects_nested_or_wrong_typed_host_facts(self) -> None:
        path = self.temp_path / "setup.json"
        invalid_facts = (
            {"os_id": {"value": "debian"}},
            {"cpu_model": ["Fixture CPU"]},
            {"uid": "1000"},
            {"memory_bytes": True},
            {"docker_present": 1},
        )
        for facts in invalid_facts:
            with self.subTest(facts=facts, operation="save"):
                with self.assertRaises(ValueError):
                    SetupState(host_facts=facts).save(path)

            with self.subTest(facts=facts, operation="load"):
                payload = {
                    "schema_version": 1,
                    "checkpoints": {},
                    "host_facts": facts,
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    SetupState.load(path)

    def test_evidence_rejects_addresses_and_unknown_fields(self) -> None:
        path = self.temp_path / "setup.json"
        with self.assertRaisesRegex(ValueError, "not permitted"):
            SetupState(evidence={"host_ip": "203.0.113.10"}).save(path)
        with self.assertRaisesRegex(ValueError, "unsafe string"):
            SetupState(
                evidence={
                    "recorded_at": "2026-08-14T12:00:00Z",
                    "image_id": "203.0.113.10",
                    "config_digest": "a" * 64,
                    "tunnel_healthy": True,
                    "tunnel_device": True,
                    "namespace_dns": True,
                    "egress_distinct": True,
                }
            ).save(path)
        self.assertFalse(path.exists())

    def test_fail_closed_boolean_is_allowlisted_and_extra_keys_stay_rejected(self) -> None:
        path = self.temp_path / "setup.json"
        evidence = {
            "recorded_at": "2026-08-14T12:00:00Z",
            "image_id": "sha256:fixturegluetunimage",
            "config_digest": "a" * 64,
            "tunnel_healthy": True,
            "tunnel_device": True,
            "namespace_dns": True,
            "egress_distinct": True,
            "fail_closed": True,
        }
        SetupState(evidence=evidence).save(path)
        self.assertEqual(SetupState.load(path).evidence["fail_closed"], True)
        with self.assertRaisesRegex(ValueError, "not permitted"):
            SetupState(evidence={**evidence, "forwarded_port": 5914}).save(path)
        with self.assertRaisesRegex(ValueError, "must be boolean"):
            SetupState(evidence={**evidence, "fail_closed": "yes"}).save(path)

    def test_secret_like_checkpoint_names_are_rejected(self) -> None:
        path = self.temp_path / "setup.json"
        for name in ("api_key", "password_saved", "jellyfin_token", "env_value", "credential"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "not permitted"):
                    SetupState(checkpoints={name: True}).save(path)

    def test_checkpoints_require_slug_names_and_boolean_values(self) -> None:
        path = self.temp_path / "setup.json"
        SetupState(checkpoints={"core_containers_started": True}).save(path)

        for checkpoints in ({"Not a slug": True}, {"configured": "yes"}, {"nested": {"done": True}}):
            with self.subTest(checkpoints=checkpoints, operation="save"):
                with self.assertRaises(ValueError):
                    SetupState(checkpoints=checkpoints).save(path)

            with self.subTest(checkpoints=checkpoints, operation="load"):
                payload = {
                    "schema_version": 1,
                    "checkpoints": checkpoints,
                    "host_facts": {},
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    SetupState.load(path)


class CommandRunnerTests(unittest.TestCase):
    @mock.patch("scripts.homeflix_setup.command.subprocess.run")
    def test_run_captures_text_and_forwards_input(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(["tool"], 0, "done\n", "")

        result = CommandRunner().run(["tool"], input_text="answer\n")

        self.assertEqual(result.stdout, "done\n")
        run.assert_called_once_with(
            ["tool"], input="answer\n", text=True, capture_output=True, check=False, timeout=None
        )

    @mock.patch("scripts.homeflix_setup.command.subprocess.run")
    def test_run_redacts_results_and_checked_error(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["tool", "token-value"], 1, "token-value", "bad token-value"
        )

        with self.assertRaises(subprocess.CalledProcessError) as raised:
            CommandRunner().run(["tool", "token-value"], check=True, redact=("token-value",))

        self.assertNotIn("token-value", str(raised.exception))
        self.assertEqual(raised.exception.stderr, "bad [REDACTED]")

    @mock.patch("scripts.homeflix_setup.command.subprocess.run")
    def test_run_redacts_overlapping_secrets_longest_first(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["tool", "token-value"], 1, "token-value", "bad token-value"
        )

        with self.assertRaises(subprocess.CalledProcessError) as raised:
            CommandRunner().run(
                ["tool", "token-value"],
                check=True,
                redact=("token", "token-value", "", "token-value"),
            )

        error = raised.exception
        for rendered in (*error.cmd, error.output, error.stderr, str(error)):
            self.assertNotIn("token", rendered)
            self.assertNotIn("value", rendered)
        self.assertEqual(error.cmd, ["tool", "[REDACTED]"])
        self.assertEqual(error.output, "[REDACTED]")
        self.assertEqual(error.stderr, "bad [REDACTED]")
