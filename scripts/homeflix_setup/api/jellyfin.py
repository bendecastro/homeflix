"""Jellyfin startup and library reconciliation."""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlencode

from .client import ApiError, JsonClient, Transport, urllib_transport


LIBRARIES = {"Movies": ("movies", "/data/media/movies"), "Shows": ("tvshows", "/data/media/tv"), "Music": ("music", "/data/media/music")}


class JellyfinClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8096", *, transport: Transport = urllib_transport) -> None:
        self.http = JsonClient("jellyfin", base_url, transport=transport)
        self.token: str | None = None

    def startup_completed(self) -> bool:
        public = self.http.request("GET", "/System/Info/Public", operation="read startup state")
        if not isinstance(public, dict) or type(public.get("StartupWizardCompleted")) is not bool:
            raise ApiError("jellyfin", "read startup state", None, "invalid_response")
        return public["StartupWizardCompleted"]

    def authenticate(self, username: str, password: str) -> None:
        result = self.http.request(
            "POST", "/Users/AuthenticateByName", operation="authenticate administrator",
            payload={"Username": username, "Pw": password},
            headers={"Authorization": 'MediaBrowser Client="Homeflix Setup", Device="Setup", DeviceId="homeflix-setup", Version="1"'},
        )
        token = result.get("AccessToken") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ApiError("jellyfin", "authenticate administrator", None, "invalid_response")
        self.token = token

    def initialize(self, username: str, password: str) -> bool:
        if self.startup_completed():
            self.authenticate(username, password)
            return False
        administrator_exists = False
        try:
            self.authenticate(username, password)
            administrator_exists = True
        except ApiError as caught:
            if caught.status not in {400, 401, 404}:
                raise
        self.http.request("POST", "/Startup/Configuration", operation="set startup configuration", payload={
            "UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en",
        })
        if not administrator_exists:
            self.http.request("POST", "/Startup/User", operation="create administrator", payload={"Name": username, "Password": password})
        self.http.request("POST", "/Startup/RemoteAccess", operation="set remote access", payload={"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        self.http.request("POST", "/Startup/Complete", operation="complete startup", payload={})
        if not administrator_exists:
            self.authenticate(username, password)
        return not administrator_exists

    def _headers(self) -> Mapping[str, str]:
        if self.token is None:
            raise ApiError("jellyfin", "authorized request", None, "authentication_required")
        return {"X-Emby-Token": self.token}

    def ensure_libraries(self) -> list[str]:
        existing = self.http.request("GET", "/Library/VirtualFolders", operation="list libraries", headers=self._headers())
        if not isinstance(existing, list):
            raise ApiError("jellyfin", "list libraries", None, "invalid_response")
        by_name: dict[str, list[str]] = {}
        for item in existing:
            if not isinstance(item, dict) or not isinstance(item.get("Name"), str) or not isinstance(item.get("Locations", []), list):
                raise ApiError("jellyfin", "list libraries", None, "invalid_response")
            locations = item.get("Locations", [])
            if not all(isinstance(value, str) for value in locations):
                raise ApiError("jellyfin", "list libraries", None, "invalid_response")
            if item["Name"] in by_name and by_name[item["Name"]] != locations:
                raise ApiError("jellyfin", "reconcile libraries", None, "library_conflict")
            by_name[item["Name"]] = locations
        for name, (collection_type, path) in LIBRARIES.items():
            if name in by_name:
                if by_name[name] != [path]:
                    raise ApiError("jellyfin", "reconcile libraries", None, "library_conflict")
                continue
            query = urlencode({"name": name, "collectionType": collection_type, "paths": path, "refreshLibrary": "false"})
            try:
                self.http.request("POST", f"/Library/VirtualFolders?{query}", operation="create library", payload={}, headers=self._headers())
            except ApiError as caught:
                if caught.code != "transport_error":
                    raise
                current = self.http.request("GET", "/Library/VirtualFolders", operation="reconcile library creation", headers=self._headers())
                if not any(isinstance(item, dict) and item.get("Name") == name and item.get("Locations") == [path] for item in current):
                    raise
        return list(LIBRARIES)
