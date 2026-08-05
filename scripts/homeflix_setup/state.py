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

_CHECKPOINT_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$")
_CHECKPOINTS = {"configured", "core_containers_started", "core_api_configured", "core_verified"}
_HOST_FACT_TYPES: dict[str, type[object]] = {
    "os_id": str,
    "os_version_id": str,
    "architecture": str,
    "uid": int,
    "gid": int,
    "timezone": str,
    "memory_bytes": int,
    "cpu_model": str,
    "docker_present": bool,
    "compose_present": bool,
    "docker_daemon_reachable": bool,
    "ssh_context": bool,
}


def _validate_checkpoints(checkpoints: object) -> None:
    if not isinstance(checkpoints, dict):
        raise ValueError("setup state checkpoints must be an object")
    for name, completed in checkpoints.items():
        if not isinstance(name, str) or _CHECKPOINT_NAME.fullmatch(name) is None or name not in _CHECKPOINTS:
            raise ValueError(f"checkpoint name {name!r} is not permitted")
        if type(completed) is not bool:
            raise ValueError(f"checkpoint {name!r} must be boolean")


def _validate_host_facts(host_facts: object) -> None:
    if not isinstance(host_facts, dict):
        raise ValueError("setup state host_facts must be an object")
    for name, value in host_facts.items():
        expected_type = _HOST_FACT_TYPES.get(name)
        if expected_type is None:
            raise ValueError(f"host fact {name!r} is not permitted")
        if type(value) is not expected_type:
            raise ValueError(f"host fact {name!r} must be {expected_type.__name__}")


def _validate_state_parts(checkpoints: object, host_facts: object) -> None:
    _validate_checkpoints(checkpoints)
    _validate_host_facts(host_facts)


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
        if type(version) is not int or version != CURRENT_SCHEMA_VERSION:
            if type(version) is int and version > CURRENT_SCHEMA_VERSION:
                raise ValueError(f"unsupported future setup state schema version {version}")
            raise ValueError(f"unsupported setup state schema version {version!r}")
        if set(payload) != {"schema_version", "checkpoints", "host_facts"}:
            raise ValueError("setup state contains unknown fields")
        checkpoints = payload["checkpoints"]
        host_facts = payload["host_facts"]
        _validate_state_parts(checkpoints, host_facts)
        return cls(version, checkpoints, host_facts)

    def save(self, path: str | os.PathLike[str]) -> None:
        if type(self.schema_version) is not int or self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported setup state schema version {self.schema_version!r}")
        payload = {
            "schema_version": self.schema_version,
            "checkpoints": self.checkpoints,
            "host_facts": self.host_facts,
        }
        _validate_state_parts(self.checkpoints, self.host_facts)
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
