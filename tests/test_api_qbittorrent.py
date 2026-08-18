from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from scripts.homeflix_setup.api import ApiError, HttpResponse
from scripts.homeflix_setup.api.qbittorrent import (
    CATEGORIES,
    QBITTORRENT_PASSWORD_KEY,
    QBittorrentClient,
    extract_temporary_password,
    reconcile_webui_credential,
)


TEMP = "qbit-temp-session"
DURABLE = "qbit-durable-credential"
STOCK_PATH = "/downloads"


class QbitTransport:
    def __init__(self, *, login_no_content: bool = False, cookie_name: str = "SID") -> None:
        self.sid = "fixture-sid"
        self.password = TEMP
        self.login_no_content = login_no_content
        self.cookie_name = cookie_name
        self.prefs = {
            "save_path": STOCK_PATH,
            "temp_path_enabled": True,
            "temp_path": "/downloads/incomplete",
            "bypass_local_auth": False,
            "listen_port": 6881,
        }
        self.cats: dict[str, dict[str, str]] = {}
        self.requests: list[object] = []

    def __call__(self, outgoing, timeout):
        self.requests.append(outgoing)
        path = urlsplit(outgoing.full_url).path
        form = {}
        if outgoing.data:
            form = {key: values[0] for key, values in parse_qs(outgoing.data.decode()).items()}
        if outgoing.method == "POST" and path.startswith("/api/v2/auth/login"):
            if form.get("username") == "admin" and form.get("password") == self.password:
                issued = {"set-cookie": f"{self.cookie_name}={self.sid}; HttpOnly; SameSite=Lax; path=/"}
                if self.login_no_content:
                    return HttpResponse(204, b"", issued)
                return HttpResponse(200, b"Ok.", issued)
            return HttpResponse(200, b"Fails.")
        cookie = outgoing.headers.get("Cookie") or outgoing.headers.get("cookie") or ""
        if f"{self.cookie_name}={self.sid}" not in cookie:
            return HttpResponse(403, b"Forbidden")
        if outgoing.method == "GET" and path.startswith("/api/v2/app/preferences"):
            return HttpResponse(200, json.dumps(self.prefs).encode())
        if outgoing.method == "POST" and path.startswith("/api/v2/app/setPreferences"):
            updates = json.loads(form["json"])
            if "web_ui_password" in updates:
                self.password = updates["web_ui_password"]
            self.prefs.update({key: value for key, value in updates.items() if key != "web_ui_password"})
            return HttpResponse(200, b"")
        if outgoing.method == "GET" and path.startswith("/api/v2/torrents/categories"):
            return HttpResponse(200, json.dumps(self.cats).encode())
        if outgoing.method == "POST" and path.startswith("/api/v2/torrents/createCategory"):
            self.cats[form["category"]] = {"name": form["category"], "savePath": form["savePath"]}
            return HttpResponse(200, b"")
        if outgoing.method == "POST" and path.startswith("/api/v2/torrents/editCategory"):
            self.cats[form["category"]] = {"name": form["category"], "savePath": form["savePath"]}
            return HttpResponse(200, b"")
        raise AssertionError(path)


class LogRunner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, **kwargs):
        command = tuple(argv)
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, self.text, "")


class QBittorrentApiTests(unittest.TestCase):
    def test_rejects_non_loopback_bases(self) -> None:
        for base in ("http://gluetun:6969", "http://qbittorrent:6969", "https://127.0.0.1:6969"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                QBittorrentClient(base)

    def test_extracts_session_password_from_bounded_logs(self) -> None:
        text = (
            "noise\n"
            f"A temporary password is provided for this session: {TEMP}\n"
            "later line\n"
        )
        self.assertEqual(extract_temporary_password(text), TEMP)

    def test_consumes_temp_password_and_writes_durable_env_secret_once(self) -> None:
        transport = QbitTransport()
        client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("QBITTORRENT_PORT=6969\n", encoding="utf-8")
            runner = LogRunner(f"session: {TEMP}\n")
            first = reconcile_webui_credential(client, env, runner)
            stored = env.read_text(encoding="utf-8")
            mode = env.stat().st_mode & 0o777
            second = reconcile_webui_credential(client, env, runner)
            after = env.read_text(encoding="utf-8")

        self.assertTrue(first["credential_updated"])
        self.assertFalse(second["credential_updated"])
        self.assertEqual(mode, 0o600)
        self.assertIn(QBITTORRENT_PASSWORD_KEY + "=", stored)
        self.assertNotIn(TEMP, stored)
        self.assertEqual(stored, after)
        durable = stored.split(QBITTORRENT_PASSWORD_KEY + "=", 1)[1].strip()
        self.assertTrue(durable)
        self.assertNotEqual(durable, TEMP)
        self.assertNotIn(TEMP, json.dumps(first) + json.dumps(second))
        self.assertNotIn(durable, json.dumps(first) + json.dumps(second))

    def test_configure_repairs_paths_categories_bypass_and_port_without_leaking(self) -> None:
        transport = QbitTransport()
        client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
        self.assertTrue(client.login("admin", TEMP))
        result = client.configure(forwarded_port=5914)
        rerun = client.configure(forwarded_port=5914)
        rendered = json.dumps(result) + json.dumps(rerun)

        self.assertTrue(result["save_path"])
        self.assertTrue(result["incomplete"])
        self.assertTrue(result["categories"])
        self.assertTrue(result["bypass_local_auth"])
        self.assertTrue(result["port_agrees"])
        self.assertTrue(result["preferences_changed"])
        self.assertTrue(result["categories_changed"])
        self.assertFalse(rerun["preferences_changed"])
        self.assertFalse(rerun["categories_changed"])
        self.assertEqual(transport.prefs["save_path"], "/data/torrents")
        self.assertFalse(transport.prefs["temp_path_enabled"])
        self.assertTrue(transport.prefs["bypass_local_auth"])
        self.assertEqual(transport.prefs["listen_port"], 5914)
        self.assertEqual(
            {name: item["savePath"] for name, item in transport.cats.items()},
            CATEGORIES,
        )
        self.assertNotIn("5914", rendered)
        self.assertNotIn(TEMP, rendered)
        self.assertNotIn(STOCK_PATH, rendered)

    def test_inspect_port_agreement_is_boolean_and_missing_forward_is_false(self) -> None:
        transport = QbitTransport()
        transport.prefs.update(
            {
                "save_path": "/data/torrents",
                "temp_path_enabled": False,
                "bypass_local_auth": True,
                "listen_port": 5914,
            }
        )
        transport.cats = {
            name: {"name": name, "savePath": path} for name, path in CATEGORIES.items()
        }
        client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
        self.assertTrue(client.login("admin", TEMP))
        agreed = client.inspect(forwarded_port=5914)
        missing = client.inspect(forwarded_port=None)
        self.assertTrue(agreed["port_agrees"])
        self.assertFalse(missing["port_agrees"])
        self.assertNotIn("5914", json.dumps(agreed) + json.dumps(missing))


DIALECTS = (
    {"login_no_content": False, "cookie_name": "SID"},
    {"login_no_content": True, "cookie_name": "QBT_SID_6969"},
)


class LoginDialectTests(unittest.TestCase):
    def test_login_succeeds_in_legacy_and_5x_dialects(self) -> None:
        for dialect in DIALECTS:
            with self.subTest(**dialect):
                transport = QbitTransport(**dialect)
                client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
                self.assertTrue(client.login("admin", TEMP))

    def test_login_rejects_wrong_credentials_in_both_dialects(self) -> None:
        for dialect in DIALECTS:
            with self.subTest(**dialect):
                transport = QbitTransport(**dialect)
                client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
                self.assertFalse(client.login("admin", "wrong-password"))

    def test_stale_session_cookie_cannot_mask_a_rejected_login(self) -> None:
        transport = QbitTransport(**DIALECTS[1])
        client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
        self.assertTrue(client.login("admin", TEMP))
        self.assertFalse(client.login("admin", "wrong-password"))

    def test_authenticated_request_echoes_the_issued_cookie_name(self) -> None:
        for dialect in DIALECTS:
            with self.subTest(**dialect):
                transport = QbitTransport(**dialect)
                client = QBittorrentClient("http://127.0.0.1:6969", transport=transport)
                self.assertTrue(client.login("admin", TEMP))
                client.preferences()
                sent = [
                    request.headers.get("Cookie") or request.headers.get("cookie") or ""
                    for request in transport.requests
                ]
                self.assertTrue(
                    any(value.startswith(dialect["cookie_name"] + "=") for value in sent), sent
                )
