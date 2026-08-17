from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.cli import build_parser, main
from scripts.homeflix_setup.preflight import run_preflight
from scripts.homeflix_setup.secrets import set_usenet_secrets, set_vpn_secrets
from tests.test_preflight import MountRunner, configured


def run_main(*args: str, repository_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            return_code = main(args, repository_root=repository_root)
        except SystemExit as raised:
            return int(raised.code or 0), stdout.getvalue(), stderr.getvalue()
    return return_code, stdout.getvalue(), stderr.getvalue()


class VpnSecretHandoffTests(unittest.TestCase):
    def test_secrets_vpn_refuses_json_and_redirected_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "must-not-appear-as-argv-or-json"
            (root / ".env").write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\n",
                encoding="utf-8",
            )
            json_code, json_stdout, json_stderr = run_main(
                "--json", "secrets", "vpn", repository_root=root
            )
            pipe_code, pipe_stdout, pipe_stderr = run_main(
                "secrets", "vpn", repository_root=root
            )
            document = (root / ".env").read_text(encoding="utf-8")

        self.assertEqual(json_code, 2)
        self.assertEqual(pipe_code, 2)
        combined = json_stdout + json_stderr + pipe_stdout + pipe_stderr
        self.assertNotIn(secret, combined)
        self.assertNotIn("VPN_PASSWORD=", combined)
        self.assertNotIn(secret, document)

    def test_secrets_vpn_refuses_secret_tokens_on_argv(self) -> None:
        secret = "must-not-appear-as-argv-or-json"
        parser = build_parser()
        argv_shapes = (
            ("secrets", "vpn", secret),
            ("secrets", "vpn", "--password", secret),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\n",
                encoding="utf-8",
            )
            for argv in argv_shapes:
                with self.subTest(argv=argv):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        parser.parse_args(list(argv))
                    self.assertEqual(raised.exception.code, 2)

                    stdout = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        main(argv, repository_root=root)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertNotIn(secret, env_path.read_text(encoding="utf-8"))

            document = env_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, document)
        self.assertIn("VPN_PASSWORD=\n", document)

    def test_supported_openvpn_secrets_confirm_and_update_env_without_leaking_values(self) -> None:
        username = "proton-openvpn-user+pmp"
        password = "proton-openvpn-secret"
        prompts: list[tuple[str, bool]] = []

        def reader(prompt: str, *, confirm: bool = False) -> str:
            prompts.append((prompt, confirm))
            if "user" in prompt.casefold():
                return username
            if "password" in prompt.casefold():
                return password
            raise AssertionError(f"unexpected prompt {prompt!r}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\nOTHER=keep\n",
                encoding="utf-8",
            )
            result = set_vpn_secrets(path, reader=reader)
            document = EnvDocument.load(path)
            mode = path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)
        self.assertEqual(document.get("VPN_USER"), username)
        self.assertEqual(document.get("VPN_PASSWORD"), password)
        self.assertEqual(document.get("OTHER"), "keep")
        self.assertEqual([confirm for _prompt, confirm in prompts], [True, True])
        rendered = repr(result)
        self.assertNotIn(username, rendered)
        self.assertNotIn(password, rendered)
        names = [item["name"] for item in result["keys"]]
        self.assertEqual(names, ["VPN_USER", "VPN_PASSWORD"])
        self.assertTrue(all(item["status"] == "updated" and item["secret"] for item in result["keys"]))

    def test_supported_wireguard_secret_confirms_and_updates_env_without_leaking_values(self) -> None:
        from scripts.homeflix_setup.secrets import required_vpn_secret_keys

        key = "wireguard-private-key-fixture"
        prompts: list[tuple[str, bool]] = []

        def reader(prompt: str, *, confirm: bool = False) -> str:
            prompts.append((prompt, confirm))
            return key

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=wireguard\nVPN_WIREGUARD_PRIVATE_KEY=\nOTHER=keep\n",
                encoding="utf-8",
            )
            result = set_vpn_secrets(path, reader=reader)
            document = EnvDocument.load(path)

        self.assertEqual(required_vpn_secret_keys("protonvpn", "wireguard"), ("VPN_WIREGUARD_PRIVATE_KEY",))
        self.assertEqual(document.get("VPN_WIREGUARD_PRIVATE_KEY"), key)
        self.assertEqual(document.get("OTHER"), "keep")
        self.assertEqual([confirm for _prompt, confirm in prompts], [True])
        self.assertNotIn(key, repr(result))

    def test_unsupported_provider_refuses_guessed_keys_and_points_at_gluetun_docs(self) -> None:
        guessed = "guessed-nord-token"

        def reader(prompt: str, *, confirm: bool = False) -> str:
            raise AssertionError("unsupported providers must not prompt for guessed keys")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "VPN_SERVICE_PROVIDER=nordvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "gluetun-wiki"):
                set_vpn_secrets(path, reader=reader)
            with self.assertRaisesRegex(ValueError, "gluetun-wiki"):
                set_vpn_secrets(path, provider="custom", vpn_type="wireguard", reader=reader)
            document = path.read_text(encoding="utf-8")
        self.assertNotIn(guessed, document)
        self.assertIn("VPN_USER=\n", document)

    def test_read_from_tty_refuses_missing_or_non_tty_and_does_not_take_stdin(self) -> None:
        from scripts.homeflix_setup.secrets import read_from_tty

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent-tty"
            with self.assertRaisesRegex(RuntimeError, "controlling terminal"):
                read_from_tty("VPN password: ", tty_path=str(missing))
            regular = Path(directory) / "regular"
            regular.write_text("stdin-secret\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "controlling terminal"):
                read_from_tty("VPN password: ", tty_path=str(regular))
            self.assertEqual(regular.read_text(encoding="utf-8"), "stdin-secret\n")

    def test_confirmation_mismatch_does_not_write_secrets(self) -> None:
        from scripts.homeflix_setup.secrets import read_from_tty

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not match"):
                set_vpn_secrets(
                    path,
                    reader=lambda prompt, confirm=False: (_ for _ in ()).throw(ValueError("entered values did not match")),
                )
            self.assertIn("VPN_USER=\n", path.read_text(encoding="utf-8"))
            with patch("scripts.homeflix_setup.secrets.getpass.getpass", side_effect=["one", "two"]), patch(
                "scripts.homeflix_setup.secrets.os.open", return_value=41
            ), patch("scripts.homeflix_setup.secrets.os.isatty", return_value=True), patch(
                "scripts.homeflix_setup.secrets.os.fdopen"
            ) as fdopen, patch("scripts.homeflix_setup.secrets.os.close"):
                terminal = MagicMock()
                terminal.__enter__.return_value = terminal
                fdopen.return_value = terminal
                with self.assertRaisesRegex(ValueError, "did not match"):
                    read_from_tty("VPN password: ", confirm=True)


class UsenetSecretHandoffTests(unittest.TestCase):
    def test_secrets_usenet_refuses_json_and_redirected_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "must-not-appear-as-argv-or-json"
            (root / ".env").write_text("USENET_HOST=\nUSENET_USER=\nUSENET_PASSWORD=\n", encoding="utf-8")
            json_code, json_stdout, json_stderr = run_main(
                "--json", "secrets", "usenet", repository_root=root
            )
            pipe_code, pipe_stdout, pipe_stderr = run_main(
                "secrets", "usenet", repository_root=root
            )
            document = (root / ".env").read_text(encoding="utf-8")

        self.assertEqual(json_code, 2)
        self.assertEqual(pipe_code, 2)
        combined = json_stdout + json_stderr + pipe_stdout + pipe_stderr
        self.assertNotIn(secret, combined)
        self.assertNotIn(secret, document)

    def test_secrets_usenet_refuses_secret_tokens_on_argv(self) -> None:
        secret = "must-not-appear-as-argv-or-json"
        parser = build_parser()
        for argv in (("secrets", "usenet", secret), ("secrets", "usenet", "--password", secret)):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    parser.parse_args(list(argv))
                self.assertEqual(raised.exception.code, 2)

    def test_supported_usenet_secrets_confirm_and_update_env_without_leaking_values(self) -> None:
        values = {
            "host": "news.example.test",
            "port": "563",
            "user": "usenet-user",
            "password": "usenet-secret",
        }

        def reader(prompt: str, *, confirm: bool = False) -> str:
            folded = prompt.casefold()
            if "host" in folded:
                return values["host"]
            if "port" in folded:
                return values["port"]
            if "user" in folded:
                return values["user"]
            if "password" in folded:
                return values["password"]
            raise AssertionError(f"unexpected prompt {prompt!r}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OTHER=keep\n", encoding="utf-8")
            result = set_usenet_secrets(path, reader=reader)
            document = EnvDocument.load(path)
            mode = path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)
        self.assertEqual(document.get("USENET_HOST"), values["host"])
        self.assertEqual(document.get("USENET_PORT"), values["port"])
        self.assertEqual(document.get("USENET_USER"), values["user"])
        self.assertEqual(document.get("USENET_PASSWORD"), values["password"])
        self.assertEqual(document.get("OTHER"), "keep")
        rendered = repr(result)
        self.assertNotIn(values["user"], rendered)
        self.assertNotIn(values["password"], rendered)
        names = [item["name"] for item in result["keys"]]
        self.assertEqual(names, ["USENET_HOST", "USENET_PORT", "USENET_USER", "USENET_PASSWORD"])
        self.assertTrue(all(item["secret"] for item in result["keys"] if item["name"] == "USENET_PASSWORD"))


class AcquisitionCredentialIsolationTests(unittest.TestCase):
    def test_unsupported_or_invalid_credentials_fail_acquisition_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = configured(root)
            config["VPN_SERVICE_PROVIDER"] = "nordvpn"
            config["VPN_TYPE"] = "openvpn"
            config["VPN_USER"] = "set"
            config["VPN_PASSWORD"] = "set"
            runner = MountRunner(Path(config["DATA_ROOT"]))
            core = run_preflight(config, "core", runner)
            acquisition = run_preflight(config, "acquisition", runner)
            status_code, status_stdout, status_stderr = run_main(
                "--json", "status", repository_root=root
            )

        self.assertTrue(core.passed)
        self.assertFalse(acquisition.passed)
        provider = next(result for result in acquisition.results if result.name == "vpn_provider")
        self.assertEqual(provider.status, "fail")
        self.assertIn("gluetun-wiki", provider.message)
        self.assertNotIn("set", repr(acquisition.results))
        self.assertEqual(status_code, 0, status_stderr)
        self.assertNotIn("nordvpn", status_stdout + status_stderr)
        self.assertNotIn("VPN_PASSWORD", status_stdout + status_stderr)

    def test_whitespace_credentials_fail_acquisition_and_warn_for_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            config["VPN_SERVICE_PROVIDER"] = "protonvpn"
            config["VPN_TYPE"] = "openvpn"
            config["VPN_USER"] = "   "
            config["VPN_PASSWORD"] = "\t"
            runner = MountRunner(Path(config["DATA_ROOT"]))
            core = run_preflight(config, "core", runner)
            acquisition = run_preflight(config, "acquisition", runner)
        self.assertTrue(core.passed)
        self.assertGreaterEqual(core.counts["warn"], 2)
        self.assertFalse(acquisition.passed)
        self.assertGreaterEqual(acquisition.counts["fail"], 2)


from datetime import datetime, timezone
import json
import os
import subprocess

from scripts.homeflix_setup.acquisition import (
    configure_acquisition,
    deploy_acquisition,
    persist_clients,
    verify_acquisition,
)
from scripts.homeflix_setup.api import HttpResponse
from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.state import SetupState
from scripts.homeflix_setup.vpn import vpn_config_digest
from tests.helpers import parse_single_json
from tests.test_vpn import FakeClock, FakeVpnRunner, write_current_evidence, write_env


QBIT_TEMP = "qbit-temp-session"
QBIT_DURABLE = "qbit-durable-credential"
PROWLARR_KEY = "PROWLARRKEY1234567890ABCD"
RADARR_KEY = "RADARRKEY1234567890ABCDEF"
SONARR_KEY = "SONARRKEY1234567890ABCDEF"
FORWARD_PORT = 5914
FORBIDDEN_LEAKS = (QBIT_TEMP, QBIT_DURABLE, PROWLARR_KEY, RADARR_KEY, SONARR_KEY, str(FORWARD_PORT), "127.0.0.1", "203.0.113.")


def write_acquisition_env(root: Path, *, password: str = "") -> Path:
    path = write_env(root)
    extra = (
        "COMPOSE_PROJECT_NAME=homeflix\n"
        "DOMAIN=homeflix.test\n"
        "QBITTORRENT_PORT=6969\n"
        "NZBGET_PORT=6789\n"
        "PROWLARR_PORT=9696\n"
        f"QBITTORRENT_PASSWORD={password}\n"
        f"CONFIG_ROOT={root / 'config'}\n"
        f"PUID={os.getuid()}\n"
        "QUALITY_PROFILE=HD-1080p\n"
    )
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    path.chmod(0o600)
    return path


def write_fail_closed_evidence(root: Path, image_id: str = "sha256:fixturegluetunimage") -> None:
    write_current_evidence(root, image_id=image_id)
    state = SetupState.load(root / ".homeflix" / "setup.json")
    state.evidence["fail_closed"] = True
    state.save(root / ".homeflix" / "setup.json")


def write_arr_key(root: Path, service: str, key: str) -> None:
    service_dir = root / "config" / service
    service_dir.mkdir(parents=True, exist_ok=True)
    config = service_dir / "config.xml"
    config.write_text(f"<Config><ApiKey>{key}</ApiKey></Config>", encoding="utf-8")
    (root / "config").chmod(0o755)
    service_dir.chmod(0o755)
    config.chmod(0o644)


class FakeAcquisitionRunner(FakeVpnRunner):
    def __init__(self, *args, forwarded_port: int | None = FORWARD_PORT, qbit_logs: str = "", started=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.forwarded_port = forwarded_port
        self.qbit_logs = qbit_logs or (
            "The WebUI administrator username is: admin\n"
            f"The WebUI administrator password was not set. A temporary password is provided for this session: {QBIT_TEMP}\n"
        )
        self.started = set(started or [])

    def run(self, argv, **kwargs):
        command = tuple(argv)
        if "up" in command and "--no-deps" in command:
            self.started.update(command[command.index("--no-deps") + 1 :])
        if "stop" in command:
            after = list(command)
            if "stop" in after:
                self.started.difference_update(after[after.index("stop") + 1 :])
        if command[:2] == ("docker", "logs") and "qbittorrent" in command:
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, self.qbit_logs, "")
        if command[:3] == ("docker", "exec", "gluetun") and "forwarded_port" in " ".join(command):
            self.commands.append(command)
            if self.forwarded_port is None:
                return subprocess.CompletedProcess(command, 1, "", "unavailable")
            return subprocess.CompletedProcess(command, 0, f"{self.forwarded_port}\n", "")
        if command[:2] == ("docker", "inspect") and any(
            name in command for name in ("qbittorrent", "prowlarr", "nzbget")
        ):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, "container:gluetun\n", "")
        if "ps" in command:
            self.commands.append(command)
            payload = list(self.inventory)
            present = {item.get("Service", item.get("service")) for item in payload}
            running = set(self.started) or {"gluetun", "qbittorrent", "prowlarr"}
            if self.started:
                running = set(self.started)
            for service in running:
                if service in present:
                    continue
                payload.append(
                    {
                        "Service": service,
                        "State": "running",
                        "Health": self.health if service == "gluetun" else "healthy",
                        "Project": "homeflix",
                    }
                )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        try:
            return super().run(argv, **kwargs)
        except AssertionError:
            if "up" in command or "stop" in command:
                return subprocess.CompletedProcess(command, self.up_returncode, "", "")
            raise


class AcquisitionDeployTests(unittest.TestCase):
    def test_refuses_to_start_clients_without_current_fail_closed_evidence(self) -> None:
        cases = ("missing", "vpn_only", "stale")
        for kind in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_acquisition_env(root)
                if kind == "vpn_only":
                    write_current_evidence(root)
                elif kind == "stale":
                    write_fail_closed_evidence(root)
                    state = SetupState.load(root / ".homeflix" / "setup.json")
                    state.evidence["recorded_at"] = "2020-01-01T00:00:00Z"
                    state.save(root / ".homeflix" / "setup.json")
                runner = FakeAcquisitionRunner()
                result = deploy_acquisition(
                    root,
                    runner=runner,
                    clock=FakeClock(),
                    sleep=lambda _seconds: None,
                    readiness_timeout=5.0,
                )
                self.assertFalse(result.get("passed") is True, result)
                self.assertEqual(result["status"], "failed")
                checks = {item["domain"]: item for item in result["checks"]}
                self.assertIn(checks["fail_closed"]["status"], {"failure", "unknown"})
                rendered_commands = " ".join(" ".join(command) for command in runner.commands)
                self.assertNotIn("qbittorrent", rendered_commands)
                self.assertNotIn("prowlarr", rendered_commands)
                self.assertNotIn("nzbget", rendered_commands)
                self.assertNotIn(QBIT_DURABLE, json.dumps(result))

    def test_starts_qbittorrent_and_prowlarr_behind_gluetun_and_leaves_nzbget_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_acquisition_env(root)
            write_fail_closed_evidence(root)
            runner = FakeAcquisitionRunner()
            result = deploy_acquisition(
                root,
                runner=runner,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            mutations = [command for command in runner.commands if "up" in command]

        self.assertTrue(result.get("passed") is True, result)
        self.assertTrue(mutations)
        rendered = " ".join(" ".join(command) for command in mutations)
        self.assertIn("gluetun", rendered)
        self.assertIn("qbittorrent", rendered)
        self.assertIn("prowlarr", rendered)
        self.assertNotIn("nzbget", rendered)
        self.assertNotIn(QBIT_DURABLE, json.dumps(result))
        self.assertNotIn(str(FORWARD_PORT), json.dumps(result))

    def test_usenet_selection_starts_nzbget_and_prowlarr_and_leaves_qbittorrent_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_acquisition_env(root)
            write_fail_closed_evidence(root)
            runner = FakeAcquisitionRunner()
            result = deploy_acquisition(
                root,
                runner=runner,
                clients="usenet",
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            mutations = [command for command in runner.commands if "up" in command]
            persisted = SetupState.load(root / ".homeflix" / "setup.json").acquisition_clients

        self.assertTrue(result.get("passed") is True, result)
        self.assertIn("nzbget", result.get("services", []))
        self.assertIn("prowlarr", result.get("services", []))
        self.assertIn("gluetun", result.get("services", []))
        self.assertNotIn("qbittorrent", result.get("services", []))
        rendered = " ".join(" ".join(command) for command in mutations)
        self.assertIn("gluetun", rendered)
        self.assertIn("nzbget", rendered)
        self.assertIn("prowlarr", rendered)
        self.assertNotIn("qbittorrent", rendered)
        checks = {item["domain"]: item for item in result["checks"]}
        self.assertEqual(checks["service:nzbget"]["status"], "pass")
        self.assertEqual(checks["qbittorrent"]["status"], "not-applicable")
        self.assertEqual(persisted, "usenet")

    def test_later_command_without_clients_resumes_persisted_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_acquisition_env(root)
            write_fail_closed_evidence(root)
            first_runner = FakeAcquisitionRunner()
            first = deploy_acquisition(
                root,
                runner=first_runner,
                clients="usenet",
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            resumed = deploy_acquisition(
                root,
                runner=FakeAcquisitionRunner(),
                dry_run=True,
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )

        self.assertTrue(first.get("passed") is True, first)
        self.assertEqual(resumed["status"], "planned")
        self.assertEqual(resumed["clients"], "usenet")
        self.assertEqual(resumed["services"], ["gluetun", "nzbget", "prowlarr"])
        rendered = json.dumps(resumed)
        self.assertIn("nzbget", rendered)
        self.assertNotIn("qbittorrent", rendered)
        self.assertFalse(resumed["state_written"])

    def test_both_selection_starts_torrent_and_usenet_clients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_acquisition_env(root)
            write_fail_closed_evidence(root)
            runner = FakeAcquisitionRunner()
            result = deploy_acquisition(
                root,
                runner=runner,
                clients="both",
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            mutations = [command for command in runner.commands if "up" in command]

        self.assertTrue(result.get("passed") is True, result)
        self.assertEqual(result["services"], ["gluetun", "qbittorrent", "nzbget", "prowlarr"])
        rendered = " ".join(" ".join(command) for command in mutations)
        self.assertIn("qbittorrent", rendered)
        self.assertIn("nzbget", rendered)
        self.assertIn("prowlarr", rendered)
        self.assertFalse(any("stop" in command for command in runner.commands))

    def test_changed_selection_stops_unselected_client_without_deleting_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_acquisition_env(root)
            write_fail_closed_evidence(root)
            config_root = root / "config"
            data_root = root / "data"
            for path in (
                data_root / "torrents" / "movies",
                data_root / "usenet" / "complete" / "movies",
                config_root / "qbittorrent",
                config_root / "nzbget",
            ):
                path.mkdir(parents=True, exist_ok=True)
                (path / "keep-me").write_text("user-data", encoding="utf-8")
            deploy_acquisition(
                root,
                runner=FakeAcquisitionRunner(),
                clients="both",
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            runner = FakeAcquisitionRunner(started={"gluetun", "qbittorrent", "nzbget", "prowlarr"})
            result = deploy_acquisition(
                root,
                runner=runner,
                clients="torrent",
                clock=FakeClock(),
                sleep=lambda _seconds: None,
                readiness_timeout=5.0,
            )
            tokens = {token for command in runner.commands for token in command}
            leftover = {
                path: path.exists()
                for path in (
                    data_root / "torrents" / "movies" / "keep-me",
                    data_root / "usenet" / "complete" / "movies" / "keep-me",
                    config_root / "qbittorrent" / "keep-me",
                    config_root / "nzbget" / "keep-me",
                )
            }

        self.assertTrue(result.get("passed") is True, result)
        self.assertTrue(any("stop" in command and "nzbget" in command for command in runner.commands))
        self.assertFalse(any("qbittorrent" in command and "stop" in command for command in runner.commands))
        self.assertFalse(tokens & {"rm", "down", "volume", "rmi"})
        self.assertTrue(all(leftover.values()), leftover)
        self.assertEqual(result["services"], ["gluetun", "qbittorrent", "prowlarr"])
        self.assertNotIn("nzbget", result["services"])


from urllib.parse import urlsplit

from tests.test_api_nzbget import NzbgetTransport
from tests.test_api_qbittorrent import QbitTransport
from tests.test_api_prowlarr import ProwlarrTransport


def _owned_notifications(service: str) -> list[dict[str, object]]:
    events = {
        "onGrab": False,
        "onDownload": service == "radarr",
        "onUpgrade": service == "radarr",
        "onRename": True,
        "onHealthIssue": False,
        "includeHealthWarnings": False,
        "onHealthRestored": False,
        "onApplicationUpdate": False,
        "onManualInteractionRequired": False,
    }
    if service == "radarr":
        events.update({
            "onMovieAdded": False, "onMovieDelete": False,
            "onMovieFileDelete": False, "onMovieFileDeleteForUpgrade": False,
        })
    else:
        events.update({
            "onImportComplete": True, "onSeriesAdd": False, "onSeriesDelete": False,
            "onEpisodeFileDelete": False, "onEpisodeFileDeleteForUpgrade": False,
        })
        events["onDownload"] = False
    return [
        {
            "id": 21,
            "implementation": "MediaBrowser",
            "fields": [
                {"name": "host", "value": "jellyfin"},
                {"name": "port", "value": 8096},
                {"name": "useSsl", "value": False},
                {"name": "urlBase", "value": ""},
                {"name": "apiKey", "value": "JELLYFIN_DEDICATED_KEY_XX"},
                {"name": "notify", "value": False},
                {"name": "updateLibrary", "value": True},
            ],
            **events,
        },
        {
            "id": 22,
            "implementation": "Webhook",
            "fields": [
                {"name": "url", "value": "http://jellyfin:8096/Library/Refresh"},
                {"name": "method", "value": 1},
                {"name": "headers", "value": [{"key": "X-Emby-Token", "value": "JELLYFIN_DEDICATED_KEY_XX"}]},
            ],
            **events,
        },
    ]


class ArrAcquisitionTransport:
    def __init__(self, service: str) -> None:
        self.service = service
        self.clients: list[dict[str, object]] = []
        self.next_id = 8
        self.requests = []

    def __call__(self, outgoing, timeout):
        self.requests.append(outgoing)
        path = urlsplit(outgoing.full_url).path
        rename = "renameMovies" if self.service == "radarr" else "renameEpisodes"
        root = "/data/media/movies" if self.service == "radarr" else "/data/media/tv"
        if outgoing.method == "GET" and path == "/api/v3/qualityprofile":
            return HttpResponse(200, json.dumps([{"id": 19, "name": "HD-1080p"}]).encode())
        if outgoing.method == "GET" and path == "/api/v3/rootfolder":
            return HttpResponse(200, json.dumps([{"id": 4, "path": root}]).encode())
        if outgoing.method == "GET" and path == "/api/v3/config/naming":
            return HttpResponse(200, json.dumps({"id": 1, rename: True}).encode())
        if outgoing.method == "GET" and path == "/api/v3/config/mediamanagement":
            return HttpResponse(200, json.dumps({"id": 1, "copyUsingHardlinks": True}).encode())
        if outgoing.method == "GET" and path == "/api/v3/config/downloadclient":
            return HttpResponse(200, json.dumps({"id": 2, "enableCompletedDownloadHandling": True}).encode())
        if outgoing.method == "GET" and path == "/api/v3/notification":
            return HttpResponse(200, json.dumps(_owned_notifications(self.service)).encode())
        if outgoing.method == "GET" and path == "/api/v3/downloadclient":
            return HttpResponse(200, json.dumps(self.clients).encode())
        if outgoing.method == "POST" and path == "/api/v3/downloadclient":
            payload = json.loads(outgoing.data.decode())
            payload["id"] = self.next_id
            self.next_id += 1
            self.clients.append(payload)
            return HttpResponse(200, json.dumps(payload).encode())
        if outgoing.method == "PUT" and path.startswith("/api/v3/downloadclient/"):
            ident = int(path.rsplit("/", 1)[-1])
            payload = json.loads(outgoing.data.decode())
            payload["id"] = ident
            self.clients = [payload if item.get("id") == ident else item for item in self.clients]
            return HttpResponse(200, json.dumps(payload).encode())
        raise AssertionError(path)


class CombinedAcquisitionTransport:
    def __init__(self) -> None:
        self.qbit = QbitTransport()
        self.nzbget = NzbgetTransport()
        self.prowlarr = ProwlarrTransport()
        self.radarr = ArrAcquisitionTransport("radarr")
        self.sonarr = ArrAcquisitionTransport("sonarr")

    def as_map(self) -> dict[str, object]:
        return {
            "qbittorrent": self.qbit,
            "nzbget": self.nzbget,
            "prowlarr": self.prowlarr,
            "radarr": self.radarr,
            "sonarr": self.sonarr,
        }

    def host_fields(self) -> list[str]:
        hosts = []
        for client in (*self.radarr.clients, *self.sonarr.clients):
            for field in client.get("fields", []):
                if field.get("name") == "host":
                    hosts.append(str(field.get("value")))
        return hosts


def _prepare_acquisition_root(root: Path) -> None:
    write_acquisition_env(root)
    write_fail_closed_evidence(root)
    write_arr_key(root, "radarr", RADARR_KEY)
    write_arr_key(root, "sonarr", SONARR_KEY)
    write_arr_key(root, "prowlarr", PROWLARR_KEY)


class AcquisitionReconcileTests(unittest.TestCase):
    def test_configure_and_rerun_are_idempotent_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            first = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            env_after = (root / ".env").read_text(encoding="utf-8")
            mode = (root / ".env").stat().st_mode & 0o777
            second = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            rendered = json.dumps(first) + json.dumps(second)

        self.assertEqual(first["status"], "credentials_required")
        self.assertFalse(first["passed"])
        self.assertTrue(first["qbittorrent"]["save_path"])
        self.assertTrue(first["qbittorrent"]["categories"])
        self.assertTrue(first["qbittorrent"]["port_agrees"])
        self.assertTrue(first["qbittorrent"]["bypass_local_auth"])
        self.assertTrue(first["prowlarr"]["radarr_application"])
        self.assertTrue(first["prowlarr"]["sonarr_application"])
        self.assertFalse(first["prowlarr"]["indexer_credentials"])
        self.assertTrue(first["radarr"]["client_exact"])
        self.assertTrue(second["radarr"]["client_exact"])
        self.assertFalse(second["radarr"]["changed"])
        self.assertEqual(len(transports.prowlarr.applications), 2)
        self.assertEqual(len(transports.radarr.clients), 1)
        self.assertEqual(len(transports.sonarr.clients), 1)
        self.assertEqual(transports.host_fields(), ["gluetun", "gluetun"])
        self.assertEqual(mode, 0o600)
        self.assertIn("QBITTORRENT_PASSWORD=", env_after)
        self.assertNotIn(QBIT_TEMP, env_after)
        for leaked in FORBIDDEN_LEAKS:
            self.assertNotIn(leaked, rendered)
        self.assertNotIn(QBIT_TEMP, json.dumps(first))

    def test_verify_is_read_only_and_stops_at_indexer_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            runner.commands.clear()
            result = verify_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            commands = [" ".join(command) for command in runner.commands]

        self.assertEqual(result["status"], "credentials_required")
        self.assertFalse(result["passed"])
        domains = {item["domain"]: item for item in result["checks"]}
        for domain in (
            "fail_closed",
            "namespace",
            "service:qbittorrent",
            "service:prowlarr",
            "paths",
            "categories",
            "connections",
            "port_agrees",
            "jellyfin_discovery",
            "indexers",
        ):
            self.assertIn(domain, domains, domain)
        self.assertEqual(domains["indexers"]["reason"], "provider/indexer credentials required")
        self.assertEqual(domains["indexers"]["status"], "failure")
        self.assertEqual(domains["namespace"]["status"], "pass")
        self.assertEqual(domains["jellyfin_discovery"]["status"], "pass")
        self.assertEqual(domains["port_agrees"]["status"], "pass")
        self.assertFalse(any("up" in command for command in commands))
        self.assertFalse(any("nzbget" in command for command in commands))
        rendered = json.dumps(result)
        for leaked in FORBIDDEN_LEAKS:
            self.assertNotIn(leaked, rendered)

    def test_ambiguous_prowlarr_apps_fail_without_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            transports.prowlarr.applications.append(dict(transports.prowlarr.applications[0], id=99))
            result = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
        self.assertFalse(result.get("passed") is True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len([item for item in transports.prowlarr.applications if item["implementation"] == "Radarr"]), 2)

    def test_usenet_configure_and_verify_use_nzbget_only_and_require_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            first = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clients="usenet",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            second = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clients="usenet",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            verified = verify_acquisition(
                root,
                runner=FakeAcquisitionRunner(started={"gluetun", "nzbget", "prowlarr"}),
                transports=transports.as_map(),
                clients="usenet",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            implementations = [
                client.get("implementation")
                for client in (*transports.radarr.clients, *transports.sonarr.clients)
            ]
            rendered = json.dumps(first) + json.dumps(second) + json.dumps(verified)

        self.assertEqual(first["status"], "credentials_required")
        self.assertFalse(first["passed"])
        self.assertTrue(first["nzbget"]["paths"])
        self.assertTrue(first["nzbget"]["categories"])
        self.assertFalse(first["nzbget"]["news_servers"])
        self.assertNotIn("qbittorrent", first)
        self.assertEqual(implementations, ["Nzbget", "Nzbget"])
        self.assertEqual(transports.host_fields(), ["gluetun", "gluetun"])
        self.assertFalse(second["radarr"]["changed"])
        self.assertEqual(len(transports.radarr.clients), 1)
        self.assertEqual(verified["status"], "credentials_required")
        self.assertFalse(verified["passed"])
        domains = {item["domain"]: item for item in verified["checks"]}
        self.assertEqual(domains["service:nzbget"]["status"], "pass")
        self.assertEqual(domains["qbittorrent"]["status"], "not-applicable")
        self.assertNotIn("service:qbittorrent", domains)
        self.assertEqual(domains["news_servers"]["status"], "failure")
        self.assertNotEqual(verified["status"], "verified")
        self.assertNotIn("tegbzn6789", rendered)
        for leaked in FORBIDDEN_LEAKS:
            self.assertNotIn(leaked, rendered)

    def test_both_selection_reconciles_both_clients_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            first = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clients="both",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            second = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clients="both",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            implementations = {
                client.get("implementation")
                for client in (*transports.radarr.clients, *transports.sonarr.clients)
            }

        self.assertEqual(first["status"], "credentials_required")
        self.assertTrue(first["qbittorrent"]["save_path"])
        self.assertTrue(first["nzbget"]["paths"])
        self.assertEqual(implementations, {"QBittorrent", "Nzbget"})
        self.assertEqual(len(transports.radarr.clients), 2)
        self.assertEqual(len(transports.sonarr.clients), 2)
        self.assertFalse(second["radarr"]["changed"])
        self.assertEqual(transports.host_fields(), ["gluetun"] * 4)


class AcquisitionCliTests(unittest.TestCase):
    def test_unknown_phase_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for command in (
                ("initialize", "usenet"),
                ("deploy", "usenet"),
                ("verify", "usenet"),
                ("setup", "usenet"),
            ):
                with self.subTest(command=command):
                    code, stdout, stderr = run_main("--json", *command, repository_root=root)
                    self.assertEqual(code, 2)
                    self.assertNotIn(QBIT_DURABLE, stdout + stderr)

    def test_setup_acquisition_dry_run_plans_torrent_clients_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, stdout, stderr = run_main(
                "--json", "setup", "acquisition", "--dry-run", repository_root=root
            )
        self.assertEqual(code, 0, stderr)
        payload = parse_single_json(stdout)
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["services"], ["gluetun", "qbittorrent", "prowlarr"])
        rendered = json.dumps(payload)
        self.assertNotIn("nzbget", rendered)
        self.assertIn("initialize", rendered)

    def test_setup_and_deploy_dry_run_honor_clients_flag(self) -> None:
        cases = {
            "usenet": (["gluetun", "nzbget", "prowlarr"], "qbittorrent"),
            "both": (["gluetun", "qbittorrent", "nzbget", "prowlarr"], None),
            "torrent": (["gluetun", "qbittorrent", "prowlarr"], "nzbget"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_acquisition_env(root)
            write_fail_closed_evidence(root)
            for selection, (services, absent) in cases.items():
                with self.subTest(selection=selection):
                    setup_code, setup_stdout, setup_stderr = run_main(
                        "--json",
                        "setup",
                        "acquisition",
                        "--dry-run",
                        "--clients",
                        selection,
                        repository_root=root,
                    )
                    with patch(
                        "scripts.homeflix_setup.acquisition._inspect_image_id",
                        return_value="sha256:fixturegluetunimage",
                    ):
                        deploy_code, deploy_stdout, deploy_stderr = run_main(
                            "--json",
                            "deploy",
                            "acquisition",
                            "--dry-run",
                            "--clients",
                            selection,
                            repository_root=root,
                        )
                    setup = parse_single_json(setup_stdout)
                    deploy = parse_single_json(deploy_stdout)
                    self.assertEqual(setup_code, 0, setup_stderr)
                    self.assertEqual(deploy_code, 0, deploy_stderr)
                    self.assertEqual(setup["services"], services)
                    self.assertEqual(deploy["services"], services)
                    rendered = json.dumps(setup) + json.dumps(deploy)
                    if absent:
                        self.assertNotIn(absent, json.dumps(setup.get("acquisition_mutations", [])))
                        self.assertFalse(
                            any(absent in command for command in deploy.get("mutation_commands", []))
                        )
                    self.assertNotIn("tegbzn6789", rendered)

    def _assert_homeflix_phase_commands_include_clients(
        self, commands: list[object], selection: str
    ) -> None:
        required = {"initialize", "verify"}
        seen: set[str] = set()
        for command in commands:
            if not isinstance(command, list) or len(command) < 3:
                continue
            tokens = [str(item) for item in command]
            if tokens[0] != "scripts/homeflix" or tokens[1] not in required | {"deploy"}:
                continue
            seen.add(tokens[1])
            self.assertIn("--clients", tokens, tokens)
            self.assertEqual(tokens[tokens.index("--clients") + 1], selection, tokens)
        self.assertTrue(required <= seen, seen)

    def test_setup_acquisition_dry_run_commands_include_clients_and_changed_selection_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for selection in ("usenet", "both"):
                with self.subTest(selection=selection, prior=None):
                    code, stdout, stderr = run_main(
                        "--json",
                        "setup",
                        "acquisition",
                        "--dry-run",
                        "--clients",
                        selection,
                        repository_root=root,
                    )
                    self.assertEqual(code, 0, stderr)
                    payload = parse_single_json(stdout)
                    self.assertEqual(payload["clients"], selection)
                    self.assertFalse(payload["state_written"])
                    self._assert_homeflix_phase_commands_include_clients(
                        payload["commands"], selection
                    )
                    self.assertFalse(
                        any("stop" in command for command in payload["commands"] if isinstance(command, list))
                    )
            persist_clients(root, "torrent")
            code, stdout, stderr = run_main(
                "--json",
                "setup",
                "acquisition",
                "--dry-run",
                "--clients",
                "usenet",
                repository_root=root,
            )
            self.assertEqual(code, 0, stderr)
            payload = parse_single_json(stdout)
            self._assert_homeflix_phase_commands_include_clients(payload["commands"], "usenet")
            self.assertTrue(
                any(
                    isinstance(command, list)
                    and "stop" in command
                    and "qbittorrent" in command
                    for command in payload["commands"]
                ),
                payload["commands"],
            )
            self.assertFalse(payload["state_written"])

    def test_fixture_journey_reaches_indexer_credentials_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            deploy = deploy_acquisition(root, runner=runner, clock=FakeClock(), sleep=lambda _s: None, readiness_timeout=5.0)
            configured = configure_acquisition(
                root, runner=runner, transports=transports.as_map(), clock=FakeClock(), readiness_timeout=5.0
            )
            verified = verify_acquisition(
                root, runner=runner, transports=transports.as_map(), clock=FakeClock(), readiness_timeout=5.0
            )
            with patch("scripts.homeflix_setup.cli.configure_acquisition", return_value=configured), patch(
                "scripts.homeflix_setup.cli.deploy_acquisition", return_value=deploy
            ), patch("scripts.homeflix_setup.cli.verify_acquisition", return_value=verified), patch(
                "scripts.homeflix_setup.cli.run_preflight",
                return_value=__import__("scripts.homeflix_setup.preflight", fromlist=["PreflightReport"]).PreflightReport(
                    "acquisition",
                    (__import__("scripts.homeflix_setup.preflight", fromlist=["CheckResult"]).CheckResult("fixture", "pass", "passed"),),
                ),
            ):
                code, stdout, stderr = run_main("--json", "setup", "acquisition", repository_root=root)
        payload = parse_single_json(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "credentials_required")
        self.assertNotEqual(payload["status"], "verified")
        rendered = stdout + stderr
        for leaked in FORBIDDEN_LEAKS:
            self.assertNotIn(leaked, rendered)
        self.assertNotIn("grab", rendered.casefold())
        self.assertIn("credentials_required", rendered)

    def test_setup_stale_vpn_evidence_fails_deploy_phase_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            failed = {
                "status": "failed",
                "passed": False,
                "checks": [
                    {
                        "domain": "fail_closed",
                        "status": "failure",
                        "reason": "current fail-closed evidence is required",
                    }
                ],
            }
            with patch("scripts.homeflix_setup.cli.deploy_acquisition", return_value=failed), patch(
                "scripts.homeflix_setup.cli.run_preflight",
                return_value=__import__("scripts.homeflix_setup.preflight", fromlist=["PreflightReport"]).PreflightReport(
                    "acquisition",
                    (__import__("scripts.homeflix_setup.preflight", fromlist=["CheckResult"]).CheckResult("fixture", "pass", "passed"),),
                ),
            ):
                code, stdout, stderr = run_main("--json", "setup", "acquisition", repository_root=root)
        payload = parse_single_json(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "deployment_failed")
        phases = {item["phase"]: item["status"] for item in payload["phases"]}
        self.assertEqual(phases["deploy:acquisition"], "fail")
        self.assertEqual(phases["initialize:acquisition"], "skipped")
        self.assertEqual(phases["verify:acquisition"], "skipped")
        self.assertNotEqual(payload["status"], "verified")

    def test_setup_usenet_dry_run_and_live_stop_at_credentials_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare_acquisition_root(root)
            runner = FakeAcquisitionRunner()
            transports = CombinedAcquisitionTransport()
            deploy = deploy_acquisition(
                root,
                runner=runner,
                clients="usenet",
                clock=FakeClock(),
                sleep=lambda _s: None,
                readiness_timeout=5.0,
            )
            configured = configure_acquisition(
                root,
                runner=runner,
                transports=transports.as_map(),
                clients="usenet",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            verified = verify_acquisition(
                root,
                runner=FakeAcquisitionRunner(started={"gluetun", "nzbget", "prowlarr"}),
                transports=transports.as_map(),
                clients="usenet",
                clock=FakeClock(),
                readiness_timeout=5.0,
            )
            with patch("scripts.homeflix_setup.cli.configure_acquisition", return_value=configured), patch(
                "scripts.homeflix_setup.cli.deploy_acquisition", return_value=deploy
            ), patch("scripts.homeflix_setup.cli.verify_acquisition", return_value=verified), patch(
                "scripts.homeflix_setup.cli.run_preflight",
                return_value=__import__("scripts.homeflix_setup.preflight", fromlist=["PreflightReport"]).PreflightReport(
                    "acquisition",
                    (__import__("scripts.homeflix_setup.preflight", fromlist=["CheckResult"]).CheckResult("fixture", "pass", "passed"),),
                ),
            ):
                live_code, live_stdout, live_stderr = run_main(
                    "--json", "setup", "acquisition", "--clients", "usenet", repository_root=root
                )
                dry_code, dry_stdout, dry_stderr = run_main(
                    "--json", "setup", "acquisition", "--dry-run", "--clients", "usenet", repository_root=root
                )
        live = parse_single_json(live_stdout)
        dry = parse_single_json(dry_stdout)
        self.assertEqual(live_code, 0)
        self.assertEqual(live["status"], "credentials_required")
        self.assertNotEqual(live["status"], "verified")
        self.assertEqual(dry_code, 0)
        self.assertEqual(dry["services"], ["gluetun", "nzbget", "prowlarr"])
        self.assertNotIn("qbittorrent", json.dumps(dry["acquisition_mutations"]))
        self.assertIn("secrets usenet", json.dumps(dry["required_human_inputs"]))
        rendered = live_stdout + live_stderr + dry_stdout + dry_stderr
        self.assertNotIn("tegbzn6789", rendered)
        for leaked in FORBIDDEN_LEAKS:
            self.assertNotIn(leaked, rendered)
