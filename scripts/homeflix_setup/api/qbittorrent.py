"""qBittorrent WebUI API v2 adapter. Secrets stay out of returned payloads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Mapping
from urllib import request
from urllib.parse import parse_qs, urlencode, urlsplit

from ..command import CommandRunner
from ..envfile import update_env
from .client import ApiError, HttpResponse, Transport, urllib_transport


QBITTORRENT_USER = "admin"
QBITTORRENT_PASSWORD_KEY = "QBITTORRENT_PASSWORD"
DEFAULT_PASSWORD = "adminadmin"
SAVE_PATH = "/data/torrents"
CATEGORIES = {
    "movies": "/data/torrents/movies",
    "tv": "/data/torrents/tv",
    "music": "/data/torrents/music",
}
_TEMP_PASSWORD = re.compile(r"session:\s*(\S+)")
_SID = re.compile(r"(?:^|;\s*)SID=([^;]+)", re.I)


def _loopback_base(service: str, base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("API base must be a plain-HTTP loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("API base contains unsupported components")
    return base_url.rstrip("/") + "/"


def extract_temporary_password(log_text: str) -> str | None:
    matches = _TEMP_PASSWORD.findall(log_text or "")
    if not matches:
        return None
    return matches[-1]


def read_temporary_password(runner: CommandRunner, *, timeout: float = 10.0) -> str | None:
    result = runner.run(("docker", "logs", "qbittorrent"), check=False, timeout=timeout)
    if result.returncode:
        return None
    return extract_temporary_password((result.stdout or "") + "\n" + (result.stderr or ""))


class QBittorrentClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport = urllib_transport,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 5.0,
    ) -> None:
        self.service = "qbittorrent"
        self.base_url = _loopback_base(self.service, base_url)
        self.transport = transport
        self.deadline = deadline
        self.clock = clock
        self.timeout = min(max(float(timeout), 0.1), 15.0)
        self._sid: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        form: Mapping[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        headers = {
            "Accept": "application/json, text/plain",
            "Referer": self.base_url,
            "Origin": self.base_url.rstrip("/"),
        }
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        encoded = None if form is None else urlencode(form).encode()
        if encoded is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        outgoing = request.Request(
            self.base_url.rstrip("/") + path,
            data=encoded,
            headers=headers,
            method=method.upper(),
        )
        remaining = self.timeout if self.deadline is None else min(self.timeout, self.deadline - self.clock())
        if remaining <= 0:
            raise ApiError(self.service, operation, None, "deadline_exhausted")
        try:
            response = self.transport(outgoing, remaining)
        except (OSError, TimeoutError) as error:
            raise ApiError(self.service, operation, None, "transport_error") from error
        if not 200 <= response.status < 300:
            raise ApiError(self.service, operation, response.status, "http_error")
        cookie = ""
        if isinstance(response.headers, Mapping):
            cookie = str(response.headers.get("Set-Cookie") or response.headers.get("set-cookie") or "")
        match = _SID.search(cookie)
        if match:
            self._sid = match.group(1)
        if not expect_json:
            return response.body
        if not response.body:
            return {}
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(self.service, operation, response.status, "invalid_response") from error
        if not isinstance(parsed, (dict, list)):
            raise ApiError(self.service, operation, response.status, "invalid_response")
        return parsed

    def login(self, username: str, password: str) -> bool:
        body = self._request(
            "POST",
            "/api/v2/auth/login",
            operation="login",
            form={"username": username, "password": password},
            expect_json=False,
        )
        text = body.decode("utf-8", errors="replace").strip() if isinstance(body, (bytes, bytearray)) else ""
        if text in {"Fails.", "Fails"}:
            return False
        # qBittorrent 5.x answers a successful login with 204 and no body;
        # older builds answer "Ok.". Both set the session cookie.
        return bool(self._sid) and (not text or text in {"Ok.", "Ok"})

    def preferences(self) -> dict[str, Any]:
        current = self._request("GET", "/api/v2/app/preferences", operation="read preferences")
        if not isinstance(current, dict):
            raise ApiError(self.service, "read preferences", None, "invalid_response")
        return current

    def set_preferences(self, updates: Mapping[str, Any]) -> None:
        self._request(
            "POST",
            "/api/v2/app/setPreferences",
            operation="set preferences",
            form={"json": json.dumps(dict(updates), separators=(",", ":"))},
            expect_json=False,
        )

    def categories(self) -> dict[str, Any]:
        current = self._request("GET", "/api/v2/torrents/categories", operation="list categories")
        if not isinstance(current, dict):
            raise ApiError(self.service, "list categories", None, "invalid_response")
        return current

    def _category_path(self, item: object) -> str | None:
        if not isinstance(item, dict):
            return None
        for key in ("savePath", "save_path"):
            value = item.get(key)
            if isinstance(value, str):
                return value
        return None

    def ensure_categories(self, desired: Mapping[str, str] | None = None) -> bool:
        wanted = dict(desired or CATEGORIES)
        current = self.categories()
        changed = False
        for name, save_path in wanted.items():
            existing = current.get(name)
            if existing is None:
                self._request(
                    "POST",
                    "/api/v2/torrents/createCategory",
                    operation="create category",
                    form={"category": name, "savePath": save_path},
                    expect_json=False,
                )
                changed = True
                continue
            if self._category_path(existing) != save_path:
                self._request(
                    "POST",
                    "/api/v2/torrents/editCategory",
                    operation="edit category",
                    form={"category": name, "savePath": save_path},
                    expect_json=False,
                )
                changed = True
        return changed

    def _incomplete_ok(self, prefs: Mapping[str, Any]) -> bool:
        if prefs.get("temp_path_enabled") is False:
            return True
        temp_path = prefs.get("temp_path")
        return isinstance(temp_path, str) and (
            temp_path == SAVE_PATH or temp_path.startswith(SAVE_PATH + "/")
        )

    def _categories_exact(self, current: Mapping[str, Any]) -> bool:
        for name, save_path in CATEGORIES.items():
            if self._category_path(current.get(name)) != save_path:
                return False
        return True

    def inspect(self, *, forwarded_port: int | None) -> dict[str, object]:
        prefs = self.preferences()
        categories = self.categories()
        listen = prefs.get("listen_port")
        port_agrees = type(forwarded_port) is int and type(listen) is int and listen == forwarded_port
        return {
            "save_path": prefs.get("save_path") == SAVE_PATH,
            "incomplete": self._incomplete_ok(prefs),
            "categories": self._categories_exact(categories),
            "bypass_local_auth": prefs.get("bypass_local_auth") is True,
            "port_agrees": port_agrees,
        }

    def configure(self, *, forwarded_port: int | None, password: str | None = None) -> dict[str, object]:
        prefs = self.preferences()
        updates: dict[str, Any] = {}
        if prefs.get("save_path") != SAVE_PATH:
            updates["save_path"] = SAVE_PATH
        if prefs.get("temp_path_enabled") is not False:
            updates["temp_path_enabled"] = False
        if prefs.get("bypass_local_auth") is not True:
            updates["bypass_local_auth"] = True
        if type(forwarded_port) is int and prefs.get("listen_port") != forwarded_port:
            updates["listen_port"] = forwarded_port
        if password:
            updates["web_ui_password"] = password
        if updates:
            self.set_preferences(updates)
        categories_changed = self.ensure_categories()
        inspected = self.inspect(forwarded_port=forwarded_port)
        return {
            "preferences_changed": bool(updates),
            "categories_changed": categories_changed,
            **inspected,
        }


def reconcile_webui_credential(
    client: QBittorrentClient,
    env_path: str | os.PathLike[str],
    runner: CommandRunner,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    """Login with the durable password if it works; otherwise consume temp/default once."""

    from ..envfile import EnvDocument

    document = EnvDocument.load(env_path)
    stored = document.get(QBITTORRENT_PASSWORD_KEY) or ""
    if stored and client.login(QBITTORRENT_USER, stored):
        return {"credential_updated": False}
    temporary = read_temporary_password(runner, timeout=timeout)
    accepted = None
    if temporary and client.login(QBITTORRENT_USER, temporary):
        accepted = temporary
    elif client.login(QBITTORRENT_USER, DEFAULT_PASSWORD):
        accepted = DEFAULT_PASSWORD
    if accepted is None:
        raise ApiError("qbittorrent", "login", None, "authentication_failed")
    generated = secrets.token_urlsafe(32)
    client.set_preferences({"web_ui_password": generated})
    if not client.login(QBITTORRENT_USER, generated):
        raise ApiError("qbittorrent", "rotate password", None, "authentication_failed")
    update_env(env_path, {QBITTORRENT_PASSWORD_KEY: generated}, {QBITTORRENT_PASSWORD_KEY})
    Path(env_path).chmod(0o600)
    return {"credential_updated": True}
