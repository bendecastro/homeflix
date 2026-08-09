"""Radarr/Sonarr configuration reconciliation."""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET

from .client import ApiError, JsonClient, Transport, urllib_transport
from .securepath import read_config_file


_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def read_api_key(config_root: str | Path, service: str, expected_uid: int) -> str:
    """Read one API key through verified CONFIG_ROOT and service components."""
    if service not in {"radarr", "sonarr"}:
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

    def inspect(self, profile_name: str, root_path: str) -> dict[str, object]:
        """Inspect setup-owned Arr state using GET requests only."""
        profile = self.profile(profile_name)
        roots = self.http.request("GET", "/api/v3/rootfolder", operation="inspect root folders")
        root_matches = [
            item for item in roots
            if isinstance(item, dict) and item.get("path") == root_path and type(item.get("id")) is int
        ] if isinstance(roots, list) else []
        media = self.http.request("GET", "/api/v3/config/mediamanagement", operation="inspect media management")
        completed = self.http.request("GET", "/api/v3/config/downloadclient", operation="inspect completed handling")
        rename = "renameMovies" if self.service == "radarr" else "renameEpisodes"
        return {
            "profile": profile["name"],
            "profile_exact": True,
            "root_exact": len(root_matches) == 1,
            "media_settings": isinstance(media, dict) and media.get(rename) is True and media.get("copyUsingHardlinks") is True,
            "completed_handling": isinstance(completed, dict) and completed.get("enableCompletedDownloadHandling") is True,
            "runtime_profile": profile,
            "runtime_root": ({"id": root_matches[0]["id"], "path": root_path} if len(root_matches) == 1 else None),
        }

    def configure(self, profile_name: str, root_path: str) -> dict[str, object]:
        profile = self.profile(profile_name)
        root = self.ensure_root(root_path)
        self.selected_profile = profile
        self.selected_root = root
        rename_field = "renameMovies" if self.service == "radarr" else "renameEpisodes"
        media_changed = self._update_config("/api/v3/config/mediamanagement", "media management", {
            rename_field: True, "copyUsingHardlinks": True,
        })
        completed_changed = self._update_config("/api/v3/config/downloadclient", "completed download handling", {
            "enableCompletedDownloadHandling": True,
        })
        return {"service": self.service, "profile": profile["name"], "root": root["path"], "media_management_changed": media_changed, "completed_handling_changed": completed_changed}
