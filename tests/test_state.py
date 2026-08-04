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

    def test_allows_host_fact_keys_with_harmless_output_words(self) -> None:
        path = self.temp_path / "setup.json"
        state = SetupState(
            host_facts={
                "output_format_supported": True,
                "environment_value_source_available": False,
            }
        )

        state.save(path)

        self.assertEqual(SetupState.load(path), state)


class CommandRunnerTests(unittest.TestCase):
    @mock.patch("scripts.homeflix_setup.command.subprocess.run")
    def test_run_captures_text_and_forwards_input(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(["tool"], 0, "done\n", "")

        result = CommandRunner().run(["tool"], input_text="answer\n")

        self.assertEqual(result.stdout, "done\n")
        run.assert_called_once_with(
            ["tool"], input="answer\n", text=True, capture_output=True, check=False
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
