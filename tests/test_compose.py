from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from scripts.homeflix_setup.compose import _atomic_write, build_override, configure
from scripts.homeflix_setup.discover import GraphicsDeviceFact, GraphicsFact, HostFacts, MountFact
from scripts.homeflix_setup.envfile import EnvDocument


def facts(*, mount: str = "/", quicksync: bool = False) -> HostFacts:
    return HostFacts(
        os_id="debian", os_version_id="12", os_pretty_name="Debian", supported=True,
        uid=1234, gid=2345, timezone="Europe/Lisbon", memory_bytes=1, architecture="x86_64",
        cpu_model="fixture", graphics=GraphicsFact(
            ("/dev/dri/renderD128",) if quicksync else (),
            devices=(GraphicsDeviceFact("/dev/dri/renderD128", "0x8086", "ok", True, True) ,) if quicksync else (),
        ),
        listening_ports=(), listening_ports_status="ok", listening_ports_reason=None,
        mounts=(MountFact(mount, "/dev/fixture", "ext4", 1000),), mounts_status="ok", mounts_reason=None,
        docker_present=True, docker_cli_status="ok", docker_cli_reason=None,
        compose_present=True, compose_status="ok", compose_reason=None,
        docker_daemon_reachable=True, docker_daemon_status="ok", docker_daemon_reason=None,
        host_nameservers=("192.0.2.1",), host_search_domains=(), host_dns_status="ok",
        host_dns_reason=None, ssh_context=False,
        lan_dns_domain="local", lan_dns_status="resolved",
    )


class ComposeOverrideTests(unittest.TestCase):
    def assert_valid_override(self, override: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yml").write_text(
                "services:\n  jellyfin:\n    image: busybox\n  jellyseerr:\n    image: busybox\n  radarr:\n    image: busybox\n  sonarr:\n    image: busybox\n",
                encoding="utf-8",
            )
            (root / "override.yml").write_text(override, encoding="utf-8")
            result = subprocess.run(
                ["docker", "compose", "-f", str(root / "compose.yml"), "-f", str(root / "override.yml"), "config", "--quiet"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_quicksync_and_direct_ports_are_bounded_and_deterministic(self) -> None:
        first = build_override(facts(quicksync=True), True)
        second = build_override(facts(quicksync=True), True)
        self.assertEqual(first, second)
        self.assertIn("/dev/dri:/dev/dri", first)
        for port in (5055, 7878, 8989):
            self.assertIn(f'"{port}:{port}"', first)
        self.assertNotIn("password", first.casefold())
        self.assert_valid_override(first)

    def test_no_adaptations_is_valid_minimal_yaml(self) -> None:
        override = build_override(facts(), False)
        self.assertEqual(override, "services: {}\n")
        self.assert_valid_override(override)

    def test_non_intel_unknown_or_inaccessible_graphics_do_not_map_dri(self) -> None:
        cases = (
            GraphicsDeviceFact("/dev/dri/renderD128", "0x1002", "ok", True, True),
            GraphicsDeviceFact("/dev/dri/renderD128", None, "error", True, True),
            GraphicsDeviceFact("/dev/dri/renderD128", "0x8086", "ok", True, False),
        )
        for device in cases:
            with self.subTest(device=device):
                host = replace(facts(), graphics=GraphicsFact((device.path,), devices=(device,)))
                self.assertNotIn("/dev/dri", build_override(host, False))

    def test_unresolved_or_error_lan_dns_enables_direct_setup_ports(self) -> None:
        for status in ("unresolved", "error"):
            with self.subTest(status=status):
                override = build_override(replace(facts(), lan_dns_status=status), False)
                for port in (5055, 7878, 8989):
                    self.assertIn(f'"{port}:{port}"', override)
                self.assert_valid_override(override)

    def test_configure_derives_host_values_generates_secret_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = Path(__file__).resolve().parents[1] / ".env.example"
            (root / ".env.example").write_bytes(example.read_bytes())
            data = root / "storage" / "data"
            config = root / "config"
            cache = root / "cache"
            for path in (data, config, cache):
                path.mkdir(parents=True)
            host = facts(mount=str(root))
            with patch("scripts.homeflix_setup.secrets.secrets.token_urlsafe", return_value="fixture-secret") as token:
                result = configure(root, host, data_root=str(data), config_root=str(config), cache_root=str(cache), quality_profile="Balanced HD", direct_setup_ports=True)
                first_env = (root / ".env").read_bytes()
                first_override = (root / "docker-compose.override.yml").read_bytes()
                rerun = configure(root, host, data_root=str(data), config_root=str(config), cache_root=str(cache), quality_profile="Balanced HD", direct_setup_ports=True)
            document = EnvDocument.load(root / ".env")
            self.assertEqual(document.get("PUID"), "1234")
            self.assertEqual(document.get("PGID"), "2345")
            self.assertEqual(document.get("TZ"), "Europe/Lisbon")
            self.assertEqual(document.get("COMPOSE_PROJECT_NAME"), "homeflix")
            self.assertEqual(document.get("QUALITY_PROFILE"), "Balanced HD")
            self.assertEqual(token.call_count, 1)
            self.assertEqual((root / ".env").read_bytes(), first_env)
            self.assertEqual((root / "docker-compose.override.yml").read_bytes(), first_override)
            self.assertEqual((root / "docker-compose.override.yml").stat().st_mode & 0o777, 0o644)
            self.assertNotIn("fixture-secret", repr(result))
            self.assertNotIn("fixture-secret", repr(rerun))

    def test_override_atomic_setup_failure_closes_fd_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.yml"
            path.write_text("services: {}\n", encoding="utf-8")
            with mock.patch("scripts.homeflix_setup.compose.os.fchmod", side_effect=OSError("mode failure")), mock.patch(
                "scripts.homeflix_setup.compose.os.close", wraps=os.close
            ) as close:
                with self.assertRaisesRegex(OSError, "mode failure"):
                    _atomic_write(path, "services:\n", 0o644)
            close.assert_called_once()
            self.assertEqual(path.read_text(encoding="utf-8"), "services: {}\n")
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_configure_refuses_nonexistent_or_unverified_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("DATA_ROOT=\n", encoding="utf-8")
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(ValueError, "does not exist"):
                configure(root, facts(mount=str(root)), data_root=str(root / "missing"), config_root=str(existing), cache_root=str(existing))
            self.assertFalse((root / ".env").exists())
            outside_mount = root / "mount"
            outside_mount.mkdir()
            with self.assertRaisesRegex(ValueError, "not under"):
                configure(root, facts(mount=str(outside_mount)), data_root=str(existing), config_root=str(existing), cache_root=str(existing))
            self.assertFalse((root / ".env").exists())


if __name__ == "__main__":
    unittest.main()
