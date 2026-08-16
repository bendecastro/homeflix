"""Fail-closed CONFIG_ROOT backup and scratch restore."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from typing import Callable, Protocol

from .command import CommandRunner
from .envfile import EnvDocument


SCHEMA_VERSION = 1
ARCHIVE_RE = re.compile(r"^homeflix-config-[A-Za-z0-9._-]+\.tar\.gz$")
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SSH_COMMAND_TIMEOUT = 60.0


class BackupError(Exception):
    def __init__(self, message: str, *, code: str = "backup_refused") -> None:
        super().__init__(message)
        self.code = code


class ArtifactRepository(Protocol):
    def list_archives(self) -> list[str]: ...

    def get(self, name: str, destination: Path) -> None: ...

    def put(self, source: Path) -> None: ...

    def prune(self, keep: int) -> None: ...


def dest_is_remote(value: str) -> bool:
    return ":" in value and not value.startswith("/") and not value.startswith(".")


_SSH_USER = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
_SSH_HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
_SSH_COMPONENT = r"[A-Za-z0-9._-]+"
_SSH_DEST_RE = re.compile(rf"^({_SSH_USER})@({_SSH_HOST}):(/{_SSH_COMPONENT}(?:/{_SSH_COMPONENT})*)$")


class SshDestination:
    def __init__(self, user: str, host: str, path: str) -> None:
        self.user = user
        self.host = host
        self.path = path

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    @property
    def spec(self) -> str:
        return f"{self.user}@{self.host}:{self.path}"

    def remote_archive(self, name: str) -> str:
        return f"{self.user}@{self.host}:{self.path}/{name}"

    def remote_path(self, name: str) -> str:
        return f"{self.path}/{name}"


def parse_ssh_destination(value: str) -> SshDestination:
    if not value or value.startswith("-") or value.count(":") != 1:
        raise BackupError("BACKUP_DEST is not a valid SSH destination")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BackupError("BACKUP_DEST is not a valid SSH destination")
    match = _SSH_DEST_RE.fullmatch(value)
    if match is None:
        raise BackupError("BACKUP_DEST is not a valid SSH destination")
    user, host, path = match.group(1), match.group(2), match.group(3)
    parts = path.split("/")[1:]
    if any(part in {".", ".."} or ".." in part for part in parts):
        raise BackupError("BACKUP_DEST is not a valid SSH destination")
    return SshDestination(user, host, path)


class LocalArtifactRepository:
    """Store Homeflix backup artifacts on a local filesystem path."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list_archives(self) -> list[str]:
        if not self.root.is_dir():
            return []
        entries = [
            path
            for path in self.root.iterdir()
            if path.is_file() and ARCHIVE_RE.fullmatch(path.name)
        ]
        entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [path.name for path in entries]

    def get(self, name: str, destination: Path) -> None:
        source = self.root / _safe_archive_name(name)
        if not source.is_file():
            raise BackupError(f"archive {name} was not found")
        shutil.copy2(source, destination)

    def put(self, source: Path) -> None:
        name = _safe_archive_name(source.name)
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / name
        fd, temporary_name = tempfile.mkstemp(prefix=".homeflix-put.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise

    def prune(self, keep: int) -> None:
        if keep < 1:
            raise BackupError("BACKUP_KEEP must be a positive integer")
        for name in self.list_archives()[keep:]:
            (self.root / name).unlink(missing_ok=True)


class SshArtifactRepository:
    """Store Homeflix backup artifacts at a validated user@host:/abs/path destination."""

    def __init__(self, destination: SshDestination, runner: CommandRunner) -> None:
        self.destination = destination
        self.runner = runner

    def _secrets(self) -> tuple[str, ...]:
        dest = self.destination
        return (dest.spec, dest.target, dest.user, dest.host, dest.path)

    def _run(self, argv: list[str], *, require_success: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner.run(argv, redact=self._secrets(), timeout=SSH_COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise BackupError("SSH transfer timed out") from error
        if require_success and result.returncode != 0:
            raise BackupError("SSH transfer failed")
        return result

    def _ssh(self, remote: list[str], *, require_success: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run(
            ["ssh", "-oBatchMode=yes", "--", self.destination.target, *remote],
            require_success=require_success,
        )

    def list_archives(self) -> list[str]:
        result = self._ssh(["ls", "-1t", "--", self.destination.path], require_success=False)
        if result.returncode == 255:
            raise BackupError("SSH transfer failed")
        if result.returncode != 0:
            return []
        names = []
        for line in result.stdout.splitlines():
            name = line.strip()
            if ARCHIVE_RE.fullmatch(name):
                names.append(name)
        return names

    def get(self, name: str, destination: Path) -> None:
        safe = _safe_archive_name(name)
        self._run(["scp", "-oBatchMode=yes", "--", self.destination.remote_archive(safe), str(destination)])

    def put(self, source: Path) -> None:
        name = _safe_archive_name(source.name)
        self._run(["scp", "-oBatchMode=yes", "--", str(source), self.destination.remote_archive(name)])

    def prune(self, keep: int) -> None:
        if keep < 1:
            raise BackupError("BACKUP_KEEP must be a positive integer")
        for name in self.list_archives()[keep:]:
            self._ssh(["rm", "-f", "--", self.destination.remote_path(name)])


def _safe_archive_name(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name or not ARCHIVE_RE.fullmatch(name):
        raise BackupError("archive name must match homeflix-config-*.tar.gz")
    return name


def _is_log_path(relative: Path) -> bool:
    parts = relative.parts
    if "logs" in parts:
        return True
    name = relative.name
    return name.endswith(".log") or ".log." in name


def _is_wal_or_shm(name: str) -> bool:
    return name.endswith("-wal") or name.endswith("-shm")


def _is_sqlite_path(path: Path) -> bool:
    return path.suffix in SQLITE_SUFFIXES and path.is_file()


def _existing_device(path: Path) -> int:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current.stat().st_dev


def _load_backup_settings(
    repository_root: Path, *, require_config: bool = False
) -> tuple[Path | None, str, int, Path | None]:
    env_path = repository_root / ".env"
    if not env_path.is_file():
        raise BackupError("no .env at repository root")
    document = EnvDocument.load(env_path)
    config_root = document.get("CONFIG_ROOT") or ""
    backup_dest = document.get("BACKUP_DEST") or ""
    keep_text = document.get("BACKUP_KEEP") or "7"
    data_root_text = document.get("DATA_ROOT") or ""
    if not backup_dest:
        raise BackupError("BACKUP_DEST is empty")
    if not keep_text.isdigit() or int(keep_text) < 1:
        raise BackupError("BACKUP_KEEP must be a positive integer")
    config: Path | None = Path(config_root) if config_root else None
    if require_config:
        if config is None:
            raise BackupError("CONFIG_ROOT is empty")
        if not config.is_dir():
            raise BackupError("CONFIG_ROOT is not a directory")
    data_root = Path(data_root_text) if data_root_text else None
    return config, backup_dest, int(keep_text), data_root


def local_repository(dest: str, *, data_root: Path | None) -> LocalArtifactRepository:
    if dest_is_remote(dest):
        raise BackupError("SSH destinations are not supported by the local backup adapter")
    path = Path(dest)
    if data_root is not None and data_root.exists() and _existing_device(path) == data_root.stat().st_dev:
        raise BackupError("BACKUP_DEST is on the same filesystem as DATA_ROOT — that is not an off-box backup")
    return LocalArtifactRepository(path)


def open_repository(
    dest: str,
    *,
    data_root: Path | None,
    runner: CommandRunner | None = None,
) -> ArtifactRepository:
    if dest.startswith("-") or dest_is_remote(dest):
        return SshArtifactRepository(parse_ssh_destination(dest), runner or CommandRunner())
    return local_repository(dest, data_root=data_root)


def snapshot_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


def _copy_tree_with_snapshots(config_root: Path, staging: Path) -> int:
    sqlite_count = 0
    for current, dirnames, filenames in os.walk(config_root):
        relative_dir = Path(current).relative_to(config_root)
        dirnames[:] = [name for name in dirnames if name != "logs"]
        target_dir = staging if relative_dir == Path(".") else staging / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = Path(current) / name
            relative = source.relative_to(config_root)
            if name == ".env" or _is_log_path(relative) or _is_wal_or_shm(name):
                continue
            if not source.is_file() or source.is_symlink():
                continue
            target = staging / relative
            if _is_sqlite_path(source):
                try:
                    snapshot_sqlite(source, target)
                except sqlite3.Error as error:
                    raise BackupError(f"SQLite snapshot failed for {relative.as_posix()}") from error
                sqlite_count += 1
            else:
                shutil.copy2(source, target)
    return sqlite_count


def _write_archive(staging: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in sorted(staging.rglob("*")):
            relative = path.relative_to(staging).as_posix()
            archive.add(path, arcname=relative, recursive=False, filter=_regular_member)


def _regular_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if member.issym() or member.islnk() or member.isfifo() or member.isdev():
        return None
    return member


def create_backup(
    repository_root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    config_root, backup_dest, keep, data_root = _load_backup_settings(repository_root, require_config=True)
    assert config_root is not None
    repository = open_repository(backup_dest, data_root=data_root, runner=runner)
    stamp = (clock or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"homeflix-config-{stamp}.tar.gz"
    staging_dir = tempfile.mkdtemp(prefix="homeflix-backup.")
    work_dir = tempfile.mkdtemp(prefix="homeflix-backup-out.")
    try:
        sqlite_count = _copy_tree_with_snapshots(config_root, Path(staging_dir))
        archive_path = Path(work_dir) / archive_name
        _write_archive(Path(staging_dir), archive_path)
        repository.put(archive_path)
        repository.prune(keep)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "created",
        "archive": archive_name,
        "sqlite": sqlite_count,
        "keep": keep,
        "dest": "set",
    }


def _repository_from_env(
    repository_root: Path, *, runner: CommandRunner | None = None
) -> tuple[ArtifactRepository, int]:
    _config_root, backup_dest, keep, data_root = _load_backup_settings(repository_root)
    return open_repository(backup_dest, data_root=data_root, runner=runner), keep


def list_backups(repository_root: Path, *, runner: CommandRunner | None = None) -> dict[str, object]:
    repository, _keep = _repository_from_env(repository_root, runner=runner)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "listed",
        "archives": repository.list_archives(),
        "dest": "set",
    }


def retrieve_backup(
    repository_root: Path,
    *,
    archive: str,
    destination: str,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    repository, _keep = _repository_from_env(repository_root, runner=runner)
    name = _safe_archive_name(archive)
    target = Path(destination)
    if target.exists() and target.is_dir():
        target = target / name
    target.parent.mkdir(parents=True, exist_ok=True)
    repository.get(name, target)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "retrieved",
        "archive": name,
        "dest": "set",
    }


def prune_backups(repository_root: Path, *, runner: CommandRunner | None = None) -> dict[str, object]:
    repository, keep = _repository_from_env(repository_root, runner=runner)
    repository.prune(keep)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pruned",
        "keep": keep,
        "dest": "set",
    }


def _restore_error(message: str) -> BackupError:
    return BackupError(message, code="restore_refused")


def _validate_member(member: tarfile.TarInfo, scratch: Path) -> None:
    name = member.name.replace("\\", "/")
    if not name or name.startswith("/") or name.startswith("~"):
        raise _restore_error("archive member is not safe to extract")
    parts = Path(name).parts
    if any(part == ".." for part in parts):
        raise _restore_error("archive member is not safe to extract")
    if member.issym() or member.islnk():
        raise _restore_error("archive contains an unsafe link")
    if member.isfifo() or member.isdev() or member.ischr() or member.isblk():
        raise _restore_error("archive contains a special file")
    if not (member.isfile() or member.isdir()):
        raise _restore_error("archive contains a special file")
    destination = (scratch / name).resolve()
    try:
        destination.relative_to(scratch.resolve())
    except ValueError as error:
        raise _restore_error("archive member would extract outside the scratch destination") from error


def _validate_archive(archive_path: Path, scratch: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
    except tarfile.TarError as error:
        raise _restore_error("archive is not a readable Homeflix backup") from error
    if not members:
        raise _restore_error("archive is empty")
    for member in members:
        _validate_member(member, scratch)


def _extract_archive(archive_path: Path, scratch: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(scratch, filter="data")


def _sqlite_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix in SQLITE_SUFFIXES:
            found.append(path)
    return sorted(found)


def _integrity_ok(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == "ok"


def restore_backup(
    repository_root: Path,
    *,
    destination: str,
    archive: str | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    config_root, backup_dest, _keep, data_root = _load_backup_settings(repository_root)
    repository = open_repository(backup_dest, data_root=data_root, runner=runner)
    scratch = Path(destination)
    if not destination:
        raise _restore_error("pass --to DIR (scratch only; refuses live CONFIG_ROOT)")
    if config_root is not None and scratch.resolve() == config_root.resolve():
        raise _restore_error("refusing to restore over live CONFIG_ROOT — pick a scratch directory")
    if scratch.exists() and any(scratch.iterdir()):
        raise _restore_error("--to directory is not empty")
    names = repository.list_archives()
    name = archive or (names[0] if names else "")
    if not name:
        raise _restore_error("no archives found at BACKUP_DEST")
    try:
        name = _safe_archive_name(name)
    except BackupError as error:
        raise _restore_error(str(error)) from error
    scratch.mkdir(parents=True, exist_ok=True)
    if any(scratch.iterdir()):
        raise _restore_error("--to directory is not empty")
    handle, fetch_name = tempfile.mkstemp(prefix="homeflix-restore.", suffix=".tar.gz")
    os.close(handle)
    fetch = Path(fetch_name)
    try:
        try:
            repository.get(name, fetch)
        except BackupError as error:
            raise _restore_error(str(error)) from error
        _validate_archive(fetch, scratch)
        _extract_archive(fetch, scratch)
        ok = 0
        fail = 0
        databases: list[str] = []
        for path in _sqlite_files(scratch):
            relative = path.relative_to(scratch).as_posix()
            if _integrity_ok(path):
                ok += 1
                databases.append(relative)
            else:
                fail += 1
        if fail:
            raise _restore_error("restored SQLite integrity check failed")
        if ok < 1:
            raise _restore_error("successful restore requires at least one SQLite database")
    finally:
        fetch.unlink(missing_ok=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "restored",
        "archive": name,
        "sqlite_ok": ok,
        "sqlite_fail": fail,
        "dest": "set",
        "databases": databases,
    }
