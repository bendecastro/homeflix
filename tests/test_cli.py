from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from tests.helpers import REPOSITORY_ROOT, parse_single_json, run_cli


class StatusCliTests(unittest.TestCase):
    def test_json_status_is_one_object_with_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_directory = Path(directory)
            result = run_cli("--json", "status", cwd=state_directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        status = parse_single_json(result.stdout)
        self.assertEqual(status["schema_version"], 1)
        self.assertFalse(status["state_exists"])

    def test_status_from_another_working_directory_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            state_path = REPOSITORY_ROOT / ".homeflix" / "setup.json"
            self.assertFalse(state_path.exists(), "test requires an absent repository state file")

            result = run_cli("--json", "status", cwd=working_directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path.exists())
