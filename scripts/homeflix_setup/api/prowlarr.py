"""Prowlarr application reconciliation. Does not invent indexer credentials."""

from __future__ import annotations

from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .arr import read_api_key
from .client import ApiError, JsonClient, Transport, urllib_transport


_OWNED = {
    "Radarr": {
        "implementation": "Radarr",
        "configContract": "RadarrSettings",
        "name": "Radarr",
        "base_service": "radarr",
        "base_port": 7878,
    },
    "Sonarr": {
        "implementation": "Sonarr",
        "configContract": "SonarrSettings",
        "name": "Sonarr",
        "base_service": "sonarr",
        "base_port": 8989,
    },
}


def _field_value(item: Mapping[str, Any], name: str) -> Any:
    fields = item.get("fields")
    if not isinstance(fields, list):
        return None
    matches = [field for field in fields if isinstance(field, dict) and field.get("name") == name]
    if len(matches) != 1:
        return None
    return matches[0].get("value")


def _set_field(fields: list[dict[str, Any]], name: str, value: Any) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    found = False
    for field in fields:
        if isinstance(field, dict) and field.get("name") == name:
            entry = dict(field)
            entry["value"] = value
            updated.append(entry)
            found = True
        elif isinstance(field, dict):
            updated.append(dict(field))
    if not found:
        updated.append({"name": name, "value": value})
    return updated


class ProwlarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: Transport = urllib_transport,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        headers = {"X-Api-Key": api_key}
        kwargs: dict[str, Any] = {"headers": headers, "transport": transport, "deadline": deadline}
        if clock is not None:
            kwargs["clock"] = clock
        self.http = JsonClient("prowlarr", base_url, **kwargs)

    def _list_applications(self, operation: str) -> list[dict[str, Any]]:
        current = self.http.request("GET", "/api/v1/applications", operation=operation)
        if not isinstance(current, list) or not all(isinstance(item, dict) for item in current):
            raise ApiError("prowlarr", operation, None, "invalid_response")
        return current

    def _owned_matches(self, items: list[dict[str, Any]], implementation: str) -> list[dict[str, Any]]:
        return [item for item in items if item.get("implementation") == implementation]

    def _desired_urls(self, implementation: str, prowlarr_port: int) -> tuple[str, str]:
        spec = _OWNED[implementation]
        return (
            f"http://gluetun:{prowlarr_port}",
            f"http://{spec['base_service']}:{spec['base_port']}",
        )

    def _application_exact(self, item: Mapping[str, Any], implementation: str, prowlarr_port: int, api_key: str) -> bool:
        prowlarr_url, base_url = self._desired_urls(implementation, prowlarr_port)
        return (
            item.get("implementation") == implementation
            and _field_value(item, "prowlarrUrl") == prowlarr_url
            and _field_value(item, "baseUrl") == base_url
            and isinstance(_field_value(item, "apiKey"), str)
            and bool(_field_value(item, "apiKey"))
            and (api_key == "" or _field_value(item, "apiKey") == api_key)
        )

    def inspect(self, *, prowlarr_port: int) -> dict[str, object]:
        apps = self._list_applications("inspect applications")
        indexers = self.http.request("GET", "/api/v1/indexer", operation="inspect indexers")
        if not isinstance(indexers, list):
            raise ApiError("prowlarr", "inspect indexers", None, "invalid_response")
        usable = any(isinstance(item, dict) and item.get("enable") is True for item in indexers)
        result: dict[str, object] = {
            "indexer_credentials": usable,
            "indexer_reason": "usable indexer present" if usable else "provider/indexer credentials required",
        }
        for implementation in _OWNED:
            matches = self._owned_matches(apps, implementation)
            key = implementation.casefold() + "_application"
            result[key] = len(matches) == 1 and self._application_exact(matches[0], implementation, prowlarr_port, "")
            result[implementation.casefold() + "_count"] = len(matches)
        return result

    def ensure_applications(
        self,
        *,
        prowlarr_port: int,
        arr_keys: Mapping[str, str],
    ) -> dict[str, object]:
        apps = self._list_applications("list applications")
        changed = {"radarr": False, "sonarr": False}
        for implementation, spec in _OWNED.items():
            matches = self._owned_matches(apps, implementation)
            if len(matches) > 1:
                raise ApiError("prowlarr", "reconcile applications", None, "application_conflict")
            service = spec["base_service"]
            api_key = arr_keys[service]
            prowlarr_url, base_url = self._desired_urls(implementation, prowlarr_port)
            if matches:
                current = matches[0]
                if self._application_exact(current, implementation, prowlarr_port, api_key):
                    continue
                if type(current.get("id")) is not int:
                    raise ApiError("prowlarr", "reconcile applications", None, "invalid_response")
                fields = list(current.get("fields") or [])
                if not all(isinstance(field, dict) for field in fields):
                    fields = []
                payload = dict(current)
                payload["fields"] = _set_field(_set_field(_set_field(fields, "prowlarrUrl", prowlarr_url), "baseUrl", base_url), "apiKey", api_key)
                payload["syncLevel"] = current.get("syncLevel") or "fullSync"
                self.http.request(
                    "PUT",
                    f"/api/v1/applications/{current['id']}",
                    operation="update application",
                    payload=payload,
                )
                changed[service] = True
                continue
            payload = {
                "name": spec["name"],
                "syncLevel": "fullSync",
                "implementation": spec["implementation"],
                "configContract": spec["configContract"],
                "fields": [
                    {"name": "prowlarrUrl", "value": prowlarr_url},
                    {"name": "baseUrl", "value": base_url},
                    {"name": "apiKey", "value": api_key},
                ],
            }
            created = self.http.request("POST", "/api/v1/applications", operation="create application", payload=payload)
            if not isinstance(created, dict) or type(created.get("id")) is not int:
                raise ApiError("prowlarr", "create application", None, "invalid_response")
            changed[service] = True
        inspected = self.inspect(prowlarr_port=prowlarr_port)
        return {
            "radarr_changed": changed["radarr"],
            "sonarr_changed": changed["sonarr"],
            **inspected,
        }
