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
from scripts.homeflix_setup.secrets import set_vpn_secrets
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

from scripts.homeflix_setup.acquisition import configure_acquisition, deploy_acquisition, verify_acquisition
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
    def __init__(self, *args, forwarded_port: int | None = FORWARD_PORT, qbit_logs: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.forwarded_port = forwarded_port
        self.qbit_logs = qbit_logs or (
            "The WebUI administrator username is: admin\n"
            f"The WebUI administrator password was not set. A temporary password is provided for this session: {QBIT_TEMP}\n"
        )

    def run(self, argv, **kwargs):
        command = tuple(argv)
        if command[:2] == ("docker", "logs") and "qbittorrent" in command:
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, self.qbit_logs, "")
        if command[:3] == ("docker", "exec", "gluetun") and "forwarded_port" in " ".join(command):
            self.commands.append(command)
            if self.forwarded_port is None:
                return subprocess.CompletedProcess(command, 1, "", "unavailable")
            return subprocess.CompletedProcess(command, 0, f"{self.forwarded_port}\n", "")
        if command[:2] == ("docker", "inspect") and any(name in command for name in ("qbittorrent", "prowlarr")):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, "container:gluetun\n", "")
        if "ps" in command:
            self.commands.append(command)
            payload = list(self.inventory)
            for service, health in (
                ("gluetun", self.health),
                ("qbittorrent", "healthy"),
                ("prowlarr", "healthy"),
            ):
                if not any(item.get("Service", item.get("service")) == service for item in payload):
                    payload.append(
                        {
                            "Service": service,
                            "State": "running",
                            "Health": health,
                            "Project": "homeflix",
                        }
                    )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        try:
            return super().run(argv, **kwargs)
        except AssertionError:
            if "up" in command:
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


from urllib.parse import urlsplit

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
        self.prowlarr = ProwlarrTransport()
        self.radarr = ArrAcquisitionTransport("radarr")
        self.sonarr = ArrAcquisitionTransport("sonarr")

    def as_map(self) -> dict[str, object]:
        return {
            "qbittorrent": self.qbit,
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
