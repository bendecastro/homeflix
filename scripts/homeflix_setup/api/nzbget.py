"""NZBGet JSON-RPC adapter. Control credentials stay out of returned payloads."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Mapping
from urllib import request
from urllib.parse import urlsplit

from ..envfile import EnvDocument, update_env
from .client import ApiError, Transport, urllib_transport


NZBGET_USER_KEY = "NZBGET_USER"
NZBGET_PASSWORD_KEY = "NZBGET_PASSWORD"
DEFAULT_USER = "nzbget"
LINUXSERVER_DEFAULT_PASSWORD = "tegbzn6789"
DEST_DIR = "/data/usenet/complete"
INTER_DIR = "/data/usenet/incomplete"
CATEGORIES = {
    "movies": "/data/usenet/complete/movies",
    "tv": "/data/usenet/complete/tv",
    "music": "/data/usenet/complete/music",
}


def _loopback_base(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("API base must be a plain-HTTP loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("API base contains unsupported components")
    return base_url.rstrip("/") + "/"


class NzbgetClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport = urllib_transport,
        username: str = DEFAULT_USER,
        password: str = "",
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 5.0,
    ) -> None:
        self.service = "nzbget"
        self.base_url = _loopback_base(base_url)
        self.transport = transport
        self.username = username
        self.password = password
        self.deadline = deadline
        self.clock = clock
        self.timeout = min(max(float(timeout), 0.1), 15.0)

    def _call(self, method: str, params: list[object], *, operation: str) -> Any:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        payload = {"version": "1.1", "method": method, "params": params}
        outgoing = request.Request(
            self.base_url.rstrip("/") + "/jsonrpc",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Basic {token}",
            },
            method="POST",
        )
        remaining = self.timeout if self.deadline is None else min(self.timeout, self.deadline - self.clock())
        if remaining <= 0:
            raise ApiError(self.service, operation, None, "deadline_exhausted")
        try:
            response = self.transport(outgoing, remaining)
        except (OSError, TimeoutError) as error:
            raise ApiError(self.service, operation, None, "transport_error") from error
        if response.status == 401:
            raise ApiError(self.service, operation, 401, "authentication_failed")
        if not 200 <= response.status < 300:
            raise ApiError(self.service, operation, response.status, "http_error")
        if not response.body:
            raise ApiError(self.service, operation, response.status, "invalid_response")
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(self.service, operation, response.status, "invalid_response") from error
        if not isinstance(parsed, dict) or "error" in parsed:
            raise ApiError(self.service, operation, response.status, "invalid_response")
        return parsed.get("result")

    def login(self, username: str, password: str) -> bool:
        previous = (self.username, self.password)
        self.username = username
        self.password = password
        try:
            self.config()
            return True
        except ApiError as error:
            self.username, self.password = previous
            if error.code == "authentication_failed":
                return False
            raise

    def _option_map(self, result: Any, operation: str) -> dict[str, str]:
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise ApiError(self.service, operation, None, "invalid_response")
        options: dict[str, str] = {}
        for item in result:
            name = item.get("Name")
            value = item.get("Value")
            if isinstance(name, str) and isinstance(value, str):
                options[name] = value
        return options

    def config(self) -> dict[str, str]:
        return self._option_map(self._call("config", [], operation="read config"), "read config")

    def loadconfig(self) -> dict[str, str]:
        return self._option_map(self._call("loadconfig", [], operation="load config"), "load config")

    def save_options(self, updates: Mapping[str, str]) -> None:
        current = self.loadconfig()
        current.update({key: str(value) for key, value in updates.items()})
        payload = [{"Name": name, "Value": value} for name, value in current.items()]
        result = self._call("saveconfig", [payload], operation="save config")
        if result is not True:
            raise ApiError(self.service, "save config", None, "invalid_response")

    def reload(self) -> None:
        result = self._call("reload", [], operation="reload")
        if result is not True:
            raise ApiError(self.service, "reload", None, "invalid_response")

    def _category_updates(self, options: Mapping[str, str]) -> dict[str, str]:
        slots: dict[str, str] = {}
        used: set[int] = set()
        for key, value in options.items():
            if not key.startswith("Category") or not key.endswith(".Name"):
                continue
            prefix = key[: -len(".Name")]
            index_text = prefix[len("Category") :]
            if index_text.isdigit():
                used.add(int(index_text))
            slots[value.strip().casefold()] = prefix
        updates: dict[str, str] = {}
        next_index = 1
        for name, dest in CATEGORIES.items():
            prefix = slots.get(name)
            if prefix is None:
                while next_index in used:
                    next_index += 1
                prefix = f"Category{next_index}"
                used.add(next_index)
                next_index += 1
                updates[f"{prefix}.Name"] = name
            elif options.get(f"{prefix}.Name") != name:
                updates[f"{prefix}.Name"] = name
            if options.get(f"{prefix}.DestDir") != dest:
                updates[f"{prefix}.DestDir"] = dest
        return updates

    def _disable_servers(self, options: Mapping[str, str]) -> dict[str, str]:
        return {
            key: "no"
            for key, value in options.items()
            if key.startswith("Server") and key.endswith(".Active") and value.casefold() != "no"
        }

    def inspect(self) -> dict[str, object]:
        options = self.config()
        category_ok = True
        names = {
            options[key].strip().casefold()
            for key in options
            if key.startswith("Category") and key.endswith(".Name")
        }
        dests = {options[key] for key in options if key.startswith("Category") and key.endswith(".DestDir")}
        for name, dest in CATEGORIES.items():
            if name not in names or dest not in dests:
                category_ok = False
        news = False
        for key, value in options.items():
            if not (key.startswith("Server") and key.endswith(".Active") and value.casefold() == "yes"):
                continue
            prefix = key[: -len(".Active")]
            host = options.get(f"{prefix}.Host") or ""
            user = options.get(f"{prefix}.Username") or ""
            password = options.get(f"{prefix}.Password") or ""
            if host and user and password:
                news = True
                break
        return {
            "paths": options.get("InterDir") == INTER_DIR and options.get("DestDir") == DEST_DIR,
            "categories": category_ok,
            "news_servers": news,
        }

    def configure(self, *, news_server: Mapping[str, str] | None = None) -> dict[str, object]:
        options = self.config()
        updates: dict[str, str] = {}
        if options.get("InterDir") != INTER_DIR:
            updates["InterDir"] = INTER_DIR
        if options.get("DestDir") != DEST_DIR:
            updates["DestDir"] = DEST_DIR
        updates.update(self._category_updates(options))
        if news_server:
            updates.update(self._news_server_updates(options, news_server))
        else:
            updates.update(self._disable_servers(options))
        if updates:
            self.save_options(updates)
            self.reload()
        inspected = self.inspect()
        return {"changed": bool(updates), **inspected}

    def _news_server_updates(self, options: Mapping[str, str], news_server: Mapping[str, str]) -> dict[str, str]:
        prefix = "Server1"
        return {
            f"{prefix}.Host": news_server["host"],
            f"{prefix}.Port": str(news_server.get("port") or "563"),
            f"{prefix}.Username": news_server["username"],
            f"{prefix}.Password": news_server["password"],
            f"{prefix}.Encryption": news_server.get("encryption") or "yes",
            f"{prefix}.Connections": str(news_server.get("connections") or "8"),
            f"{prefix}.Active": "yes",
            f"{prefix}.Name": news_server.get("name") or "news",
        }


def reconcile_control_credential(
    client: NzbgetClient,
    env_path: str | os.PathLike[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] | None = None,
    login_timeout: float = 5.0,
) -> dict[str, object]:
    """Login with the durable Control user if it works; otherwise rotate the linuxserver default."""

    tick = clock or client.clock
    document = EnvDocument.load(env_path) if Path(env_path).exists() else EnvDocument([])
    user = document.get(NZBGET_USER_KEY) or DEFAULT_USER
    stored = document.get(NZBGET_PASSWORD_KEY) or ""
    if stored and client.login(user, stored):
        return {"credential_updated": False}
    if not client.login(DEFAULT_USER, LINUXSERVER_DEFAULT_PASSWORD):
        raise ApiError("nzbget", "login", None, "authentication_failed")
    generated = secrets.token_urlsafe(32)
    client.save_options({"ControlUsername": user, "ControlPassword": generated})
    update_env(env_path, {NZBGET_USER_KEY: user, NZBGET_PASSWORD_KEY: generated}, {NZBGET_PASSWORD_KEY})
    Path(env_path).chmod(0o600)
    client.reload()
    deadline = tick() + max(0.0, float(login_timeout))
    if client.deadline is not None:
        deadline = min(deadline, client.deadline)
    attempts = 8
    for attempt in range(attempts):
        try:
            ready = client.login(user, generated)
        except ApiError as error:
            if error.code not in {"authentication_failed", "transport_error"}:
                raise
            ready = False
        if ready:
            return {"credential_updated": True}
        remaining = deadline - tick()
        if remaining <= 0 or attempt == attempts - 1:
            raise ApiError("nzbget", "rotate password", None, "authentication_failed")
        sleep(min(0.25 * (2 ** attempt), 1.0, remaining))
    raise ApiError("nzbget", "rotate password", None, "authentication_failed")
