from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import subprocess

from scripts.homeflix_setup.backup import (
    BackupError,
    LocalArtifactRepository,
    SshArtifactRepository,
    dest_is_remote,
    open_repository,
    parse_ssh_destination,
)
from scripts.homeflix_setup.cli import main
from tests.helpers import REPOSITORY_ROOT, parse_single_json


def run_main(*args: str, repository_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = main(args, repository_root=repository_root)
    return return_code, stdout.getvalue(), stderr.getvalue()


def write_env(
    root: Path,
    *,
    config_root: Path,
    backup_dest: Path | str,
    keep: int = 7,
    data_root: Path | None = None,
) -> None:
    lines = [
        f"CONFIG_ROOT={config_root}",
        f"BACKUP_DEST={backup_dest}",
        f"BACKUP_KEEP={keep}",
    ]
    if data_root is not None:
        lines.append(f"DATA_ROOT={data_root}")
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / ".env").chmod(0o600)


def create_wal_database(path: Path, values: tuple[int, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        connection.executemany("INSERT INTO items(value) VALUES (?)", ((value,) for value in values))
        connection.commit()
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            raise AssertionError(f"expected WAL journal, got {mode!r}")
        if not path.with_name(path.name + "-wal").exists():
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        connection.close()


def sqlite_values(path: Path) -> list[int]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT value FROM items ORDER BY id").fetchall()
        check = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if check is None or check[0] != "ok":
        raise AssertionError(f"integrity_check failed: {check}")
    return [int(row[0]) for row in rows]


def archive_members(path: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(path, "r:gz") as archive:
        return list(archive.getmembers())


class BackupCreateCliTests(unittest.TestCase):
    def test_json_create_snapshots_wal_sqlite_and_excludes_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            db_path = config_root / "radarr" / "radarr.db"
            db_path.parent.mkdir(parents=True)
            live = sqlite3.connect(db_path)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
            live.execute("INSERT INTO items(value) VALUES (1)")
            live.commit()
            live.execute("INSERT INTO items(value) VALUES (2)")
            live.execute("INSERT INTO items(value) VALUES (3)")
            live.commit()
            self.assertEqual(str(live.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")
            self.assertTrue(Path(str(db_path) + "-wal").exists())
            raw_copy = root / "naive-copy.db"
            shutil.copy2(db_path, raw_copy)
            (config_root / "radarr" / "radarr.debug.log").write_text("noise\n", encoding="utf-8")
            (config_root / "logs" / "sonarr.txt").parent.mkdir(parents=True)
            (config_root / "logs" / "sonarr.txt").write_text("log\n", encoding="utf-8")
            (config_root / "jellyfin" / "config.xml").parent.mkdir(parents=True)
            (config_root / "jellyfin" / "config.xml").write_text("<ok/>\n", encoding="utf-8")
            (config_root / ".env").write_text("JELLYFIN_ADMIN_PASSWORD=must-not-archive\n", encoding="utf-8")
            write_env(root, config_root=config_root, backup_dest=backup_dest)

            try:
                code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)
            finally:
                live.close()

            self.assertEqual(code, 0, stderr + stdout)
            self.assertEqual(stderr, "")
            payload = parse_single_json(stdout)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["status"], "created")
            self.assertEqual(payload["sqlite"], 1)
            self.assertEqual(payload["keep"], 7)
            self.assertEqual(payload["dest"], "set")
            self.assertNotIn(str(backup_dest), stdout)
            archive_name = payload["archive"]
            self.assertRegex(str(archive_name), r"^homeflix-config-\d{8}T\d{6}Z\.tar\.gz$")
            published = backup_dest / str(archive_name)
            self.assertTrue(published.is_file())
            self.assertEqual(list(backup_dest.iterdir()), [published])

            members = archive_members(published)
            names = {member.name.lstrip("./") for member in members if member.isfile()}
            self.assertIn("radarr/radarr.db", names)
            self.assertIn("jellyfin/config.xml", names)
            self.assertNotIn(".env", names)
            self.assertNotIn("radarr/radarr.debug.log", names)
            self.assertNotIn("must-not-archive", stdout + stderr)
            self.assertTrue(all("logs/" not in name for name in names))
            self.assertTrue(all(not name.endswith(("-wal", "-shm")) for name in names))
            self.assertTrue(all(member.isreg() or member.isdir() for member in members))
            self.assertFalse(any(member.issym() or member.islnk() or member.isfifo() or member.isdev() for member in members))

            with tarfile.open(published, "r:gz") as archive:
                extracted = archive.extractfile("radarr/radarr.db")
                assert extracted is not None
                restored = root / "extracted.db"
                restored.write_bytes(extracted.read())
            self.assertEqual(sqlite_values(restored), [1, 2, 3])
            naive = sqlite3.connect(raw_copy)
            try:
                naive_values = [int(row[0]) for row in naive.execute("SELECT value FROM items ORDER BY id")]
            except sqlite3.Error:
                naive_values = []
            finally:
                naive.close()
            self.assertNotEqual(naive_values, [1, 2, 3], "live-file copy must not be enough; WAL backup is required")

    def test_snapshot_failure_aborts_publication_and_retention_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            (config_root / "sonarr" / "sonarr.db").parent.mkdir(parents=True)
            (config_root / "sonarr" / "sonarr.db").write_bytes(b"not a sqlite database")
            existing = backup_dest / "homeflix-config-20200101T000000Z.tar.gz"
            existing.write_bytes(b"keep-me")
            foreign = backup_dest / "notes.txt"
            foreign.write_text("foreign\n", encoding="utf-8")
            before = {path.name: path.read_bytes() for path in backup_dest.iterdir()}
            write_env(root, config_root=config_root, backup_dest=backup_dest, keep=1)

            code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)

            self.assertEqual(code, 1)
            payload = parse_single_json(stdout)
            self.assertEqual(payload["error"]["code"], "backup_refused")
            self.assertIn("snapshot", payload["error"]["message"].casefold())
            after = {path.name: path.read_bytes() for path in backup_dest.iterdir()}
            self.assertEqual(after, before)
            self.assertEqual(set(after), {"homeflix-config-20200101T000000Z.tar.gz", "notes.txt"})
            self.assertFalse(any(path.name.endswith(".tmp") for path in backup_dest.iterdir()))
            self.assertNotIn(str(backup_dest), stdout + stderr)


class BackupDestinationGuardTests(unittest.TestCase):
    def test_same_filesystem_dest_is_refused_when_present_or_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root = root / "data"
            config_root = root / "config"
            data_root.mkdir()
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            present = data_root / "backups"
            present.mkdir()
            absent = data_root / "missing" / "backups"
            for dest in (present, absent):
                with self.subTest(dest_exists=dest.exists()):
                    write_env(root, config_root=config_root, backup_dest=dest, data_root=data_root)
                    before = list(present.iterdir()) if dest == present else []
                    code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)
                    self.assertEqual(code, 1)
                    payload = parse_single_json(stdout)
                    self.assertEqual(payload["error"]["code"], "backup_refused")
                    self.assertRegex(payload["error"]["message"], r"same filesystem|DATA_ROOT")
                    self.assertFalse(absent.exists())
                    if dest == present:
                        self.assertEqual(list(present.iterdir()), before)
                    self.assertNotIn(str(dest), stdout + stderr)

    def test_remote_dest_is_not_treated_as_a_local_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            remote = "user@host:/var/backups/homeflix"
            write_env(root, config_root=config_root, backup_dest=remote, keep=1)
            store = root / "remote-store"
            store.mkdir()
            stale = store / "homeflix-config-20200101T000000Z.tar.gz"
            stale.write_bytes(b"stale")
            runner = FakeSshRunner(store)

            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)

            self.assertEqual(code, 0, stderr + stdout)
            payload = parse_single_json(stdout)
            self.assertEqual(payload["status"], "created")
            self.assertEqual(payload["dest"], "set")
            self.assertFalse((root / "user@host:/var/backups/homeflix").exists())
            self.assertFalse(any(path.name == "user@host:" or "user@host" in path.name for path in root.rglob("*") if path != root / ".env"))
            self.assertNotIn("/var/backups/homeflix", stdout + stderr)
            self.assertNotIn("user@", stdout + stderr)
            archive_name = str(payload["archive"])
            self.assertTrue((store / archive_name).is_file())
            self.assertFalse(stale.exists())
            self.assertEqual(runner.operations()[0], "put")
            self.assertIn("prune", runner.operations())
            self.assertLess(runner.operations().index("put"), runner.operations().index("prune"))

    def test_remote_put_failure_does_not_prune_or_mkdir_dest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            write_env(root, config_root=config_root, backup_dest=VALID_SSH_DEST)
            store = root / "remote-store"
            store.mkdir()
            existing = store / "homeflix-config-20200101T000000Z.tar.gz"
            existing.write_bytes(b"keep")
            foreign = store / "notes.txt"
            foreign.write_text("foreign\n", encoding="utf-8")
            before = {path.name: path.read_bytes() for path in store.iterdir()}
            runner = FakeSshRunner(store)
            runner.fail_at = "put"

            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)

            self.assertEqual(code, 1)
            payload = parse_single_json(stdout)
            self.assertEqual(payload["error"]["code"], "backup_refused")
            self.assertNotIn("prune", runner.operations())
            self.assertEqual({path.name: path.read_bytes() for path in store.iterdir()}, before)
            self.assertFalse((root / "user@host:/var/backups/homeflix").exists())
            self.assertNotIn("user@", stdout + stderr)
            self.assertNotIn("/var/backups/homeflix", stdout + stderr)

    def test_json_ssh_list_retrieve_restore_are_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            create_wal_database(config_root / "radarr" / "radarr.db", (7, 8))
            write_env(root, config_root=config_root, backup_dest=VALID_SSH_DEST)
            store = root / "remote-store"
            store.mkdir()
            runner = FakeSshRunner(store)
            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)
            self.assertEqual(code, 0, stderr + stdout)
            archive_name = str(parse_single_json(stdout)["archive"])

            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main("--json", "backup", "list", repository_root=root)
            self.assertEqual(code, 0, stderr)
            listed = parse_single_json(stdout)
            self.assertEqual(listed["status"], "listed")
            self.assertEqual(listed["dest"], "set")
            self.assertEqual(listed["archives"], [archive_name])
            self.assertNotIn("user@", stdout + stderr)
            self.assertNotIn("/var/backups/homeflix", stdout + stderr)

            fetched = root / "fetched.tar.gz"
            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main(
                    "--json", "backup", "retrieve", "--archive", archive_name, "--to", str(fetched),
                    repository_root=root,
                )
            self.assertEqual(code, 0, stderr)
            retrieved = parse_single_json(stdout)
            self.assertEqual(retrieved["dest"], "set")
            self.assertTrue(fetched.is_file())
            self.assertNotIn("user@", stdout + stderr)
            self.assertNotEqual(fetched.read_bytes(), b"")

            scratch = root / "scratch"
            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main(
                    "--json", "backup", "restore", "--to", str(scratch),
                    repository_root=root,
                )
            self.assertEqual(code, 0, stderr + stdout)
            restored = parse_single_json(stdout)
            self.assertEqual(restored["status"], "restored")
            self.assertEqual(restored["dest"], "set")
            self.assertEqual(sqlite_values(scratch / "radarr" / "radarr.db"), [7, 8])
            self.assertNotIn("user@", stdout + stderr)
            self.assertNotIn("/var/backups/homeflix", stdout + stderr)
            self.assertNotIn("payload", stdout + stderr)

            with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                code, stdout, stderr = run_main(
                    "backup", "restore", "--to", str(root / "human-scratch"),
                    repository_root=root,
                )
            self.assertEqual(code, 0, stderr + stdout)
            self.assertIn(f"dest={root / 'human-scratch'}", stdout)
            self.assertNotIn(VALID_SSH_DEST, stdout + stderr)
            self.assertNotIn("user@host", stdout + stderr)

    def test_invalid_remote_dest_forms_refuse_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            runner = FakeSshRunner(root / "unused")
            for dest in (
                "user@host:/foo/../bar",
                "scp://user@host/var/backups",
                "user@2001:db8::1:/var/backups/homeflix",
                "-oProxyCommand=evil",
            ):
                with self.subTest(dest=dest):
                    write_env(root, config_root=config_root, backup_dest=dest)
                    runner.calls.clear()
                    with patch("scripts.homeflix_setup.backup.CommandRunner", return_value=runner):
                        code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)
                    self.assertEqual(code, 1)
                    payload = parse_single_json(stdout)
                    self.assertEqual(payload["error"]["code"], "backup_refused")
                    self.assertEqual(runner.calls, [])
                    self.assertFalse((root / dest).exists())
                    self.assertNotIn("user@", stdout + stderr)
                    if dest != "-oProxyCommand=evil":
                        self.assertNotIn(dest, stdout + stderr)


class BackupRepositoryCliTests(unittest.TestCase):
    def test_json_list_retrieve_and_prune_are_schema_versioned_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            older = backup_dest / "homeflix-config-20200101T000000Z.tar.gz"
            newer = backup_dest / "homeflix-config-20200202T000000Z.tar.gz"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            (backup_dest / "notes.txt").write_text("foreign\n", encoding="utf-8")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))
            write_env(root, config_root=config_root, backup_dest=backup_dest, keep=1)
            config_root.mkdir()

            code, stdout, stderr = run_main("--json", "backup", "list", repository_root=root)
            self.assertEqual(code, 0, stderr)
            listed = parse_single_json(stdout)
            self.assertEqual(listed["schema_version"], 1)
            self.assertEqual(listed["status"], "listed")
            self.assertEqual(listed["dest"], "set")
            self.assertEqual(listed["archives"], [newer.name, older.name])
            self.assertNotIn(str(backup_dest), stdout)

            retrieved = root / "fetched.tar.gz"
            code, stdout, stderr = run_main(
                "--json", "backup", "retrieve", "--archive", newer.name, "--to", str(retrieved),
                repository_root=root,
            )
            self.assertEqual(code, 0, stderr)
            payload = parse_single_json(stdout)
            self.assertEqual(payload["status"], "retrieved")
            self.assertEqual(payload["archive"], newer.name)
            self.assertEqual(payload["dest"], "set")
            self.assertEqual(retrieved.read_bytes(), b"new")
            self.assertNotIn(str(backup_dest), stdout)

            code, stdout, stderr = run_main("--json", "backup", "prune", repository_root=root)
            self.assertEqual(code, 0, stderr)
            pruned = parse_single_json(stdout)
            self.assertEqual(pruned["status"], "pruned")
            self.assertEqual(pruned["keep"], 1)
            self.assertEqual(pruned["dest"], "set")
            self.assertTrue(newer.is_file())
            self.assertFalse(older.exists())
            self.assertTrue((backup_dest / "notes.txt").is_file())


class BackupRestoreCliTests(unittest.TestCase):
    def test_json_restore_verifies_every_sqlite_and_requires_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            create_wal_database(config_root / "radarr" / "radarr.db", (4, 5))
            create_wal_database(config_root / "sonarr" / "sonarr.sqlite", (6,))
            write_env(root, config_root=config_root, backup_dest=backup_dest)
            code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)
            self.assertEqual(code, 0, stderr)
            archive_name = parse_single_json(stdout)["archive"]
            scratch = root / "scratch"

            code, stdout, stderr = run_main(
                "--json", "backup", "restore", "--to", str(scratch),
                repository_root=root,
            )
            self.assertEqual(code, 0, stderr + stdout)
            payload = parse_single_json(stdout)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["status"], "restored")
            self.assertEqual(payload["archive"], archive_name)
            self.assertEqual(payload["sqlite_ok"], 2)
            self.assertEqual(payload["sqlite_fail"], 0)
            self.assertEqual(payload["dest"], "set")
            self.assertNotIn(str(backup_dest), stdout)
            self.assertEqual(sqlite_values(scratch / "radarr" / "radarr.db"), [4, 5])
            self.assertEqual(sqlite_values(scratch / "sonarr" / "sonarr.sqlite"), [6])

            empty = root / "empty-scratch"
            empty.mkdir()
            (backup_dest / str(archive_name)).unlink()
            code, stdout, stderr = run_main(
                "--json", "backup", "restore", "--to", str(empty), "--archive", str(archive_name),
                repository_root=root,
            )
            self.assertEqual(code, 1)
            self.assertEqual(parse_single_json(stdout)["error"]["code"], "restore_refused")
            self.assertEqual(list(empty.iterdir()), [])

    def test_restore_refuses_live_config_and_nonempty_scratch_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            marker = config_root / "keep-me.txt"
            marker.write_text("live\n", encoding="utf-8")
            write_env(root, config_root=config_root, backup_dest=backup_dest)
            code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)
            self.assertEqual(code, 0, stderr)
            archive_name = parse_single_json(stdout)["archive"]

            code, stdout, stderr = run_main(
                "--json", "backup", "restore", "--to", str(config_root), "--archive", str(archive_name),
                repository_root=root,
            )
            self.assertEqual(code, 1)
            self.assertEqual(parse_single_json(stdout)["error"]["code"], "restore_refused")
            self.assertIn("CONFIG_ROOT", parse_single_json(stdout)["error"]["message"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "live\n")
            self.assertEqual({path.name for path in config_root.iterdir()}, {"radarr", "keep-me.txt"})

            scratch = root / "scratch"
            scratch.mkdir()
            existing = scratch / "already.txt"
            existing.write_text("stay\n", encoding="utf-8")
            code, stdout, stderr = run_main(
                "--json", "backup", "restore", "--to", str(scratch), "--archive", str(archive_name),
                repository_root=root,
            )
            self.assertEqual(code, 1)
            self.assertEqual(parse_single_json(stdout)["error"]["code"], "restore_refused")
            self.assertEqual(list(scratch.iterdir()), [existing])
            self.assertEqual(existing.read_text(encoding="utf-8"), "stay\n")

    def test_restore_refuses_unsafe_names_and_members_before_extract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            scratch = root / "scratch"
            config_root.mkdir()
            backup_dest.mkdir()
            write_env(root, config_root=config_root, backup_dest=backup_dest)
            good_db = root / "good.db"
            create_wal_database(good_db, (1,))

            def publish(name: str, builder) -> None:
                builder(backup_dest / name)

            def regular_with(member_name: str, *, extra=None):
                def build(path: Path) -> None:
                    with tarfile.open(path, "w:gz") as archive:
                        archive.add(good_db, arcname="radarr.db")
                        if extra is None:
                            info = tarfile.TarInfo(member_name)
                            info.size = 4
                            archive.addfile(info, io.BytesIO(b"evil"))
                        else:
                            extra(archive)
                return build

            for name, builder in (
                ("homeflix-config-20200101T000000Z.tar.gz", regular_with("../outside.txt")),
                ("homeflix-config-20200102T000000Z.tar.gz", regular_with("/etc/passwd")),
                ("homeflix-config-20200103T000000Z.tar.gz", regular_with("link", extra=_add_symlink)),
                ("homeflix-config-20200104T000000Z.tar.gz", regular_with("fifo", extra=_add_fifo)),
            ):
                publish(name, builder)

            for archive_name, needle in (
                ("homeflix-config-../x.tar.gz", "archive name"),
                ("notes.txt", "archive name"),
                ("homeflix-config-20200101T000000Z.tar.gz", "safe|outside|member"),
                ("homeflix-config-20200102T000000Z.tar.gz", "safe|member"),
                ("homeflix-config-20200103T000000Z.tar.gz", "link"),
                ("homeflix-config-20200104T000000Z.tar.gz", "special"),
            ):
                with self.subTest(archive=archive_name):
                    if scratch.exists():
                        shutil.rmtree(scratch)
                    scratch.mkdir()
                    sentinel = root / "outside-sentinel"
                    sentinel.write_text("safe\n", encoding="utf-8")
                    code, stdout, stderr = run_main(
                        "--json", "backup", "restore", "--to", str(scratch), "--archive", archive_name,
                        repository_root=root,
                    )
                    self.assertEqual(code, 1)
                    payload = parse_single_json(stdout)
                    self.assertEqual(payload["error"]["code"], "restore_refused")
                    self.assertRegex(payload["error"]["message"], needle, payload["error"]["message"])
                    self.assertEqual(list(scratch.iterdir()), [])
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe\n")
                    self.assertFalse((root / "outside.txt").exists())

    def test_restore_fails_integrity_or_missing_sqlite_after_safe_extract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            config_root.mkdir()
            backup_dest.mkdir()
            write_env(root, config_root=config_root, backup_dest=backup_dest)
            corrupt = backup_dest / "homeflix-config-20200101T000000Z.tar.gz"
            with tarfile.open(corrupt, "w:gz") as archive:
                info = tarfile.TarInfo("radarr/radarr.db")
                payload = b"this is not sqlite"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            empty = backup_dest / "homeflix-config-20200102T000000Z.tar.gz"
            with tarfile.open(empty, "w:gz") as archive:
                info = tarfile.TarInfo("jellyfin/config.xml")
                payload = b"<ok/>\n"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            scratch = root / "scratch"
            code, stdout, stderr = run_main(
                "--json", "backup", "restore", "--to", str(scratch), "--archive", corrupt.name,
                repository_root=root,
            )
            self.assertEqual(code, 1)
            self.assertEqual(parse_single_json(stdout)["error"]["code"], "restore_refused")
            self.assertRegex(parse_single_json(stdout)["error"]["message"], r"integrity")

            other = root / "empty"
            code, stdout, stderr = run_main(
                "--json", "backup", "restore", "--to", str(other), "--archive", empty.name,
                repository_root=root,
            )
            self.assertEqual(code, 1)
            self.assertEqual(parse_single_json(stdout)["error"]["code"], "restore_refused")
            self.assertRegex(parse_single_json(stdout)["error"]["message"], r"at least one")


class BackupCleanupTests(unittest.TestCase):
    def test_failure_injection_cleans_temps_without_removing_foreign_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tmpdir = root / "tmp"
            tmpdir.mkdir()
            foreign_tmp = tmpdir / "preexisting.txt"
            foreign_tmp.write_text("keep-tmp\n", encoding="utf-8")
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            foreign_dest = backup_dest / "notes.txt"
            foreign_dest.write_text("keep-dest\n", encoding="utf-8")
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            write_env(root, config_root=config_root, backup_dest=backup_dest)
            before_tmp = {path.name: path.read_bytes() for path in tmpdir.iterdir()}
            before_dest = {path.name: path.read_bytes() for path in backup_dest.iterdir()}

            with patch.dict(os.environ, {"TMPDIR": str(tmpdir)}), patch(
                "scripts.homeflix_setup.backup.snapshot_sqlite",
                side_effect=sqlite3.Error("injected snapshot failure"),
            ):
                code, stdout, stderr = run_main("--json", "backup", "create", repository_root=root)

            self.assertEqual(code, 1)
            self.assertEqual(parse_single_json(stdout)["error"]["code"], "backup_refused")
            self.assertEqual({path.name: path.read_bytes() for path in tmpdir.iterdir()}, before_tmp)
            self.assertEqual({path.name: path.read_bytes() for path in backup_dest.iterdir()}, before_dest)
            self.assertFalse(any(path.name.startswith("homeflix-backup") for path in tmpdir.iterdir()))
            self.assertEqual(foreign_tmp.read_text(encoding="utf-8"), "keep-tmp\n")
            self.assertEqual(foreign_dest.read_text(encoding="utf-8"), "keep-dest\n")


def _install_compatibility_tree(root: Path) -> tuple[Path, Path]:
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copy2(REPOSITORY_ROOT / "scripts" / "backup-config.sh", scripts / "backup-config.sh")
    shutil.copy2(REPOSITORY_ROOT / "scripts" / "restore-config.sh", scripts / "restore-config.sh")
    launcher = scripts / "homeflix"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"sys.path.insert(0, {str(REPOSITORY_ROOT / 'scripts')!r})\n"
        "from homeflix_setup.cli import main\n"
        "raise SystemExit(main(repository_root=Path(__file__).resolve().parents[1]))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return scripts / "backup-config.sh", scripts / "restore-config.sh"


class CompatibilityScriptTests(unittest.TestCase):
    def test_backup_and_restore_scripts_preserve_help_flags_and_delegate_local_paths(self) -> None:
        backup_script = REPOSITORY_ROOT / "scripts" / "backup-config.sh"
        restore_script = REPOSITORY_ROOT / "scripts" / "restore-config.sh"
        backup_help = subprocess.run([str(backup_script), "-h"], check=False, capture_output=True, text=True)
        restore_help = subprocess.run([str(restore_script), "--help"], check=False, capture_output=True, text=True)
        self.assertEqual(backup_help.returncode, 0, backup_help.stderr)
        self.assertEqual(restore_help.returncode, 0, restore_help.stderr)
        self.assertIn("--install-cron", backup_help.stdout)
        self.assertIn("-h", backup_help.stdout)
        self.assertIn("--list", restore_help.stdout)
        self.assertIn("--to", restore_help.stdout)
        self.assertIn("--archive", restore_help.stdout)
        backup_source = backup_script.read_text(encoding="utf-8")
        restore_source = restore_script.read_text(encoding="utf-8")
        self.assertIn("backup create", backup_source)
        self.assertIn("backup list", restore_source)
        self.assertIn("backup restore", restore_source)
        self.assertIn("--install-cron", backup_source)
        self.assertNotIn('sqlite3 "$live"', backup_source)
        self.assertNotRegex(backup_source, r'sqlite3\s+"\$live"')
        self.assertNotIn("if sqlite3", backup_source)
        self.assertNotIn("tar -C", restore_source)
        self.assertNotIn("rsync", restore_source)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            backup_dest = root / "offbox"
            backup_dest.mkdir()
            create_wal_database(config_root / "radarr" / "radarr.db", (9, 10))
            (root / ".env").write_text(
                f"CONFIG_ROOT={config_root}\nBACKUP_DEST={backup_dest}\nBACKUP_KEEP=7\n",
                encoding="utf-8",
            )
            (root / ".env").chmod(0o600)
            backup_cmd, restore_cmd = _install_compatibility_tree(root)
            created = subprocess.run([str(backup_cmd)], check=False, capture_output=True, text=True, cwd=root)
            self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
            self.assertRegex(created.stdout, r"^OK archive=homeflix-config-\d{8}T\d{6}Z\.tar\.gz sqlite=1 keep=7 dest=set\n$")
            listed = subprocess.run([str(restore_cmd), "--list"], check=False, capture_output=True, text=True, cwd=root)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            archive_name = listed.stdout.strip().splitlines()[0]
            self.assertRegex(archive_name, r"^homeflix-config-\d{8}T\d{6}Z\.tar\.gz$")
            scratch = root / "scratch"
            restored = subprocess.run(
                [str(restore_cmd), "--to", str(scratch), "--archive", archive_name],
                check=False,
                capture_output=True,
                text=True,
                cwd=root,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr + restored.stdout)
            self.assertIn(f"OK archive={archive_name} sqlite_ok=1 sqlite_fail=0 dest={scratch}", restored.stdout)
            self.assertEqual(sqlite_values(scratch / "radarr" / "radarr.db"), [9, 10])

    def test_compatibility_scripts_do_not_mkdir_remote_dest_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            create_wal_database(config_root / "radarr" / "radarr.db", (1,))
            (root / ".env").write_text(
                f"CONFIG_ROOT={config_root}\nBACKUP_DEST=user@host:/srv/backups\n",
                encoding="utf-8",
            )
            backup_cmd, restore_cmd = _install_compatibility_tree(root)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("ssh", "scp"):
                stub = bin_dir / name
                stub.write_text("#!/bin/bash\nexit 255\n", encoding="utf-8")
                stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
            created = subprocess.run(
                [str(backup_cmd)], check=False, capture_output=True, text=True, cwd=root, env=env,
            )
            self.assertNotEqual(created.returncode, 0)
            self.assertFalse((root / "user@host:/srv/backups").exists())
            self.assertNotIn("user@host:/srv/backups", created.stdout)
            self.assertNotIn("user@host:/srv/backups", created.stderr)
            listed = subprocess.run(
                [str(restore_cmd), "--list"], check=False, capture_output=True, text=True, cwd=root, env=env,
            )
            self.assertFalse((root / "user@host:/srv/backups").exists())
            self.assertNotIn("user@host:/srv/backups", listed.stdout)

    def test_install_cron_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(REPOSITORY_ROOT / "scripts" / "backup-config.sh", scripts / "backup-config.sh")
            (root / ".env").write_text("CONFIG_ROOT=/tmp/config\nBACKUP_DEST=/tmp/offbox\n", encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            cron_file = root / "crontab.txt"
            cron_file.write_text("", encoding="utf-8")
            crontab = bin_dir / "crontab"
            crontab.write_text(
                "#!/bin/bash\n"
                f"file={str(cron_file)!r}\n"
                'if [[ "${1:-}" == "-l" ]]; then\n'
                '  if [[ -s "$file" ]]; then cat "$file"; else exit 0; fi\n'
                "  exit 0\n"
                "fi\n"
                'cat > "$file"\n',
                encoding="utf-8",
            )
            crontab.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
            first = subprocess.run(
                [str(scripts / "backup-config.sh"), "--install-cron"],
                check=False,
                capture_output=True,
                text=True,
                cwd=root,
                env=env,
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            expected = f"15 3 * * * /bin/bash {root}/scripts/backup-config.sh"
            self.assertIn("installed daily 03:15", first.stdout)
            self.assertEqual(cron_file.read_text(encoding="utf-8").strip(), expected)
            second = subprocess.run(
                [str(scripts / "backup-config.sh"), "--install-cron"],
                check=False,
                capture_output=True,
                text=True,
                cwd=root,
                env=env,
            )
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            self.assertIn("cron already installed", second.stdout)
            self.assertEqual(cron_file.read_text(encoding="utf-8").count(expected), 1)


VALID_SSH_DEST = "user@host:/var/backups/homeflix"

INVALID_SSH_DESTS = (
    "",
    "   ",
    "@host:/var/backups/homeflix",
    "user@:/var/backups/homeflix",
    "host:/var/backups/homeflix",
    "user@host:",
    "user@host",
    "user@host:rel/path",
    "user@host:./rel",
    "user@host:../etc",
    "user@host:/foo/../bar",
    "user@host:~/backups",
    "user@host:/var/backups/homeflix\n",
    "user@host:/var/backups/homeflix\x00hidden",
    "user@host:/var/backups/\thomeflix",
    "-oProxyCommand=evil",
    "-luser",
    "--help",
    "scp://user@host/var/backups/homeflix",
    "rsync://user@host/var/backups/homeflix",
    r"C:\backups",
    "C:/backups",
    "user@host:C:\\backups",
    "user@2001:db8::1:/var/backups/homeflix",
    "user@::1:/var/backups/homeflix",
    "user@[::1]:/var/backups/homeflix",
    "user@host:22:/var/backups/homeflix",
    "user@host:/var/backups/homeflix:extra",
    "user@host:/var/backups/homeflix;rm",
    "user@host:/var/backups/$(id)",
    "user@host:/var/backups/`id`",
    "user@host:/var/backups/homeflix|tee",
    "user@host:/var/backups/homeflix&id",
    "user@host:/var/backups/homeflix and more",
    "user@host:/var/backups/homeflix#frag",
)


class FakeSshRunner:
    """CommandRunner stand-in that stores archives in a local directory."""

    def __init__(
        self,
        store: Path,
        *,
        user: str = "user",
        host: str = "host",
        remote_path: str = "/var/backups/homeflix",
    ) -> None:
        self.store = store
        self.user = user
        self.host = host
        self.remote_path = remote_path
        self.calls: list[dict[str, object]] = []
        self.fail_at: str | None = None
        self.timeout_at: str | None = None

    def run(
        self,
        argv,
        *,
        input_text=None,
        check=False,
        redact=(),
        timeout=None,
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append({"argv": command, "timeout": timeout, "redact": tuple(redact)})
        kind = self.operation(command)
        if self.timeout_at == kind:
            raise subprocess.TimeoutExpired(command, timeout)
        if self.fail_at == kind:
            return subprocess.CompletedProcess(command, 1, "", "remote failed")
        if command[:1] == ["scp"]:
            return self._scp(command)
        if command[:1] == ["ssh"]:
            return self._ssh(command)
        return subprocess.CompletedProcess(command, 127, "", "unknown command")

    def operation(self, argv: list[str]) -> str:
        if argv[:1] == ["scp"]:
            prefix = f"{self.user}@{self.host}:"
            if any(argument.startswith(prefix) for argument in argv[1:-1]):
                return "get"
            return "put"
        if "ls" in argv:
            return "list"
        if "rm" in argv:
            return "prune"
        return "unknown"

    def operations(self) -> list[str]:
        return [self.operation(call["argv"]) for call in self.calls]  # type: ignore[arg-type]

    def _scp(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:3] != ["scp", "-oBatchMode=yes", "--"] or len(argv) != 5:
            return subprocess.CompletedProcess(argv, 2, "", "bad scp argv")
        source, destination = argv[3], argv[4]
        prefix = f"{self.user}@{self.host}:"
        if destination.startswith(prefix):
            name = Path(destination[len(prefix) :]).name
            target = self.store / name
            target.write_bytes(Path(source).read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")
        if source.startswith(prefix):
            name = Path(source[len(prefix) :]).name
            remote = self.store / name
            if not remote.is_file():
                return subprocess.CompletedProcess(argv, 1, "", "missing")
            Path(destination).write_bytes(remote.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", "unmapped scp dest")

    def _ssh(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        expected_prefix = ["ssh", "-oBatchMode=yes", "--", f"{self.user}@{self.host}"]
        if argv[:4] != expected_prefix:
            return subprocess.CompletedProcess(argv, 2, "", "bad ssh argv")
        remote = argv[4:]
        if remote[:3] == ["ls", "-1t", "--"] and remote[3:] == [self.remote_path]:
            if not self.store.is_dir():
                return subprocess.CompletedProcess(argv, 2, "", "missing")
            entries = sorted(self.store.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)
            names = "\n".join(path.name for path in entries if path.is_file())
            return subprocess.CompletedProcess(argv, 0, names + ("\n" if names else ""), "")
        if remote[:3] == ["rm", "-f", "--"] and len(remote) == 4:
            remote_file = remote[3]
            prefix = self.remote_path.rstrip("/") + "/"
            if not remote_file.startswith(prefix):
                return subprocess.CompletedProcess(argv, 2, "", "bad rm path")
            (self.store / remote_file[len(prefix) :]).unlink(missing_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", "bad ssh remote")


def _write_archive_file(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


class ArtifactRepositoryContractMixin:
    def open_repo(self) -> LocalArtifactRepository | SshArtifactRepository:
        raise NotImplementedError

    def test_list_get_put_prune_keep_matching_archives_and_foreign_files(self) -> None:
        repo = self.open_repo()
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            older = _write_archive_file(staging / "homeflix-config-20200101T000000Z.tar.gz", b"old")
            newer = _write_archive_file(staging / "homeflix-config-20200202T000000Z.tar.gz", b"new")
            repo.put(older)
            repo.put(newer)
            listed = repo.list_archives()
            self.assertEqual(listed, [newer.name, older.name])
            fetched = staging / "fetched.tar.gz"
            repo.get(newer.name, fetched)
            self.assertEqual(fetched.read_bytes(), b"new")
            foreign_name = "notes.txt"
            if isinstance(repo, LocalArtifactRepository):
                (repo.root / foreign_name).write_text("foreign\n", encoding="utf-8")
            else:
                assert isinstance(self, SshArtifactRepositoryContractTests)
                (self.store / foreign_name).write_text("foreign\n", encoding="utf-8")
            repo.prune(1)
            self.assertEqual(repo.list_archives(), [newer.name])
            if isinstance(repo, LocalArtifactRepository):
                self.assertTrue((repo.root / foreign_name).is_file())
                self.assertFalse((repo.root / older.name).exists())
                self.assertTrue((repo.root / newer.name).is_file())
            else:
                self.assertTrue((self.store / foreign_name).is_file())
                self.assertFalse((self.store / older.name).exists())
                self.assertTrue((self.store / newer.name).is_file())


class LocalArtifactRepositoryContractTests(ArtifactRepositoryContractMixin, unittest.TestCase):
    def open_repo(self) -> LocalArtifactRepository:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        dest = self.root / "offbox"
        dest.mkdir()
        return LocalArtifactRepository(dest)


class SshArtifactRepositoryContractTests(ArtifactRepositoryContractMixin, unittest.TestCase):
    def open_repo(self) -> SshArtifactRepository:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = self.root / "remote"
        self.store.mkdir()
        self.runner = FakeSshRunner(self.store)
        return SshArtifactRepository(parse_ssh_destination(VALID_SSH_DEST), self.runner)


class SshRepositoryDispatchTests(unittest.TestCase):
    def test_open_repository_dispatches_ssh_and_local_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_dest = root / "offbox"
            local_dest.mkdir()
            runner = FakeSshRunner(root / "unused")
            ssh_repo = open_repository(VALID_SSH_DEST, data_root=None, runner=runner)
            local_repo = open_repository(str(local_dest), data_root=None, runner=runner)
            self.assertIsInstance(ssh_repo, SshArtifactRepository)
            self.assertIsInstance(local_repo, LocalArtifactRepository)
            self.assertEqual(runner.calls, [])

    def test_invalid_ssh_dest_is_rejected_before_any_command(self) -> None:
        runner = FakeSshRunner(Path("/tmp"))
        for dest in ("-oProxyCommand=evil", "user@host:/foo/../bar", "scp://user@host/path"):
            with self.subTest(dest=dest):
                with self.assertRaises(BackupError):
                    open_repository(dest, data_root=None, runner=runner)
        self.assertEqual(runner.calls, [])


class SshRepositoryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / "remote"
        self.store.mkdir()
        self.runner = FakeSshRunner(self.store)
        self.repo = SshArtifactRepository(parse_ssh_destination(VALID_SSH_DEST), self.runner)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_put_get_list_prune_use_exact_argv_batchmode_and_timeout(self) -> None:
        source = self.root / "homeflix-config-20200101T000000Z.tar.gz"
        source.write_bytes(b"payload")
        self.repo.put(source)
        self.repo.list_archives()
        fetched = self.root / "out.tar.gz"
        self.repo.get(source.name, fetched)
        extra = self.store / "homeflix-config-20190101T000000Z.tar.gz"
        extra.write_bytes(b"old")
        os.utime(extra, (1, 1))
        os.utime(self.store / source.name, (2, 2))
        self.repo.prune(1)

        self.assertEqual(fetched.read_bytes(), b"payload")
        expected_put = [
            "scp",
            "-oBatchMode=yes",
            "--",
            str(source),
            f"{VALID_SSH_DEST}/{source.name}",
        ]
        expected_list = [
            "ssh",
            "-oBatchMode=yes",
            "--",
            "user@host",
            "ls",
            "-1t",
            "--",
            "/var/backups/homeflix",
        ]
        expected_get = [
            "scp",
            "-oBatchMode=yes",
            "--",
            f"{VALID_SSH_DEST}/{source.name}",
            str(fetched),
        ]
        expected_rm = [
            "ssh",
            "-oBatchMode=yes",
            "--",
            "user@host",
            "rm",
            "-f",
            "--",
            f"/var/backups/homeflix/{extra.name}",
        ]
        recorded = [call["argv"] for call in self.runner.calls]
        self.assertEqual(recorded[0], expected_put)
        self.assertEqual(recorded[1], expected_list)
        self.assertEqual(recorded[2], expected_get)
        self.assertIn(expected_rm, recorded)
        for call in self.runner.calls:
            self.assertIsInstance(call["argv"], list)
            self.assertTrue(all(isinstance(part, str) for part in call["argv"]))  # type: ignore[union-attr]
            self.assertIsInstance(call["timeout"], (int, float))
            self.assertGreater(call["timeout"], 0)
            redact = call["redact"]
            self.assertIn("user", redact)
            self.assertIn("host", redact)
            self.assertIn("/var/backups/homeflix", redact)

    def test_put_failure_and_timeout_do_not_issue_prune(self) -> None:
        existing = self.store / "homeflix-config-20200101T000000Z.tar.gz"
        existing.write_bytes(b"keep")
        foreign = self.store / "notes.txt"
        foreign.write_text("foreign\n", encoding="utf-8")
        source = self.root / "homeflix-config-20200202T000000Z.tar.gz"
        source.write_bytes(b"new")
        before = {path.name: path.read_bytes() for path in self.store.iterdir()}

        self.runner.fail_at = "put"
        with self.assertRaises(BackupError):
            self.repo.put(source)
        self.assertNotIn("prune", self.runner.operations())
        self.assertEqual({path.name: path.read_bytes() for path in self.store.iterdir()}, before)

        self.runner.fail_at = None
        self.runner.timeout_at = "put"
        self.runner.calls.clear()
        with self.assertRaises(BackupError):
            self.repo.put(source)
        self.assertNotIn("prune", self.runner.operations())
        self.assertEqual({path.name: path.read_bytes() for path in self.store.iterdir()}, before)

    def test_partial_failures_leave_existing_archives_and_foreign_files(self) -> None:
        older = self.store / "homeflix-config-20200101T000000Z.tar.gz"
        newer = self.store / "homeflix-config-20200202T000000Z.tar.gz"
        older.write_bytes(b"old")
        newer.write_bytes(b"new")
        foreign = self.store / "notes.txt"
        foreign.write_text("foreign\n", encoding="utf-8")
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))

        self.runner.fail_at = "get"
        with self.assertRaises(BackupError):
            self.repo.get(newer.name, self.root / "out.tar.gz")
        self.assertEqual(self.runner.operations(), ["get"])
        self.assertEqual(older.read_bytes(), b"old")
        self.assertEqual(newer.read_bytes(), b"new")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign\n")
        self.assertFalse((self.root / "out.tar.gz").exists())

        self.runner.fail_at = "prune"
        self.runner.calls.clear()
        source = self.root / "homeflix-config-20200303T000000Z.tar.gz"
        source.write_bytes(b"newest")
        self.repo.put(source)
        with self.assertRaises(BackupError):
            self.repo.prune(1)
        self.assertEqual(self.runner.operations()[0], "put")
        self.assertTrue((self.store / source.name).is_file())
        self.assertTrue(older.is_file())
        self.assertTrue(newer.is_file())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign\n")


class SshDestinationParserTests(unittest.TestCase):
    def test_documented_form_is_accepted_and_classified_remote(self) -> None:
        parsed = parse_ssh_destination(VALID_SSH_DEST)
        self.assertEqual(parsed.user, "user")
        self.assertEqual(parsed.host, "host")
        self.assertEqual(parsed.path, "/var/backups/homeflix")
        self.assertTrue(dest_is_remote(VALID_SSH_DEST))
        self.assertFalse(dest_is_remote("/var/backups/homeflix"))

    def test_documented_form_rejects_unsupported_dests_before_any_run(self) -> None:
        for dest in INVALID_SSH_DESTS:
            with self.subTest(dest=dest):
                with self.assertRaises(BackupError):
                    parse_ssh_destination(dest)


def _add_symlink(archive: tarfile.TarFile) -> None:
    info = tarfile.TarInfo("escape")
    info.type = tarfile.SYMTYPE
    info.linkname = "../outside-sentinel"
    archive.addfile(info)


def _add_fifo(archive: tarfile.TarFile) -> None:
    info = tarfile.TarInfo("pipe")
    info.type = tarfile.FIFOTYPE
    archive.addfile(info)


if __name__ == "__main__":
    unittest.main()
