from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from scripts.homeflix_setup.envfile import EnvDocument, update_env
from scripts.homeflix_setup.secrets import ensure_service_credentials, reveal_jellyfin


class EnvDocumentTests(unittest.TestCase):
    def test_comments_order_unknown_keys_and_effective_last_duplicate_survive(self) -> None:
        source = "# heading\nFIRST=one # earlier\n# middle\nUNKNOWN=keep\nFIRST=duplicate  # effective\nLAST=three\n"
        document = EnvDocument.parse(source)
        self.assertEqual(document.get("FIRST"), "duplicate")
        rendered = document.updated({"FIRST": "changed value"}).render()
        self.assertEqual(
            rendered,
            "# heading\n# middle\nUNKNOWN=keep\nFIRST='changed value'  # effective\nLAST=three\n",
        )

    def test_inline_comments_are_quote_aware_and_preserved_on_rewrite(self) -> None:
        source = (
            "PLAIN=value # note\n"
            "SPACED=value   # spaced note\n"
            "SINGLE='value # data' # single note\n"
            'DOUBLE="value # data"  # double note\n'
            "HASH=value#data\n"
        )
        document = EnvDocument.parse(source)
        self.assertEqual(document.get("PLAIN"), "value")
        self.assertEqual(document.get("SPACED"), "value")
        self.assertEqual(document.get("SINGLE"), "value # data")
        self.assertEqual(document.get("DOUBLE"), "value # data")
        self.assertEqual(document.get("HASH"), "value#data")
        rendered = document.updated({
            "PLAIN": "new value", "SPACED": "new value",
            "SINGLE": "new # data", "DOUBLE": "new # data",
        }).render()
        self.assertIn("PLAIN='new value' # note\n", rendered)
        self.assertIn("SPACED='new value'   # spaced note\n", rendered)
        self.assertIn("SINGLE='new # data' # single note\n", rendered)
        self.assertIn("DOUBLE='new # data'  # double note\n", rendered)

    def test_values_round_trip_through_shell_and_compose_dotenv(self) -> None:
        values = (
            "value with spaces",
            "some 'safe' value",
            "$HOME `literal` C:\\path",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose_path = root / "compose.yml"
            compose_path.write_text(
                "services:\n  fixture:\n    image: busybox\n    environment:\n      VALUE: ${VALUE}\n",
                encoding="utf-8",
            )
            env_path = root / ".env"
            for value in values:
                with self.subTest(value=value):
                    update_env(env_path, {"VALUE": value})
                    shell = subprocess.run(
                        ["bash", "-c", '. "$1"; printf "%s" "$VALUE"', "bash", str(env_path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(shell.returncode, 0, shell.stderr)
                    self.assertEqual(shell.stdout, value)
                    composed = subprocess.run(
                        ["docker", "compose", "--env-file", str(env_path), "-f", str(compose_path), "config", "--environment"],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(composed.returncode, 0, composed.stderr)
                    environment = dict(line.split("=", 1) for line in composed.stdout.splitlines() if "=" in line)
                    self.assertEqual(environment["VALUE"], value)

    def test_rejects_values_not_portably_representable(self) -> None:
        for bad in (
            "line\nbreak", "line\rbreak", "nul\0byte",
            "apostrophe '$dollar", "apostrophe '`command", "apostrophe '\\path", 'apostrophe \'"quote',
        ):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                EnvDocument.parse("").updated({"VALUE": bad})

    def test_inline_comment_round_trips_through_shell_and_compose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text("VALUE=old # retained\n", encoding="utf-8")
            update_env(env_path, {"VALUE": "value # data"})
            self.assertEqual(env_path.read_text(encoding="utf-8"), "VALUE='value # data' # retained\n")
            shell = subprocess.run(
                ["bash", "-c", '. "$1"; printf "%s" "$VALUE"', "bash", str(env_path)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(shell.stdout, "value # data")
            compose_path = root / "compose.yml"
            compose_path.write_text("services:\n  fixture:\n    image: busybox\n    environment:\n      VALUE: ${VALUE}\n", encoding="utf-8")
            composed = subprocess.run(
                ["docker", "compose", "--env-file", str(env_path), "-f", str(compose_path), "config", "--environment"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(composed.returncode, 0, composed.stderr)
            self.assertIn("VALUE=value # data\n", composed.stdout)

    def test_update_is_atomic_mode_0600_and_result_never_contains_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SECRET=old\nOTHER=value\n", encoding="utf-8")
            before_inode = path.stat().st_ino
            secret = "not-for-output"
            result = update_env(path, {"SECRET": secret}, {"SECRET"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(path.stat().st_ino, before_inode)
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])
            self.assertNotIn(secret, repr(result))
            self.assertIn("SECRET", repr(result))

    def test_atomic_setup_failure_closes_fd_cleans_temp_and_preserves_destination(self) -> None:
        for operation in ("fchmod", "fdopen"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".env"
                path.write_text("VALUE=original\n", encoding="utf-8")
                with mock.patch(
                    f"scripts.homeflix_setup.envfile.os.{operation}", side_effect=OSError("setup failure")
                ), mock.patch("scripts.homeflix_setup.envfile.os.close", wraps=os.close) as close:
                    with self.assertRaisesRegex(OSError, "setup failure"):
                        update_env(path, {"VALUE": "replacement"})
                close.assert_called_once()
                self.assertEqual(path.read_text(encoding="utf-8"), "VALUE=original\n")
                self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_duplicate_existing_credentials_preserve_and_reveal_effective_last_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "JELLYFIN_ADMIN_USER=earlier\nJELLYFIN_ADMIN_PASSWORD=\n"
                "JELLYFIN_ADMIN_USER=effective\nJELLYFIN_ADMIN_PASSWORD=effective-secret\n",
                encoding="utf-8",
            )
            with patch("scripts.homeflix_setup.secrets.secrets.token_urlsafe") as token:
                result = ensure_service_credentials(path)
            token.assert_not_called()
            self.assertIn("preserved", repr(result))
            document = EnvDocument.load(path)
            self.assertEqual(document.get("JELLYFIN_ADMIN_USER"), "effective")
            self.assertEqual(document.get("JELLYFIN_ADMIN_PASSWORD"), "effective-secret")

            terminal = mock.MagicMock()
            terminal.__enter__.return_value = terminal
            with mock.patch("scripts.homeflix_setup.secrets.os.open", return_value=41), mock.patch(
                "scripts.homeflix_setup.secrets.os.isatty", return_value=True
            ), mock.patch("scripts.homeflix_setup.secrets.os.fdopen", return_value=terminal), mock.patch(
                "scripts.homeflix_setup.secrets.os.close"
            ):
                reveal_jellyfin(path)
            written = "".join(call.args[0] for call in terminal.write.call_args_list)
            self.assertIn("effective\n", written)
            self.assertIn("effective-secret\n", written)
            self.assertNotIn("earlier", written)

    def test_credentials_are_generated_once_without_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("JELLYFIN_ADMIN_USER=\nJELLYFIN_ADMIN_PASSWORD=\n", encoding="utf-8")
            with patch("scripts.homeflix_setup.secrets.secrets.token_urlsafe", return_value="generated-password") as token:
                first = ensure_service_credentials(path)
                second = ensure_service_credentials(path)
            self.assertEqual(token.call_count, 1)
            self.assertEqual(path.read_text(encoding="utf-8").count("generated-password"), 1)
            self.assertNotIn("generated-password", repr(first))
            self.assertNotIn("generated-password", repr(second))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
