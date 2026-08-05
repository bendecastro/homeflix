from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.preflight import run_preflight
from tests.helpers import parse_single_json


class MountRunner:
    def __init__(
        self,
        target: Path,
        filesystem: str = "ext4",
        mount_available: bool = True,
        compose_result: int = 0,
        compose_exception: Exception | None = None,
    ) -> None:
        self.target = target
        self.filesystem = filesystem
        self.mount_available = mount_available
        self.compose_result = compose_result
        self.compose_exception = compose_exception
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, argv, **kwargs):
        command = tuple(argv)
        self.calls.append((command, kwargs))
        if command[:2] == ("docker", "compose") and "config" in command:
            if self.compose_exception is not None:
                raise self.compose_exception
            return subprocess.CompletedProcess(argv, self.compose_result, "sensitive output", "sensitive error")
        if argv[0] == "findmnt" and not self.mount_available:
            return subprocess.CompletedProcess(argv, 1, "", "not mounted")
        payload = {"filesystems": [{"target": str(self.target), "source": "/dev/fixture", "fstype": self.filesystem}]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def configured(root: Path) -> dict[str, object]:
    data = root / "data"
    for path in (data / "torrents", data / "media", root / "config", root / "cache"):
        path.mkdir(parents=True)
    env_file = root / ".env"
    env_file.touch()
    return {
        "_ENV_FILE": str(env_file),
        "DATA_ROOT": str(data),
        "CONFIG_ROOT": str(root / "config"),
        "CACHE_ROOT": str(root / "cache"),
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
        "VPN_USER": "",
        "VPN_PASSWORD": "",
    }


class PreflightTests(unittest.TestCase):
    def test_vpn_credentials_warn_for_core_and_fail_for_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            runner = MountRunner(Path(config["DATA_ROOT"]))
            core = run_preflight(config, "core", runner)
            acquisition = run_preflight(config, "acquisition", runner)
        self.assertTrue(core.passed)
        self.assertEqual(core.counts["warn"], 2)
        self.assertFalse(acquisition.passed)
        self.assertEqual(acquisition.counts["fail"], 2)
        self.assertNotIn("VPN_USER\":", repr(core.results))

    def test_compose_config_uses_exact_project_and_env_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = configured(root)
            runner = MountRunner(Path(config["DATA_ROOT"]))
            report = run_preflight(config, "core", runner)
            compose_call = next(call for call in runner.calls if "config" in call[0])
            self.assertEqual(
                compose_call[0],
                (
                    "docker", "compose", "--project-directory", str(root),
                    "--env-file", str(root / ".env"), "config", "--quiet",
                ),
            )
            self.assertEqual(compose_call[1]["timeout"], 30)
            self.assertEqual(next(r for r in report.results if r.name == "compose_config").status, "pass")

            for runner in (
                MountRunner(Path(config["DATA_ROOT"]), compose_result=1),
                MountRunner(
                    Path(config["DATA_ROOT"]),
                    compose_exception=subprocess.TimeoutExpired(("docker", "compose"), 30),
                ),
                MountRunner(Path(config["DATA_ROOT"]), compose_exception=FileNotFoundError()),
            ):
                with self.subTest(exception=runner.compose_exception, returncode=runner.compose_result):
                    failed = run_preflight(config, "core", runner)
                    result = next(r for r in failed.results if r.name == "compose_config")
                    self.assertEqual(result.status, "fail")
                    self.assertFalse(failed.passed)
                    self.assertNotIn("sensitive", result.message)

    def test_hardlink_probe_verifies_inode_and_cleans_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            data = Path(config["DATA_ROOT"])
            report = run_preflight(config, "core", MountRunner(data))
            hardlink = next(result for result in report.results if result.name == "hardlink")
            self.assertEqual(hardlink.status, "pass")
            self.assertIn("inode", hardlink.message)
            self.assertEqual(list((data / "torrents").glob(".homeflix-preflight-*")), [])
            self.assertEqual(list((data / "media").glob(".homeflix-preflight-*")), [])

    def test_remaining_probe_paths_make_successful_hardlink_cleanup_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            data = Path(config["DATA_ROOT"])
            unlink_attempts: list[Path] = []
            original_unlink = Path.unlink

            def leave_probe_files(path: Path, *args, **kwargs):
                if path.name.startswith(".homeflix-preflight-"):
                    unlink_attempts.append(path)
                    return None
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=leave_probe_files):
                report = run_preflight(config, "core", MountRunner(data))

            self.assertEqual(next(r for r in report.results if r.name == "hardlink").status, "pass")
            self.assertEqual(next(r for r in report.results if r.name == "hardlink_cleanup").status, "fail")
            self.assertFalse(report.passed)
            self.assertEqual(len(unlink_attempts), 2)

    def test_hardlink_cleanup_failure_is_blocking_and_attempts_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            data = Path(config["DATA_ROOT"])
            unlink_attempts: list[Path] = []
            original_unlink = Path.unlink

            def fail_probe_cleanup(path: Path, *args, **kwargs):
                if path.name.startswith(".homeflix-preflight-"):
                    unlink_attempts.append(path)
                    raise OSError("forced cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_probe_cleanup), mock.patch(
                "scripts.homeflix_setup.preflight.os.link", side_effect=OSError("forced link failure")
            ):
                report = run_preflight(config, "core", MountRunner(data))

            hardlink_results = [r for r in report.results if r.name == "hardlink"]
            cleanup_results = [r for r in report.results if r.name == "hardlink_cleanup"]
            self.assertEqual([result.status for result in hardlink_results], ["fail"])
            self.assertEqual([result.status for result in cleanup_results], ["fail"])
            self.assertFalse(report.passed)
            self.assertEqual(len(unlink_attempts), 2)
            self.assertEqual({path.parent for path in unlink_attempts}, {data / "torrents", data / "media"})

    def test_identity_mismatch_and_bool_types_fail(self) -> None:
        for key, wrong in (("PUID", os.getuid() + 1), ("PGID", os.getgid() + 1)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                config = configured(Path(directory))
                data = Path(config["DATA_ROOT"])
                config[key] = wrong
                mismatch = run_preflight(config, "core", MountRunner(data))
                self.assertEqual(next(r for r in mismatch.results if r.name == "data_ownership").status, "fail")
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            data = Path(config["DATA_ROOT"])
            config["PUID"] = True
            invalid = run_preflight(config, "core", MountRunner(data))
        self.assertEqual(next(r for r in invalid.results if r.name == "puid").status, "fail")

    def test_unsupported_filesystem_root_fallback_and_absent_mount_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            data = Path(config["DATA_ROOT"])
            unsupported = run_preflight(config, "core", MountRunner(data, "exfat"))
            fallback = run_preflight(config, "core", MountRunner(Path("/"), "ext4"))
            absent = run_preflight(config, "core", MountRunner(data, mount_available=False))
        self.assertEqual(next(r for r in unsupported.results if r.name == "data_filesystem").status, "fail")
        self.assertEqual(next(r for r in fallback.results if r.name == "data_mount").status, "fail")
        self.assertEqual(next(r for r in absent.results if r.name == "data_mount").status, "fail")

    def test_absent_paths_fail_without_being_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            config = {
                "DATA_ROOT": str(missing), "CONFIG_ROOT": str(root / "config"),
                "CACHE_ROOT": str(root / "cache"), "PUID": "1000", "PGID": "1000",
                "VPN_USER": "", "VPN_PASSWORD": "",
            }
            report = run_preflight(config, "core", MountRunner(missing))
            self.assertFalse(missing.exists())
        self.assertGreaterEqual(report.counts["fail"], 3)

    def test_json_cli_emits_one_object_with_counts_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = configured(root)
            (root / ".env").write_text(
                "\n".join(f"{key}={value}" for key, value in config.items() if not key.startswith("_")) + "\n",
                encoding="utf-8",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("scripts.homeflix_setup.cli.CommandRunner", return_value=MountRunner(Path(config["DATA_ROOT"]))), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(("--json", "preflight", "--phase", "core"), repository_root=root)
            acquisition_stdout = io.StringIO()
            with patch("scripts.homeflix_setup.cli.CommandRunner", return_value=MountRunner(Path(config["DATA_ROOT"]))), redirect_stdout(acquisition_stdout), redirect_stderr(io.StringIO()):
                acquisition_code = main(("--json", "preflight", "--phase", "acquisition"), repository_root=root)
        result = parse_single_json(stdout.getvalue())
        acquisition = parse_single_json(acquisition_stdout.getvalue())
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(acquisition_code, 1)
        self.assertEqual(set(result["counts"]), {"pass", "warn", "fail"})
        self.assertEqual(result["counts"]["warn"], 2)
        self.assertEqual(result["counts"]["fail"], 0)
        self.assertEqual(acquisition["counts"]["fail"], 2)


if __name__ == "__main__":
    unittest.main()
