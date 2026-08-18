"""Shared helpers for setup CLI tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from scripts.homeflix_setup.envfile import EnvDocument


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "scripts" / "homeflix"


def compose_test_environment(fixture: Path | None = None) -> dict[str, str]:
    """Keep exported variables from overriding Compose fixture interpolation."""
    fixture = fixture or REPOSITORY_ROOT / ".env.example"
    documents = [EnvDocument.load(fixture)]
    default_fixture = REPOSITORY_ROOT / ".env.example"
    if fixture.resolve() != default_fixture.resolve():
        documents.append(EnvDocument.load(default_fixture))
    return {
        key: value
        for key, value in os.environ.items()
        if all(document.get(key) is None for document in documents)
    }


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LAUNCHER), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def parse_single_json(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(text)
    if text[end:].strip():
        raise AssertionError("CLI emitted content after its JSON object")
    if not isinstance(value, dict):
        raise AssertionError("CLI output was not a JSON object")
    return value


class TemporaryDirectoryTestCase:
    """Mixin that exposes a temporary directory as ``self.temp_path``."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()
        super().tearDown()  # type: ignore[misc]
