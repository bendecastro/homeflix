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


def _assignment_parts(line: str) -> tuple[str, str, str, str] | None:
    if line.endswith("\r\n"):
        content, ending = line[:-2], "\r\n"
    elif line.endswith("\n") or line.endswith("\r"):
        content, ending = line[:-1], line[-1]
    else:
        content, ending = line, ""
    match = _ASSIGNMENT.match(content)
    if match is None:
        return None
    key, body = match.groups()
    quote: str | None = None
    escaped = False
    for index, character in enumerate(body):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in ("'", '"'):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (index == 0 or body[index - 1].isspace()):
            comment_start = index
            while comment_start > 0 and body[comment_start - 1] in " \t":
                comment_start -= 1
            return key, body[:comment_start], body[comment_start:], ending
    return key, body, "", ending


def _decode(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
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
    if "'" not in value:
        return "'" + value + "'"
    if not any(character in value for character in ('"', "$", "`", "\\")):
        return '"' + value + '"'
    raise ValueError(
        "environment value cannot be represented identically for shell and Compose dotenv"
    )


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
            assignment = _assignment_parts(line)
            if assignment is not None and assignment[0] == key:
                found = _decode(assignment[1])
        return found

    def updated(self, updates: Mapping[str, str]) -> "EnvDocument":
        for key, value in updates.items():
            if not _KEY.fullmatch(key):
                raise ValueError(f"invalid environment key {key!r}")
            _quote(str(value))
        remaining = dict(updates)
        assignments = [_assignment_parts(line) for line in self.lines]
        last_indexes: dict[str, int] = {}
        for index, assignment in enumerate(assignments):
            if assignment is not None and assignment[0] in updates:
                last_indexes[assignment[0]] = index
        output: list[str] = []
        for index, (line, assignment) in enumerate(zip(self.lines, assignments)):
            if assignment is None or assignment[0] not in updates:
                output.append(line)
                continue
            key, _value, comment, ending = assignment
            if last_indexes[key] != index:
                continue
            output.append(f"{key}={_quote(str(updates[key]))}{comment}{ending}")
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
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=env_path.parent, prefix=f".{env_path.name}.", suffix=".tmp"
        )
        os.fchmod(descriptor, 0o600)
        temporary = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor = None  # fdopen owns the descriptor after a successful call.
        with temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, env_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return {
        "keys": [
            {"name": key, "status": "updated", "secret": key in secret_set}
            for key in updates
        ]
    }
