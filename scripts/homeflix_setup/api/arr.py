"""Radarr/Sonarr configuration reconciliation."""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .client import ApiError, JsonClient, Transport, urllib_transport
from .securepath import read_config_file


_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_JELLYFIN_HOST = "jellyfin"
_JELLYFIN_PORT = 8096
_REFRESH_URL = "http://jellyfin:8096/Library/Refresh"
_WEBHOOK_POST = 1
_TOKEN_HEADER = "X-Emby-Token"
_MEDIA_BROWSER = "MediaBrowser"
_WEBHOOK = "Webhook"
_QBITTORRENT = "QBittorrent"
_FORBIDDEN_DOWNLOAD_HOSTS = {"localhost", "127.0.0.1", "::1", "qbittorrent", "0.0.0.0"}

_RADARR_EVENTS = {
    "onGrab": False,
    "onDownload": True,
    "onUpgrade": True,
    "onRename": True,
    "onMovieAdded": False,
    "onMovieDelete": False,
    "onMovieFileDelete": False,
    "onMovieFileDeleteForUpgrade": False,
    "onHealthIssue": False,
    "includeHealthWarnings": False,
    "onHealthRestored": False,
    "onApplicationUpdate": False,
    "onManualInteractionRequired": False,
}
_SONARR_EVENTS = {
    "onGrab": False,
    "onDownload": False,
    "onUpgrade": False,
    "onImportComplete": True,
    "onRename": True,
    "onSeriesAdd": False,
    "onSeriesDelete": False,
    "onEpisodeFileDelete": False,
    "onEpisodeFileDeleteForUpgrade": False,
    "onHealthIssue": False,
    "includeHealthWarnings": False,
    "onHealthRestored": False,
    "onApplicationUpdate": False,
    "onManualInteractionRequired": False,
}


def _owned_events(service: str) -> dict[str, bool]:
    return dict(_RADARR_EVENTS if service == "radarr" else _SONARR_EVENTS)


def _field_value(item: Mapping[str, Any], name: str) -> Any:
    fields = item.get("fields")
    if not isinstance(fields, list):
        return None
    matches = [field for field in fields if isinstance(field, dict) and field.get("name") == name]
    if len(matches) != 1:
        return None
    return matches[0].get("value")


def _header_pairs(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = entry.get("key", entry.get("name"))
        header_value = entry.get("value")
        if isinstance(name, str) and isinstance(header_value, str):
            pairs.append((name, header_value))
    return pairs


def _events_exact(item: Mapping[str, Any], service: str) -> bool:
    return all(item.get(name) is expected for name, expected in _owned_events(service).items())


def _media_browser_address_exact(item: Mapping[str, Any]) -> bool:
    url_base = _field_value(item, "urlBase")
    return (
        item.get("implementation") == _MEDIA_BROWSER
        and _field_value(item, "host") == _JELLYFIN_HOST
        and _field_value(item, "port") == _JELLYFIN_PORT
        and _field_value(item, "useSsl") is False
        and (url_base in {None, ""})
    )


def _refresh_url_identity(url: object) -> tuple[str, str, int, str] | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        return None
    port = parsed.port if parsed.port is not None else _JELLYFIN_PORT
    return (parsed.scheme, parsed.hostname, port, parsed.path)


def _webhook_owned_identity(item: Mapping[str, Any]) -> bool:
    return item.get("implementation") == _WEBHOOK and _refresh_url_identity(_field_value(item, "url")) == (
        "http",
        _JELLYFIN_HOST,
        _JELLYFIN_PORT,
        "/Library/Refresh",
    )


def _inspect_targeted(items: list[Mapping[str, Any]], service: str) -> dict[str, object]:
    matches = [item for item in items if _media_browser_address_exact(item)]
    empty = {
        "present": False,
        "events_exact": False,
        "address_exact": False,
        "update_library": False,
        "notify": False,
    }
    if len(matches) != 1:
        return empty
    item = matches[0]
    update_library = _field_value(item, "updateLibrary") is True
    notify = _field_value(item, "notify") is True
    events_exact = _events_exact(item, service)
    api_key = _field_value(item, "apiKey")
    key_present = isinstance(api_key, str) and bool(api_key)
    exact = events_exact and update_library and notify is False and key_present
    return {
        "present": True,
        "events_exact": events_exact,
        "address_exact": True,
        "update_library": update_library,
        "notify": notify,
        "exact": exact,
    }


def _inspect_refresh(items: list[Mapping[str, Any]], service: str) -> dict[str, object]:
    matches = [item for item in items if _webhook_owned_identity(item)]
    empty = {
        "present": False,
        "events_exact": False,
        "url_exact": False,
        "method_exact": False,
        "token_header": False,
    }
    if len(matches) != 1:
        return empty
    item = matches[0]
    url = _field_value(item, "url")
    url_exact = url == _REFRESH_URL
    method_exact = _field_value(item, "method") == _WEBHOOK_POST
    headers = _header_pairs(_field_value(item, "headers"))
    token_names = [name for name, value in headers if name == _TOKEN_HEADER and value]
    token_header = len(token_names) == 1
    events_exact = _events_exact(item, service)
    return {
        "present": True,
        "events_exact": events_exact,
        "url_exact": url_exact,
        "method_exact": method_exact,
        "token_header": token_header,
        "exact": events_exact and url_exact and method_exact and token_header,
    }


def read_api_key(config_root: str | Path, service: str, expected_uid: int) -> str:
    """Read one API key through verified CONFIG_ROOT and service components."""
    if service not in {"radarr", "sonarr", "prowlarr"}:
        raise ValueError("service API key location is invalid")
    raw = read_config_file(config_root, (service, "config.xml"), expected_uid)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError("service API key file is invalid") from None
    nodes = root.findall("./ApiKey")
    value = nodes[0].text.strip() if len(nodes) == 1 and nodes[0].text else ""
    if not _KEY.fullmatch(value):
        raise ValueError("service API key is invalid")
    return value


class ArrClient:
    def __init__(self, service: str, base_url: str, api_key: str, *, headers: Mapping[str, str] | None = None, transport: Transport = urllib_transport, deadline: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if service not in {"radarr", "sonarr"} or not _KEY.fullmatch(api_key):
            raise ValueError("invalid Arr client configuration")
        self.service = service
        self.http = JsonClient(service, base_url, headers={**dict(headers or {}), "X-Api-Key": api_key}, transport=transport, deadline=deadline, clock=clock)
        self.selected_profile: dict[str, Any] | None = None
        self.selected_root: dict[str, Any] | None = None

    def profile(self, name: str) -> dict[str, Any]:
        profiles = self.http.request("GET", "/api/v3/qualityprofile", operation="list quality profiles")
        if not isinstance(profiles, list):
            raise ApiError(self.service, "list quality profiles", None, "invalid_response")
        matches = [item for item in profiles if isinstance(item, dict) and item.get("name") == name and type(item.get("id")) is int]
        if len(matches) > 1:
            raise ApiError(self.service, "select quality profile", None, "profile_conflict")
        if not matches:
            raise ApiError(self.service, "select quality profile", None, "profile_not_found")
        return {"id": matches[0]["id"], "name": name}

    def ensure_root(self, path: str) -> dict[str, Any]:
        roots = self.http.request("GET", "/api/v3/rootfolder", operation="list root folders")
        if not isinstance(roots, list):
            raise ApiError(self.service, "list root folders", None, "invalid_response")
        matches = [item for item in roots if isinstance(item, dict) and item.get("path") == path and type(item.get("id")) is int]
        if len(matches) > 1:
            raise ApiError(self.service, "reconcile root folder", None, "root_conflict")
        if matches:
            return {"id": matches[0]["id"], "path": path}
        try:
            created = self.http.request("POST", "/api/v3/rootfolder", operation="create root folder", payload={"path": path})
        except ApiError as caught:
            if caught.code != "transport_error":
                raise
            current = self.http.request("GET", "/api/v3/rootfolder", operation="reconcile root creation")
            found = [item for item in current if isinstance(item, dict) and item.get("path") == path and type(item.get("id")) is int]
            if len(found) != 1:
                raise
            return {"id": found[0]["id"], "path": path}
        if not isinstance(created, dict) or type(created.get("id")) is not int or created.get("path") != path:
            raise ApiError(self.service, "create root folder", None, "invalid_response")
        return {"id": created["id"], "path": path}

    def _update_config(self, endpoint: str, operation: str, owned: dict[str, Any]) -> bool:
        current = self.http.request("GET", endpoint, operation=f"read {operation}")
        if not isinstance(current, dict) or type(current.get("id")) is not int:
            raise ApiError(self.service, f"read {operation}", None, "invalid_response")
        if all(current.get(key) == value for key, value in owned.items()):
            return False
        updated = dict(current)
        updated.update(owned)
        self.http.request("PUT", endpoint, operation=f"update {operation}", payload=updated)
        return True

    def _list_notifications(self, operation: str) -> list[dict[str, Any]]:
        current = self.http.request("GET", "/api/v3/notification", operation=operation)
        if not isinstance(current, list) or not all(isinstance(item, dict) for item in current):
            raise ApiError(self.service, operation, None, "invalid_response")
        return current

    def _notification_payload(self, implementation: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        if implementation == _MEDIA_BROWSER:
            name, contract = "Jellyfin", "MediaBrowserSettings"
        else:
            name, contract = "Jellyfin library scan", "WebhookSettings"
        payload: dict[str, Any] = {
            "name": name,
            "implementation": implementation,
            "configContract": contract,
            "fields": fields,
            **_owned_events(self.service),
        }
        return payload

    def _create_notification(self, implementation: str, fields: list[dict[str, Any]], owned_match: Callable[[Mapping[str, Any]], bool]) -> None:
        payload = self._notification_payload(implementation, fields)
        try:
            created = self.http.request("POST", "/api/v3/notification", operation="create notification", payload=payload)
        except ApiError as caught:
            if caught.code != "transport_error":
                raise
            current = self._list_notifications("reconcile notification creation")
            found = [item for item in current if owned_match(item)]
            if len(found) != 1:
                raise
            return
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ApiError(self.service, "create notification", None, "invalid_response")

    def _ensure_discovery(self, jellyfin_api_key: str) -> tuple[bool, bool]:
        if not re.fullmatch(r"^[A-Za-z0-9_-]{16,256}$", jellyfin_api_key):
            raise ValueError("Jellyfin API key is invalid")
        current = self._list_notifications("list notifications")
        targeted_matches = [item for item in current if _media_browser_address_exact(item)]
        refresh_matches = [item for item in current if _webhook_owned_identity(item)]
        if len(targeted_matches) > 1 or len(refresh_matches) > 1:
            raise ApiError(self.service, "reconcile notifications", None, "notification_conflict")
        if targeted_matches:
            inspected = _inspect_targeted(targeted_matches, self.service)
            if inspected.get("exact") is not True or _field_value(targeted_matches[0], "apiKey") != jellyfin_api_key:
                raise ApiError(self.service, "reconcile notifications", None, "notification_conflict")
        if refresh_matches:
            inspected = _inspect_refresh(refresh_matches, self.service)
            headers = _header_pairs(_field_value(refresh_matches[0], "headers"))
            token_values = [value for name, value in headers if name == _TOKEN_HEADER]
            if inspected.get("exact") is not True or token_values != [jellyfin_api_key]:
                raise ApiError(self.service, "reconcile notifications", None, "notification_conflict")
        targeted_fields = [
            {"name": "host", "value": _JELLYFIN_HOST},
            {"name": "port", "value": _JELLYFIN_PORT},
            {"name": "useSsl", "value": False},
            {"name": "urlBase", "value": ""},
            {"name": "apiKey", "value": jellyfin_api_key},
            {"name": "notify", "value": False},
            {"name": "updateLibrary", "value": True},
        ]
        refresh_fields = [
            {"name": "url", "value": _REFRESH_URL},
            {"name": "method", "value": _WEBHOOK_POST},
            {"name": "headers", "value": [{"key": _TOKEN_HEADER, "value": jellyfin_api_key}]},
        ]
        targeted_changed = False
        if not targeted_matches:
            self._create_notification(_MEDIA_BROWSER, targeted_fields, _media_browser_address_exact)
            targeted_changed = True
        refresh_changed = False
        if not refresh_matches:
            self._create_notification(_WEBHOOK, refresh_fields, _webhook_owned_identity)
            refresh_changed = True
        return targeted_changed, refresh_changed

    def _download_clients(self, operation: str) -> list[dict[str, Any]]:
        current = self.http.request("GET", "/api/v3/downloadclient", operation=operation)
        if not isinstance(current, list) or not all(isinstance(item, dict) for item in current):
            raise ApiError(self.service, operation, None, "invalid_response")
        return current

    def _category_field(self) -> str:
        return "movieCategory" if self.service == "radarr" else "tvCategory"

    def _desired_category(self) -> str:
        return "movies" if self.service == "radarr" else "tv"

    def _qbittorrent_matches(self, items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [item for item in items if item.get("implementation") == _QBITTORRENT]

    def inspect_download_client(self, *, host: str, port: int) -> dict[str, object]:
        matches = self._qbittorrent_matches(self._download_clients("inspect download clients"))
        empty = {
            "present": False,
            "exact": False,
            "host_exact": False,
            "port_exact": False,
            "category_exact": False,
            "count": len(matches),
        }
        if len(matches) != 1:
            return empty
        item = matches[0]
        host_exact = _field_value(item, "host") == host
        port_exact = _field_value(item, "port") == port
        category_exact = _field_value(item, self._category_field()) == self._desired_category()
        return {
            "present": True,
            "exact": host_exact and port_exact and category_exact,
            "host_exact": host_exact,
            "port_exact": port_exact,
            "category_exact": category_exact,
            "count": 1,
        }

    def ensure_qbittorrent_client(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        force_password: bool = False,
    ) -> bool:
        if not isinstance(host, str) or host.strip().casefold() in _FORBIDDEN_DOWNLOAD_HOSTS:
            raise ApiError(self.service, "reconcile download client", None, "invalid_download_client_host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ApiError(self.service, "reconcile download client", None, "invalid_download_client_port")
        current = self._download_clients("list download clients")
        matches = self._qbittorrent_matches(current)
        if len(matches) > 1:
            raise ApiError(self.service, "reconcile download client", None, "download_client_conflict")
        category_field = self._category_field()
        category = self._desired_category()
        owned_fields = [
            {"name": "host", "value": host},
            {"name": "port", "value": port},
            {"name": "useSsl", "value": False},
            {"name": "username", "value": username},
            {"name": "password", "value": password},
            {"name": category_field, "value": category},
        ]
        if matches:
            item = dict(matches[0])
            inspected = self.inspect_download_client(host=host, port=port)
            # qBittorrent password is write-only on the *arr API; inspect cannot
            # see it. After a WebUI rotation, force_password writes the new value.
            if (
                inspected.get("exact") is True
                and _field_value(item, "username") == username
                and not force_password
            ):
                return False
            if type(item.get("id")) is not int:
                raise ApiError(self.service, "reconcile download client", None, "invalid_response")
            fields = item.get("fields")
            field_list = [dict(field) for field in fields] if isinstance(fields, list) else []
            values = {field["name"]: field for field in field_list if isinstance(field, dict) and "name" in field}
            for field in owned_fields:
                values[field["name"]] = {**values.get(field["name"], {}), **field}
            item["fields"] = list(values.values())
            item["enable"] = True
            item["removeCompletedDownloads"] = False
            item["implementation"] = _QBITTORRENT
            item["configContract"] = "QBittorrentSettings"
            self.http.request(
                "PUT",
                f"/api/v3/downloadclient/{item['id']}",
                operation="update download client",
                payload=item,
            )
            return True
        payload = {
            "enable": True,
            "name": "qBittorrent",
            "implementation": _QBITTORRENT,
            "configContract": "QBittorrentSettings",
            "removeCompletedDownloads": False,
            "fields": owned_fields,
        }
        created = self.http.request("POST", "/api/v3/downloadclient", operation="create download client", payload=payload)
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ApiError(self.service, "create download client", None, "invalid_response")
        return True

    def inspect(self, profile_name: str, root_path: str) -> dict[str, object]:
        """Inspect setup-owned Arr state using GET requests only."""
        profile = self.profile(profile_name)
        roots = self.http.request("GET", "/api/v3/rootfolder", operation="inspect root folders")
        root_matches = [
            item for item in roots
            if isinstance(item, dict) and item.get("path") == root_path and type(item.get("id")) is int
        ] if isinstance(roots, list) else []
        naming = self.http.request("GET", "/api/v3/config/naming", operation="inspect naming")
        media = self.http.request("GET", "/api/v3/config/mediamanagement", operation="inspect media management")
        completed = self.http.request("GET", "/api/v3/config/downloadclient", operation="inspect completed handling")
        notifications = self.http.request("GET", "/api/v3/notification", operation="inspect notifications")
        if not isinstance(notifications, list) or not all(isinstance(item, dict) for item in notifications):
            raise ApiError(self.service, "inspect notifications", None, "invalid_response")
        targeted = _inspect_targeted(notifications, self.service)
        refresh = _inspect_refresh(notifications, self.service)
        rename = "renameMovies" if self.service == "radarr" else "renameEpisodes"
        return {
            "profile": profile["name"],
            "profile_exact": True,
            "root_exact": len(root_matches) == 1,
            "media_settings": (
                isinstance(naming, dict) and naming.get(rename) is True
                and isinstance(media, dict) and media.get("copyUsingHardlinks") is True
            ),
            "completed_handling": isinstance(completed, dict) and completed.get("enableCompletedDownloadHandling") is True,
            "targeted_connection_exact": targeted.get("exact") is True,
            "refresh_connection_exact": refresh.get("exact") is True,
            "targeted_connection": {key: targeted[key] for key in ("present", "events_exact", "address_exact", "update_library", "notify")},
            "refresh_connection": {key: refresh[key] for key in ("present", "events_exact", "url_exact", "method_exact", "token_header")},
            "runtime_profile": profile,
            "runtime_root": ({"id": root_matches[0]["id"], "path": root_path} if len(root_matches) == 1 else None),
        }

    def configure(self, profile_name: str, root_path: str, *, jellyfin_api_key: str | None = None) -> dict[str, object]:
        profile = self.profile(profile_name)
        root = self.ensure_root(root_path)
        self.selected_profile = profile
        self.selected_root = root
        # renameMovies/renameEpisodes belong to config/naming. Sending them to
        # config/mediamanagement is accepted but silently dropped, leaving
        # renaming off while the rest of the payload applies.
        rename_field = "renameMovies" if self.service == "radarr" else "renameEpisodes"
        naming_changed = self._update_config("/api/v3/config/naming", "naming", {
            rename_field: True,
        })
        media_changed = self._update_config("/api/v3/config/mediamanagement", "media management", {
            "copyUsingHardlinks": True,
        })
        completed_changed = self._update_config("/api/v3/config/downloadclient", "completed download handling", {
            "enableCompletedDownloadHandling": True,
        })
        targeted_changed = False
        refresh_changed = False
        if jellyfin_api_key is not None:
            targeted_changed, refresh_changed = self._ensure_discovery(jellyfin_api_key)
        return {
            "service": self.service,
            "profile": profile["name"],
            "root": root["path"],
            "naming_changed": naming_changed,
            "media_management_changed": naming_changed or media_changed,
            "completed_handling_changed": completed_changed,
            "targeted_connection_changed": targeted_changed,
            "refresh_connection_changed": refresh_changed,
        }
