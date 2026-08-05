"""Phase-aware, fail-closed checks for a configured Homeflix host."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping, Protocol, Sequence
import uuid

from .envfile import EnvDocument


_STATUSES = {"pass", "warn", "fail"}
_UNSUPPORTED_FILESYSTEMS = {
    "9p",
    "cifs",
    "exfat",
    "fuseblk",
    "msdos",
    "ntfs",
    "ntfs3",
    "smbfs",
    "vfat",
}


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"invalid check status {self.status!r}")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class PreflightReport:
    phase: str
    results: tuple[CheckResult, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(result.status == status for result in self.results)
            for status in ("pass", "warn", "fail")
        }

    @property
    def passed(self) -> bool:
        return self.counts["fail"] == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "passed": self.passed,
            "counts": self.counts,
            "results": [result.to_dict() for result in self.results],
        }


def _value(config: EnvDocument | Mapping[str, object], key: str) -> object | None:
    if isinstance(config, EnvDocument):
        return config.get(key)
    return config.get(key)


def _path(config: EnvDocument | Mapping[str, object], key: str) -> Path | None:
    value = _value(config, key)
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _identity(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value and value.isascii() and value.isdecimal():
        parsed = int(value, 10)
        return parsed if parsed >= 0 else None
    return None


def _mount_fact(data_root: Path, runner: Runner) -> tuple[str, str] | None:
    try:
        completed = runner.run(
            ("findmnt", "--json", "--target", str(data_root), "--output", "TARGET,SOURCE,FSTYPE"),
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    try:
        payload = json.loads(completed.stdout)
        filesystems = payload["filesystems"]
        fact = filesystems[0]
        target_value = fact["target"]
        filesystem_value = fact["fstype"]
        if not isinstance(target_value, str) or not target_value.startswith("/"):
            return None
        if not isinstance(filesystem_value, str) or not filesystem_value.strip():
            return None
        target = target_value
        filesystem = filesystem_value.casefold()
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return target, filesystem


def _result(results: list[CheckResult], name: str, status: str, message: str) -> None:
    results.append(CheckResult(name, status, message))


def run_preflight(
    config: EnvDocument | Mapping[str, object], phase: str, runner: Runner
) -> PreflightReport:
    """Run bounded checks without creating any missing deployment directories."""

    if phase not in {"core", "acquisition"}:
        raise ValueError("phase must be 'core' or 'acquisition'")

    results: list[CheckResult] = []
    roots: dict[str, Path | None] = {}
    for key in ("DATA_ROOT", "CONFIG_ROOT", "CACHE_ROOT"):
        path = _path(config, key)
        roots[key] = path
        if path is None:
            _result(results, key.casefold(), "fail", f"{key} must be a non-empty path")
        elif not path.is_dir():
            _result(results, key.casefold(), "fail", f"{key} directory is absent")
        else:
            _result(results, key.casefold(), "pass", f"{key} directory exists")

    for key in ("VPN_USER", "VPN_PASSWORD"):
        is_set = isinstance(_value(config, key), str) and bool(str(_value(config, key)))
        if is_set:
            _result(results, key.casefold(), "pass", f"{key} is set")
        else:
            status = "warn" if phase == "core" else "fail"
            _result(results, key.casefold(), status, f"{key} is empty")

    for name, argv in (
        ("docker_cli", ("docker", "--version")),
        ("compose_plugin", ("docker", "compose", "version")),
        ("docker_daemon", ("docker", "info")),
    ):
        try:
            completed = runner.run(argv, check=False, timeout=10)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            _result(results, name, "pass", name.replace("_", " ") + " is available")
        else:
            _result(results, name, "fail", name.replace("_", " ") + " is unavailable")

    puid = _identity(_value(config, "PUID"))
    pgid = _identity(_value(config, "PGID"))
    if puid is None:
        _result(results, "puid", "fail", "PUID must be a non-negative base-10 integer")
    else:
        _result(results, "puid", "pass", "PUID is valid")
    if pgid is None:
        _result(results, "pgid", "fail", "PGID must be a non-negative base-10 integer")
    else:
        _result(results, "pgid", "pass", "PGID is valid")

    data_root = roots["DATA_ROOT"]
    if data_root is not None and data_root.is_dir():
        filesystem_allows_probe = False
        mount_is_safe = False
        mount = _mount_fact(data_root, runner)
        if mount is None:
            _result(results, "data_mount", "fail", "DATA_ROOT mount facts are unavailable")
        else:
            mount_target, filesystem = mount
            try:
                target_path = Path(mount_target).resolve()
            except OSError:
                target_path = Path(mount_target)
            try:
                data_root.relative_to(target_path)
                under_mount = True
            except ValueError:
                under_mount = False
            if target_path == Path("/"):
                _result(
                    results,
                    "data_mount",
                    "fail",
                    "DATA_ROOT is only on the root filesystem (fallback refused)",
                )
            elif not under_mount:
                _result(results, "data_mount", "fail", "DATA_ROOT mount fact does not contain the configured path")
            else:
                mount_is_safe = True
                _result(results, "data_mount", "pass", "DATA_ROOT is backed by a non-root mount")
            if filesystem in _UNSUPPORTED_FILESYSTEMS:
                _result(
                    results,
                    "data_filesystem",
                    "fail",
                    f"DATA_ROOT filesystem {filesystem} is not supported for hardlinks",
                )
            else:
                filesystem_allows_probe = mount_is_safe
                _result(
                    results,
                    "data_filesystem",
                    "pass",
                    f"DATA_ROOT filesystem {filesystem} is not known to prevent hardlinks",
                )

        try:
            stat = data_root.stat()
        except OSError:
            _result(results, "data_ownership", "fail", "DATA_ROOT ownership is unavailable")
        else:
            if puid is not None and pgid is not None and stat.st_uid == puid and stat.st_gid == pgid:
                _result(results, "data_ownership", "pass", "DATA_ROOT ownership matches PUID/PGID")
            elif puid is not None and pgid is not None:
                _result(results, "data_ownership", "fail", "DATA_ROOT ownership does not match PUID/PGID")

        torrents = data_root / "torrents"
        media = data_root / "media"
        missing = [name for name, path in (("torrents", torrents), ("media", media)) if not path.is_dir()]
        if missing:
            _result(results, "hardlink", "fail", "Required DATA_ROOT directories are absent: " + ", ".join(missing))
        elif filesystem_allows_probe:
            token = uuid.uuid4().hex
            source = torrents / f".homeflix-preflight-{token}"
            link = media / f".homeflix-preflight-{token}"
            try:
                with source.open("x", encoding="utf-8") as stream:
                    stream.write("homeflix preflight\n")
                os.link(source, link)
                if source.stat().st_ino == link.stat().st_ino:
                    _result(results, "hardlink", "pass", "Hardlink probe shares one inode")
                else:
                    _result(results, "hardlink", "fail", "Hardlink probe did not share one inode")
            except OSError:
                _result(results, "hardlink", "fail", "Hardlink probe failed")
            finally:
                for probe_path in (link, source):
                    try:
                        probe_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    return PreflightReport(phase, tuple(results))
