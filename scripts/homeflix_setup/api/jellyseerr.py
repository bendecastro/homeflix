"""Jellyseerr initialization and service reconciliation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .client import ApiError, JsonClient, Transport, urllib_transport


_KEY = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_INTERNAL = {"jellyfin": 8096, "radarr": 7878, "sonarr": 8989}


def read_settings_api_key(path: str | Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ValueError("Jellyseerr settings cannot be opened safely") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & stat.S_IROTH or metadata.st_size > 1024 * 1024:
            raise ValueError("Jellyseerr settings permissions are unsafe")
        raw = b""
        while chunk := os.read(descriptor, 65536):
            raw += chunk
            if len(raw) > 1024 * 1024:
                raise ValueError("Jellyseerr settings are too large")
    finally:
        os.close(descriptor)
    try:
        settings = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Jellyseerr settings are invalid") from None
    value = settings.get("main", {}).get("apiKey") if isinstance(settings, dict) and isinstance(settings.get("main"), dict) else None
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("Jellyseerr API key is invalid")
    return value


class JellyseerrClient:
    def __init__(self, base_url: str = "http://127.0.0.1", *, headers: Mapping[str, str] | None = None, transport: Transport = urllib_transport) -> None:
        self.http = JsonClient("jellyseerr", base_url, headers=headers, transport=transport)

    def authenticate_jellyfin(self, username: str, password: str) -> None:
        self.http.request("POST", "/api/v1/auth/jellyfin", operation="connect Jellyfin", payload={
            "username": username, "password": password, "hostname": "jellyfin", "port": 8096,
            "urlBase": "", "useSsl": False, "email": "", "serverType": 2,
        })

    def authorize(self, api_key: str) -> None:
        if not _KEY.fullmatch(api_key):
            raise ValueError("invalid Jellyseerr API key")
        self.http.headers["X-Api-Key"] = api_key

    def initialized(self) -> bool:
        public = self.http.request("GET", "/api/v1/settings/public", operation="read initialization state")
        if not isinstance(public, dict):
            raise ApiError("jellyseerr", "read initialization state", None, "invalid_response")
        value = public.get("initialized", public.get("setupComplete"))
        if type(value) is not bool:
            raise ApiError("jellyseerr", "read initialization state", None, "invalid_response")
        return value

    def _payload(self, service: str, api_key: str, profile: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
        if service not in {"radarr", "sonarr"} or not _KEY.fullmatch(api_key):
            raise ValueError("invalid internal service configuration")
        payload: dict[str, Any] = {
            "name": service.capitalize(), "hostname": service, "port": _INTERNAL[service], "apiKey": api_key,
            "useSsl": False, "baseUrl": "", "activeProfileId": profile["id"], "activeProfileName": profile["name"],
            "activeDirectory": root["path"], "is4k": False, "minimumAvailability": "released", "isDefault": True,
            "externalUrl": "", "syncEnabled": True, "preventSearch": False, "tags": [],
        }
        if service == "sonarr":
            payload.update({"enableSeasonFolders": True, "animeTags": []})
        return payload

    def ensure_arr(self, service: str, api_key: str, profile: dict[str, Any], root: dict[str, Any]) -> bool:
        desired = self._payload(service, api_key, profile, root)
        test = self.http.request("POST", f"/api/v1/settings/{service}/test", operation=f"test {service} connection", payload=desired)
        if not isinstance(test, dict) or not (test.get("success") is True or test.get("status") == 200):
            raise ApiError("jellyseerr", f"test {service} connection", None, "connection_failed")
        current = self.http.request("GET", f"/api/v1/settings/{service}", operation=f"list {service} settings")
        if not isinstance(current, list):
            raise ApiError("jellyseerr", f"list {service} settings", None, "invalid_response")
        defaults = [item for item in current if isinstance(item, dict) and item.get("isDefault") is True]
        if len(defaults) > 1:
            raise ApiError("jellyseerr", f"reconcile {service} settings", None, "multiple_defaults")
        candidates = [item for item in current if isinstance(item, dict) and item.get("hostname") == service and item.get("port") == _INTERNAL[service] and item.get("is4k") is False]
        if len(candidates) > 1 or (defaults and (not candidates or defaults[0] is not candidates[0])):
            raise ApiError("jellyseerr", f"reconcile {service} settings", None, "server_conflict")
        existing = candidates[0] if candidates else None
        if existing is None:
            try:
                self.http.request("POST", f"/api/v1/settings/{service}", operation=f"create {service} settings", payload=desired)
            except ApiError as caught:
                if caught.code != "transport_error":
                    raise
                reconciled = self.http.request("GET", f"/api/v1/settings/{service}", operation=f"reconcile {service} creation")
                matches = [item for item in reconciled if isinstance(item, dict) and all(item.get(key) == value for key, value in desired.items())]
                if len(matches) != 1:
                    raise
            return True
        if type(existing.get("id")) is not int:
            raise ApiError("jellyseerr", f"reconcile {service} settings", None, "invalid_response")
        owned = set(desired)
        if all(existing.get(key) == value for key, value in desired.items()):
            return False
        updated = dict(existing)
        updated.update({key: desired[key] for key in owned})
        self.http.request("PUT", f"/api/v1/settings/{service}/{existing['id']}", operation=f"update {service} settings", payload=updated)
        return True

    def finish(self) -> bool:
        if self.initialized():
            return False
        self.http.request("POST", "/api/v1/settings/initialize", operation="initialize settings", payload={})
        return True
