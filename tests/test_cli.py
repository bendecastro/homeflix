from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
from pathlib import Path
import unittest

from scripts.homeflix_setup.cli import main
from tests.helpers import REPOSITORY_ROOT, parse_single_json, run_cli


def run_main(*args: str, repository_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = main(args, repository_root=repository_root)
    return return_code, stdout.getvalue(), stderr.getvalue()


class StatusCliTests(unittest.TestCase):
    def test_json_status_is_one_object_with_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository_root = Path(directory)
            return_code, stdout, stderr = run_main(
                "--json", "status", repository_root=repository_root
            )

            self.assertEqual(return_code, 0, stderr)
            status = parse_single_json(stdout)
            self.assertEqual(status["schema_version"], 1)
            self.assertFalse(status["state_exists"])
            self.assertFalse((repository_root / ".homeflix" / "setup.json").exists())

    def test_json_status_reports_corrupt_and_future_state_as_one_error_object(self) -> None:
        invalid_contents = (
            "not json",
            json.dumps({"schema_version": 2, "checkpoints": {}, "host_facts": {}}),
        )
        for contents in invalid_contents:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                repository_root = Path(directory)
                state_path = repository_root / ".homeflix" / "setup.json"
                state_path.parent.mkdir()
                state_path.write_text(contents, encoding="utf-8")

                return_code, stdout, stderr = run_main(
                    "--json", "status", repository_root=repository_root
                )

                self.assertEqual(return_code, 1)
                self.assertEqual(stderr, "")
                error = parse_single_json(stdout)
                self.assertEqual(error["error"]["code"], "invalid_state")
                self.assertIsInstance(error["error"]["message"], str)

    def test_text_status_reports_invalid_state_concisely_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository_root = Path(directory)
            state_path = repository_root / ".homeflix" / "setup.json"
            state_path.parent.mkdir()
            state_path.write_text("not json", encoding="utf-8")

            return_code, stdout, stderr = run_main("status", repository_root=repository_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertRegex(stderr, r"^homeflix: invalid setup state: .+\n$")
        self.assertNotIn("Traceback", stderr)

    def test_secret_reveal_refuses_json_and_redirected_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "must-not-appear"
            (root / ".env").write_text(
                f"JELLYFIN_ADMIN_USER=admin\nJELLYFIN_ADMIN_PASSWORD={secret}\n",
                encoding="utf-8",
            )
            json_code, json_stdout, json_stderr = run_main(
                "--json", "secrets", "reveal", "jellyfin", repository_root=root
            )
            pipe_code, pipe_stdout, pipe_stderr = run_main(
                "secrets", "reveal", "jellyfin", repository_root=root
            )
        self.assertEqual(json_code, 2)
        self.assertEqual(pipe_code, 2)
        self.assertNotIn(secret, json_stdout + json_stderr + pipe_stdout + pipe_stderr)

    def test_invalid_domain_is_structured_for_all_discovery_entry_points(self) -> None:
        commands = (
            ("discover",),
            ("host", "prepare"),
            ("configure", "--data-root", "/fixture/data", "--config-root", "/fixture/config", "--cache-root", "/fixture/cache"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("DOMAIN='invalid domain'\n", encoding="utf-8")
            for command in commands:
                with self.subTest(command=command, output="text"):
                    code, stdout, stderr = run_main(*command, repository_root=root)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stdout, "")
                    self.assertRegex(stderr, r"^homeflix: .+: LAN DNS domain is invalid\n$")
                    self.assertNotIn("Traceback", stderr)
                with self.subTest(command=command, output="json"):
                    code, stdout, stderr = run_main("--json", *command, repository_root=root)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stderr, "")
                    error = parse_single_json(stdout)
                    self.assertEqual(set(error), {"error"})
                    self.assertIn(error["error"]["code"], {"discovery_refused", "configuration_refused"})
                    self.assertEqual(error["error"]["message"], "LAN DNS domain is invalid")

    def test_launcher_works_cross_cwd_without_changing_repository_state(self) -> None:
        state_path = REPOSITORY_ROOT / ".homeflix" / "setup.json"
        existed_before = state_path.exists()
        contents_before = state_path.read_bytes() if existed_before else None

        with tempfile.TemporaryDirectory() as directory:
            result = run_cli("--json", "status", cwd=Path(directory))

        self.assertIn(result.returncode, (0, 1), result.stderr)
        parse_single_json(result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(state_path.exists(), existed_before)
        if existed_before:
            self.assertEqual(state_path.read_bytes(), contents_before)
