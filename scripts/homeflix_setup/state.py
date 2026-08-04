"""Non-secret, local setup checkpoints and host facts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


CURRENT_SCHEMA_VERSION = 1

_FORBIDDEN_KEY_SUFFIXES = (
    ("api", "key"),
    ("apikey",),
    ("command", "output"),
    ("credential",),
    ("env",),
    ("environment",),
    ("environment", "value"),
    ("output",),
    ("password",),
    ("secret",),
    ("stderr",),
    ("stdout",),
    ("token",),
)
_SINGULAR_KEY_TOKENS = {
    "credentials": "credential",
    "outputs": "output",
    "passwords": "password",
    "secrets": "secret",
    "tokens": "token",
    "values": "value",
}


def _key_tokens(key: str) -> tuple[str, ...]:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return tuple(
        _SINGULAR_KEY_TOKENS.get(token, token)
        for token in re.findall(r"[a-z0-9]+", separated.lower())
    )


def _is_forbidden_key(key: str) -> bool:
    tokens = _key_tokens(key)
    return any(tokens[-len(suffix) :] == suffix for suffix in _FORBIDDEN_KEY_SUFFIXES)


def _validate_keys(value: Any, location: str = "state") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{location} keys must be strings")
            if _is_forbidden_key(key):
                raise ValueError(f"state field {key!r} is not permitted")
            _validate_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_keys(child, f"{location}[{index}]")
    elif value is not None and not isinstance(value, (bool, int, float, str)):
        raise ValueError(f"{location} contains a non-JSON value")


@dataclass(eq=True)
class SetupState:
    """Versioned setup state containing only checkpoints and non-secret facts."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    checkpoints: dict[str, Any] = field(default_factory=dict)
    host_facts: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "SetupState":
        state_path = Path(path)
        if not state_path.exists():
            return cls()

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read setup state: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("setup state must be a JSON object")

        version = payload.get("schema_version")
        if version != CURRENT_SCHEMA_VERSION:
            if isinstance(version, int) and version > CURRENT_SCHEMA_VERSION:
                raise ValueError(f"unsupported future setup state schema version {version}")
            raise ValueError(f"unsupported setup state schema version {version!r}")
        if set(payload) != {"schema_version", "checkpoints", "host_facts"}:
            raise ValueError("setup state contains unknown fields")
        checkpoints = payload["checkpoints"]
        host_facts = payload["host_facts"]
        if not isinstance(checkpoints, dict) or not isinstance(host_facts, dict):
            raise ValueError("setup state checkpoints and host_facts must be objects")
        _validate_keys({"checkpoints": checkpoints, "host_facts": host_facts})
        return cls(version, checkpoints, host_facts)

    def save(self, path: str | os.PathLike[str]) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported setup state schema version {self.schema_version!r}")
        payload = {
            "schema_version": self.schema_version,
            "checkpoints": self.checkpoints,
            "host_facts": self.host_facts,
        }
        _validate_keys(payload)
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=state_path.parent,
                prefix=f".{state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, state_path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
