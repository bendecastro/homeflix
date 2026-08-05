"""Phase-aware, fail-closed checks for a configured Homeflix host."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping, Protocol, Sequence
import uuid

from .envfile import EnvDocument


_STATUSES = {"pass", "warn", "fail"}
# These common Linux filesystems are recommended, but this set only controls whether
# an otherwise permitted filesystem receives an informational warning. The real link
# probe remains authoritative for both recommended and unrecognized filesystems.
_RECOMMENDED_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs"}
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
    return Path(os.path.abspath(Path(value).expanduser()))


def _is_real_directory(path: Path) -> bool:
    """Reject a symlink at any point in a configured directory chain."""

    try:
        return path.is_dir() and path.resolve(strict=True) == path
    except OSError:
        return False


def _environment_path(config: EnvDocument | Mapping[str, object]) -> Path | None:
    if isinstance(config, EnvDocument):
        return config.source_path
    value = config.get("_ENV_FILE")
    if not isinstance(value, (str, os.PathLike)):
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


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.lstat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing a destination on supported Linux hosts."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(source),
            str(destination),
        )


def _cleanup_owned_probe(
    path: Path, *, created: bool, identity: tuple[int, int] | None
) -> tuple[bool, str | None]:
    """Atomically quarantine a live name before deciding whether it is ours."""

    if not created:
        return True, None
    if identity is None:
        return False, f"probe identity unavailable at {path}"

    quarantine: Path | None = None
    for _attempt in range(3):
        candidate = path.parent / f".homeflix-quarantine-{uuid.uuid4().hex}"
        try:
            _rename_noreplace(path, candidate)
        except FileNotFoundError:
            return True, None
        except FileExistsError:
            continue
        except OSError:
            return False, f"could not quarantine probe path {path}"
        quarantine = candidate
        break
    if quarantine is None:
        return False, f"could not allocate quarantine path for {path}"

    captured_identity = _file_identity(quarantine)
    if captured_identity != identity:
        try:
            _rename_noreplace(quarantine, path)
        except FileExistsError:
            return False, f"foreign probe inode retained at {quarantine}; original path is occupied"
        except OSError:
            return False, f"foreign probe inode retained at {quarantine}; restore failed"
        return False, f"foreign probe inode restored at {path}"

    try:
        quarantine.unlink()
    except OSError:
        return False, f"owned probe quarantine could not be removed at {quarantine}"
    if quarantine.exists():
        return False, f"owned probe quarantine remains at {quarantine}"
    return True, None


def run_preflight(
    config: EnvDocument | Mapping[str, object], phase: str, runner: Runner
) -> PreflightReport:
    """Run bounded checks without creating any missing deployment directories."""

    if phase not in {"core", "acquisition"}:
        raise ValueError("phase must be 'core' or 'acquisition'")

    results: list[CheckResult] = []
    roots: dict[str, Path | None] = {}
    data_root_is_real = False
    for key in ("DATA_ROOT", "CONFIG_ROOT", "CACHE_ROOT"):
        path = _path(config, key)
        roots[key] = path
        if path is None:
            _result(results, key.casefold(), "fail", f"{key} must be a non-empty path")
        elif not path.is_dir():
            _result(results, key.casefold(), "fail", f"{key} directory is absent")
        elif key == "DATA_ROOT" and not _is_real_directory(path):
            _result(results, "data_root", "fail", "DATA_ROOT must not contain symlinks")
        else:
            if key == "DATA_ROOT":
                data_root_is_real = True
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

    environment_path = _environment_path(config)
    if environment_path is None:
        _result(results, "compose_config", "fail", "Compose configuration location is unavailable")
    else:
        compose_command = (
            "docker",
            "compose",
            "--project-directory",
            str(environment_path.parent),
            "--env-file",
            str(environment_path),
            "config",
            "--quiet",
        )
        try:
            compose_result = runner.run(compose_command, check=False, timeout=30)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            compose_result = None
        if compose_result is not None and compose_result.returncode == 0:
            _result(results, "compose_config", "pass", "Compose configuration is valid")
        else:
            _result(results, "compose_config", "fail", "Compose configuration validation failed")

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
    if data_root is not None and data_root_is_real:
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
                if filesystem in _RECOMMENDED_FILESYSTEMS:
                    _result(
                        results,
                        "data_filesystem",
                        "pass",
                        f"DATA_ROOT filesystem {filesystem} is recommended",
                    )
                else:
                    _result(
                        results,
                        "data_filesystem",
                        "warn",
                        f"DATA_ROOT filesystem {filesystem} is unrecognized; relying on the hardlink probe",
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
        required_paths = [("torrents", torrents), ("media", media)]
        if phase == "acquisition":
            required_paths.append(("usenet", data_root / "usenet"))
        absent = [name for name, path in required_paths if not path.is_dir()]
        linked = [name for name, path in required_paths if path.is_dir() and not _is_real_directory(path)]
        layout_is_real = not absent and not linked
        if not layout_is_real:
            details = []
            if absent:
                details.append("absent: " + ", ".join(absent))
            if linked:
                details.append("symlinked: " + ", ".join(linked))
            _result(results, "data_layout", "fail", "Required DATA_ROOT directories are invalid (" + "; ".join(details) + ")")
        elif filesystem_allows_probe:
            token = uuid.uuid4().hex
            source = torrents / f".homeflix-preflight-{token}"
            link = media / f".homeflix-preflight-{token}"
            source_created = False
            link_created = False
            source_identity: tuple[int, int] | None = None
            link_identity: tuple[int, int] | None = None
            try:
                with source.open("x", encoding="utf-8") as stream:
                    source_created = True
                    source_stat = os.fstat(stream.fileno())
                    source_identity = (source_stat.st_dev, source_stat.st_ino)
                    stream.write("homeflix preflight\n")
                os.link(source, link)
                link_created = True
                observed_link_identity = _file_identity(link)
                # A hardlink created from our source must have its recorded identity.
                # Retain that expected identity so a raced replacement is never removed.
                link_identity = source_identity
                if source_identity is not None and source_identity == observed_link_identity:
                    _result(results, "hardlink", "pass", "Hardlink probe shares one device and inode")
                else:
                    _result(results, "hardlink", "fail", "Hardlink probe did not share one device and inode")
            except OSError:
                _result(results, "hardlink", "fail", "Hardlink probe failed")
            finally:
                link_clean, link_cleanup_error = _cleanup_owned_probe(
                    link, created=link_created, identity=link_identity
                )
                source_clean, source_cleanup_error = _cleanup_owned_probe(
                    source, created=source_created, identity=source_identity
                )
                if not link_clean or not source_clean:
                    cleanup_errors = [
                        error
                        for error in (link_cleanup_error, source_cleanup_error)
                        if error is not None
                    ]
                    _result(
                        results,
                        "hardlink_cleanup",
                        "fail",
                        "Hardlink probe cleanup failed: " + "; ".join(cleanup_errors),
                    )

    return PreflightReport(phase, tuple(results))
