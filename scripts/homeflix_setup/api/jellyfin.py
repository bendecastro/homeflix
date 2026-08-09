"""Jellyfin startup and library reconciliation."""

from __future__ import annotations

import posixpath
import time
from typing import Callable, Mapping
from urllib.parse import urlencode

from .client import ApiError, JsonClient, Transport, urllib_transport


LIBRARIES = {"Movies": ("movies", "/data/media/movies"), "Shows": ("tvshows", "/data/media/tv"), "Music": ("music", "/data/media/music")}


class JellyfinClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8096", *, transport: Transport = urllib_transport, deadline: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.http = JsonClient("jellyfin", base_url, transport=transport, deadline=deadline, clock=clock)
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

    @staticmethod
    def _library_equivalent(item: object, name: str, collection_type: str, path: str) -> bool:
        if not isinstance(item, dict) or item.get("Name") != name or item.get("CollectionType") != collection_type:
            return False
        locations = item.get("Locations")
        if not isinstance(locations, list) or len(locations) != 1:
            return False
        if not all(isinstance(value, str) and value.startswith("/") for value in locations):
            return False
        normalized = {posixpath.normpath(value) for value in locations}
        return normalized == {posixpath.normpath(path)}

    def logout(self) -> None:
        """Close the ephemeral verification session and always forget its token."""
        if self.token is None:
            return
        try:
            self.http.request(
                "POST", "/Sessions/Logout", operation="close verification session",
                payload={}, headers=self._headers(),
            )
        finally:
            self.token = None

    def inspect(self, username: str, password: str) -> dict[str, object]:
        """Authenticate ephemerally, inspect with GET, then close the session."""
        if not self.startup_completed():
            return {"initialized": False, "libraries_exact": False}
        self.authenticate(username, password)
        try:
            existing = self.http.request(
                "GET", "/Library/VirtualFolders", operation="inspect libraries", headers=self._headers()
            )
            if not isinstance(existing, list):
                raise ApiError("jellyfin", "inspect libraries", None, "invalid_response")
            exact = len(existing) == len(LIBRARIES) and all(
                sum(1 for item in existing if self._library_equivalent(item, name, kind, path)) == 1
                for name, (kind, path) in LIBRARIES.items()
            )
        except Exception:
            try:
                self.logout()
            except Exception:
                pass
            raise
        self.logout()
        return {"initialized": True, "libraries_exact": exact}

    def ensure_libraries(self) -> list[str]:
        existing = self.http.request("GET", "/Library/VirtualFolders", operation="list libraries", headers=self._headers())
        if not isinstance(existing, list) or not all(isinstance(item, dict) and isinstance(item.get("Name"), str) for item in existing):
            raise ApiError("jellyfin", "list libraries", None, "invalid_response")

        missing: list[tuple[str, str, str]] = []
        for name, (collection_type, path) in LIBRARIES.items():
            matches = [item for item in existing if item.get("Name") == name]
            if not matches:
                missing.append((name, collection_type, path))
                continue
            if len(matches) != 1 or not self._library_equivalent(matches[0], name, collection_type, path):
                raise ApiError("jellyfin", "reconcile libraries", None, "library_conflict")

        for name, collection_type, path in missing:
            query = urlencode({"name": name, "collectionType": collection_type, "paths": path, "refreshLibrary": "false"})
            try:
                self.http.request("POST", f"/Library/VirtualFolders?{query}", operation="create library", payload={}, headers=self._headers())
            except ApiError as caught:
                if caught.code != "transport_error":
                    raise
                current = self.http.request("GET", "/Library/VirtualFolders", operation="reconcile library creation", headers=self._headers())
                matches = [item for item in current if isinstance(item, dict) and item.get("Name") == name] if isinstance(current, list) else []
                if len(matches) != 1 or not self._library_equivalent(matches[0], name, collection_type, path):
                    raise
        return list(LIBRARIES)
