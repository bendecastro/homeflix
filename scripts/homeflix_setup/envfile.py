"""Comment-preserving, shell-compatible Homeflix environment files."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Iterable


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _decode(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("environment values must not contain newline, carriage return, or NUL")
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@,+-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass
class EnvDocument:
    """A line-preserving dotenv document with unique-key updates."""

    lines: list[str]

    @classmethod
    def parse(cls, contents: str) -> "EnvDocument":
        return cls(contents.splitlines(keepends=True))

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "EnvDocument":
        return cls.parse(Path(path).read_text(encoding="utf-8"))

    def get(self, key: str) -> str | None:
        found: str | None = None
        for line in self.lines:
            match = _ASSIGNMENT.match(line.rstrip("\r\n"))
            if match and match.group(1) == key and found is None:
                found = _decode(match.group(2))
        return found

    def updated(self, updates: Mapping[str, str]) -> "EnvDocument":
        for key, value in updates.items():
            if not _KEY.fullmatch(key):
                raise ValueError(f"invalid environment key {key!r}")
            _quote(str(value))
        remaining = dict(updates)
        seen: set[str] = set()
        output: list[str] = []
        for line in self.lines:
            match = _ASSIGNMENT.match(line.rstrip("\r\n"))
            if not match or match.group(1) not in updates:
                output.append(line)
                continue
            key = match.group(1)
            if key in seen:
                continue
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            output.append(f"{key}={_quote(str(updates[key]))}{ending}")
            seen.add(key)
            remaining.pop(key, None)
        if remaining:
            if output and not output[-1].endswith(("\n", "\r")):
                output[-1] += "\n"
            for key, value in remaining.items():
                output.append(f"{key}={_quote(str(value))}\n")
        return EnvDocument(output)

    def render(self) -> str:
        return "".join(self.lines)


def update_env(
    path: str | os.PathLike[str],
    updates: Mapping[str, str],
    secret_keys: Iterable[str] = (),
) -> dict[str, object]:
    """Atomically update a dotenv file and return only key names and statuses."""

    env_path = Path(path)
    document = EnvDocument.load(env_path) if env_path.exists() else EnvDocument([])
    secret_set = set(secret_keys)
    unknown_secrets = secret_set.difference(updates)
    if unknown_secrets:
        raise ValueError("secret_keys must refer to updated keys")
    serialized = document.updated({key: str(value) for key, value in updates.items()}).render()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=env_path.parent, prefix=f".{env_path.name}.", suffix=".tmp"
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, env_path)
        os.chmod(env_path, 0o600)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return {
        "keys": [
            {"name": key, "status": "updated", "secret": key in secret_set}
            for key in updates
        ]
    }
