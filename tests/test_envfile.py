from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.homeflix_setup.envfile import EnvDocument, update_env
from scripts.homeflix_setup.secrets import ensure_service_credentials


class EnvDocumentTests(unittest.TestCase):
    def test_comments_order_unknown_keys_and_duplicate_removal_survive(self) -> None:
        source = "# heading\nFIRST=one\n# middle\nUNKNOWN=keep\nFIRST=duplicate\nLAST=three\n"
        rendered = EnvDocument.parse(source).updated({"FIRST": "changed value"}).render()
        self.assertEqual(
            rendered,
            "# heading\nFIRST='changed value'\n# middle\nUNKNOWN=keep\nLAST=three\n",
        )

    def test_shell_quotes_spaces_and_single_quotes_and_rejects_injection(self) -> None:
        rendered = EnvDocument.parse("").updated({"VALUE": "some 'safe' value"}).render()
        self.assertEqual(rendered, "VALUE='some '\\''safe'\\'' value'\n")
        for bad in ("line\nbreak", "line\rbreak", "nul\0byte"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                EnvDocument.parse("").updated({"VALUE": bad})

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
