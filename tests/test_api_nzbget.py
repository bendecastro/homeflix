from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from scripts.homeflix_setup.api import ApiError, HttpResponse
from scripts.homeflix_setup.api.nzbget import (
    CATEGORIES,
    DEFAULT_USER,
    DEST_DIR,
    INTER_DIR,
    LINUXSERVER_DEFAULT_PASSWORD,
    NZBGET_PASSWORD_KEY,
    NZBGET_USER_KEY,
    NzbgetClient,
    reconcile_control_credential,
)
from scripts.homeflix_setup.envfile import EnvDocument


def _option_items(options: dict[str, str]) -> list[dict[str, str]]:
    return [{"Name": name, "Value": value} for name, value in options.items()]


class NzbgetTransport:
    """Models official NZBGet JSON-RPC: saveconfig writes the file only."""

    def __init__(self) -> None:
        self.username = DEFAULT_USER
        self.password = LINUXSERVER_DEFAULT_PASSWORD
        self.file_options = {
            "ControlUsername": DEFAULT_USER,
            "ControlPassword": LINUXSERVER_DEFAULT_PASSWORD,
            "MainDir": "/downloads",
            "DestDir": "${MainDir}",
            "InterDir": "${MainDir}/incomplete",
            "ScriptDir": "${MainDir}/scripts",
            "Category1.Name": "Movies",
            "Category1.DestDir": "${MainDir}/Movies",
            "Server1.Name": "news",
            "Server1.Host": "",
            "Server1.Port": "563",
            "Server1.Username": "",
            "Server1.Password": "",
            "Server1.Encryption": "yes",
            "Server1.Connections": "8",
            "Server1.Active": "yes",
        }
        self.runtime_options = self._expand(self.file_options)
        self.saved: list[list[dict[str, str]]] = []
        self.requests: list[object] = []
        self.reloads = 0
        self.auth_warmup_requests = 1
        self._pending_auth: tuple[str, str] | None = None
        self._auth_not_ready = 0

    @property
    def options(self) -> dict[str, str]:
        return self.runtime_options

    def _expand(self, options: dict[str, str]) -> dict[str, str]:
        main = options.get("MainDir", "")
        return {name: value.replace("${MainDir}", main) for name, value in options.items()}

    def _authorized(self, outgoing) -> bool:
        header = outgoing.headers.get("Authorization") or outgoing.headers.get("authorization") or ""
        if self._auth_not_ready:
            self._auth_not_ready -= 1
            if self._auth_not_ready == 0 and self._pending_auth is not None:
                self.username, self.password = self._pending_auth
                self._pending_auth = None
            return False
        expected = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        return header == expected

    def __call__(self, outgoing, timeout):
        self.requests.append(outgoing)
        path = urlsplit(outgoing.full_url).path
        if path != "/jsonrpc":
            return HttpResponse(404, b"{}")
        payload = json.loads(outgoing.data.decode()) if outgoing.data else {}
        method = payload.get("method")
        params = payload.get("params") or []
        if not self._authorized(outgoing):
            return HttpResponse(401, b"{}")
        if method == "config":
            return HttpResponse(200, json.dumps({"result": _option_items(self.runtime_options)}).encode())
        if method == "loadconfig":
            return HttpResponse(200, json.dumps({"result": _option_items(self.file_options)}).encode())
        if method == "saveconfig":
            options = params[0] if params else []
            self.saved.append(options)
            for item in options:
                self.file_options[item["Name"]] = item["Value"]
            return HttpResponse(200, json.dumps({"result": True}).encode())
        if method == "reload":
            self.reloads += 1
            self.runtime_options = self._expand(self.file_options)
            next_user = self.file_options.get("ControlUsername") or self.username
            next_password = self.file_options.get("ControlPassword") or self.password
            if (next_user, next_password) != (self.username, self.password):
                self._pending_auth = (next_user, next_password)
                self._auth_not_ready = max(int(self.auth_warmup_requests), 0)
            else:
                self.username, self.password = next_user, next_password
            return HttpResponse(200, json.dumps({"result": True}).encode())
        return HttpResponse(200, json.dumps({"error": {"message": method}}).encode())


class NzbgetAdapterTests(unittest.TestCase):
    def test_configure_writes_control_credentials_paths_and_categories_without_exposing_secrets(self) -> None:
        transport = NzbgetTransport()
        client = NzbgetClient("http://127.0.0.1:6789", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NZBGET_PORT=6789\n", encoding="utf-8")
            env_path.chmod(0o600)
            credential = reconcile_control_credential(client, env_path, sleep=lambda _seconds: None)
            result = client.configure()
            document = EnvDocument.load(env_path)
            rendered = json.dumps(result) + json.dumps(credential)

        password = document.get(NZBGET_PASSWORD_KEY) or ""
        self.assertTrue(credential["credential_updated"])
        self.assertTrue(password)
        self.assertNotEqual(password, LINUXSERVER_DEFAULT_PASSWORD)
        self.assertEqual(document.get(NZBGET_USER_KEY), DEFAULT_USER)
        self.assertEqual(transport.options["ControlUsername"], DEFAULT_USER)
        self.assertEqual(transport.options["ControlPassword"], password)
        self.assertEqual(transport.options["InterDir"], INTER_DIR)
        self.assertEqual(transport.options["DestDir"], DEST_DIR)
        for name, dest in CATEGORIES.items():
            self.assertIn(dest, transport.options.values(), name)
        self.assertEqual(transport.options["Server1.Active"], "no")
        self.assertTrue(result["paths"])
        self.assertTrue(result["categories"])
        self.assertFalse(result["news_servers"])
        self.assertNotIn(password, rendered)
        self.assertNotIn(LINUXSERVER_DEFAULT_PASSWORD, rendered)
        self.assertNotIn("/downloads", rendered)

    def test_second_configure_is_idempotent_and_keeps_servers_disabled_without_credentials(self) -> None:
        transport = NzbgetTransport()
        client = NzbgetClient("http://127.0.0.1:6789", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NZBGET_PORT=6789\n", encoding="utf-8")
            reconcile_control_credential(client, env_path, sleep=lambda _seconds: None)
            first = client.configure()
            saves_after_first = len(transport.saved)
            second = client.configure()
            inspect = client.inspect()

        self.assertTrue(first["paths"])
        self.assertEqual(len(transport.saved), saves_after_first)
        self.assertFalse(second.get("changed", True))
        self.assertTrue(inspect["paths"])
        self.assertTrue(inspect["categories"])
        self.assertFalse(inspect["news_servers"])
        self.assertEqual(transport.options["Server1.Active"], "no")
        self.assertNotIn(LINUXSERVER_DEFAULT_PASSWORD, json.dumps(inspect))

    def test_news_server_stays_disabled_until_supplied_then_activates_once(self) -> None:
        transport = NzbgetTransport()
        client = NzbgetClient("http://127.0.0.1:6789", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NZBGET_PORT=6789\n", encoding="utf-8")
            reconcile_control_credential(client, env_path, sleep=lambda _seconds: None)
            before = client.configure()
            after = client.configure(
                news_server={
                    "host": "news.example.test",
                    "port": "563",
                    "username": "usenet-user",
                    "password": "usenet-secret",
                }
            )
            inspect = client.inspect()

        self.assertFalse(before["news_servers"])
        self.assertEqual(transport.options["Server1.Active"], "yes")
        self.assertEqual(transport.options["Server1.Host"], "news.example.test")
        self.assertTrue(after["news_servers"])
        self.assertTrue(inspect["news_servers"])
        self.assertNotIn("usenet-secret", json.dumps(after) + json.dumps(inspect))

    def test_control_rotation_persists_env_before_post_reload_login_is_ready(self) -> None:
        transport = NzbgetTransport()
        client = NzbgetClient("http://127.0.0.1:6789", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NZBGET_PORT=6789\n", encoding="utf-8")
            env_path.chmod(0o600)
            credential = reconcile_control_credential(client, env_path, sleep=lambda _seconds: None)
            document = EnvDocument.load(env_path)
            password = document.get(NZBGET_PASSWORD_KEY) or ""
            file_password = transport.file_options.get("ControlPassword") or ""
            stored_login = NzbgetClient(
                "http://127.0.0.1:6789", transport=transport, username=DEFAULT_USER, password=password
            ).login(DEFAULT_USER, password)
            env_text = env_path.read_text(encoding="utf-8")

        self.assertTrue(credential["credential_updated"])
        self.assertTrue(password)
        self.assertNotEqual(password, LINUXSERVER_DEFAULT_PASSWORD)
        self.assertEqual(file_password, password)
        self.assertEqual(document.get(NZBGET_USER_KEY), DEFAULT_USER)
        self.assertTrue(stored_login)
        self.assertGreaterEqual(transport.reloads, 1)
        self.assertIn(NZBGET_PASSWORD_KEY, env_text)
        self.assertNotIn(LINUXSERVER_DEFAULT_PASSWORD, env_text)

    def test_control_rotation_writes_env_when_post_reload_login_stays_unready(self) -> None:
        transport = NzbgetTransport()
        transport.auth_warmup_requests = 100
        client = NzbgetClient("http://127.0.0.1:6789", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NZBGET_PORT=6789\n", encoding="utf-8")
            env_path.chmod(0o600)
            with self.assertRaises(ApiError) as raised:
                reconcile_control_credential(client, env_path, sleep=lambda _seconds: None)
            document = EnvDocument.load(env_path)
            password = document.get(NZBGET_PASSWORD_KEY) or ""

        self.assertEqual(raised.exception.code, "authentication_failed")
        self.assertTrue(password)
        self.assertEqual(transport.file_options.get("ControlPassword"), password)
        self.assertNotEqual(password, LINUXSERVER_DEFAULT_PASSWORD)

    def test_save_options_preserves_file_variable_references(self) -> None:
        transport = NzbgetTransport()
        client = NzbgetClient(
            "http://127.0.0.1:6789",
            transport=transport,
            password=LINUXSERVER_DEFAULT_PASSWORD,
        )
        client.save_options({"DestDir": DEST_DIR})
        payload = {item["Name"]: item["Value"] for item in transport.saved[-1]}
        self.assertEqual(payload["ScriptDir"], "${MainDir}/scripts")
        self.assertEqual(payload["DestDir"], DEST_DIR)
        self.assertEqual(payload["InterDir"], "${MainDir}/incomplete")
        self.assertEqual(transport.runtime_options["ScriptDir"], "/downloads/scripts")
        self.assertEqual(transport.runtime_options["DestDir"], "/downloads")

    def test_rejects_non_loopback_base(self) -> None:
        with self.assertRaises(ValueError):
            NzbgetClient("http://nzbget:6789")


if __name__ == "__main__":
    unittest.main()
