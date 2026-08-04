from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.homeflix_setup.compose import build_override, configure
from scripts.homeflix_setup.discover import GraphicsFact, HostFacts, MountFact
from scripts.homeflix_setup.envfile import EnvDocument


def facts(*, mount: str = "/", quicksync: bool = False) -> HostFacts:
    return HostFacts(
        os_id="debian", os_version_id="12", os_pretty_name="Debian", supported=True,
        uid=1234, gid=2345, timezone="Europe/Lisbon", memory_bytes=1, architecture="x86_64",
        cpu_model="fixture", graphics=GraphicsFact(("/dev/dri/renderD128",) if quicksync else ()),
        listening_ports=(), listening_ports_status="ok", listening_ports_reason=None,
        mounts=(MountFact(mount, "/dev/fixture", "ext4", 1000),), mounts_status="ok", mounts_reason=None,
        docker_present=True, docker_cli_status="ok", docker_cli_reason=None,
        compose_present=True, compose_status="ok", compose_reason=None,
        docker_daemon_reachable=True, docker_daemon_status="ok", docker_daemon_reason=None,
        host_nameservers=("192.0.2.1",), host_search_domains=(), host_dns_status="ok",
        host_dns_reason=None, ssh_context=False,
    )


class ComposeOverrideTests(unittest.TestCase):
    def test_quicksync_and_direct_ports_are_bounded_and_deterministic(self) -> None:
        first = build_override(facts(quicksync=True), True)
        second = build_override(facts(quicksync=True), True)
        self.assertEqual(first, second)
        self.assertIn("/dev/dri:/dev/dri", first)
        for port in (5055, 7878, 8989):
            self.assertIn(f'"{port}:{port}"', first)
        self.assertNotIn("password", first.casefold())

    def test_no_adaptations_is_valid_minimal_yaml(self) -> None:
        self.assertEqual(build_override(facts(), False), "services: {}\n")

    def test_unresolved_host_dns_enables_direct_setup_ports(self) -> None:
        unresolved = replace(facts(), host_dns_status="error", host_dns_reason="probe failed")
        override = build_override(unresolved, False)
        for port in (5055, 7878, 8989):
            self.assertIn(f'"{port}:{port}"', override)

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
            self.assertNotIn("fixture-secret", repr(result))
            self.assertNotIn("fixture-secret", repr(rerun))

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
