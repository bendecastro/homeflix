"""Adding titles to an existing Radarr/Sonarr library by external id.

Deliberately separate from `arr.py`: that module reconciles setup-owned
configuration, whereas adding titles is acquisition and is never part of core
setup. Callers must opt in explicitly.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from .client import ApiError, JsonClient, Transport, urllib_transport


# Sonarr applies `addOptions.monitor` from a refresh task that finishes after the
# POST returns, so it can revert monitoring written immediately afterwards. The
# desired state must therefore be observed twice, across a delay, before it is
# trusted. See docs/media-library.md.
SETTLE_ATTEMPTS = 6
SETTLE_DELAY = 2.0


def _identifier(value: Any) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise ValueError("external id must be a positive integer")
    return value


def _season_number(value: Any) -> int:
    # Season 0 is valid; it is the specials season.
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ValueError("season number must be a non-negative integer")
    return value


def _season_numbers(entry: Any) -> int | None:
    if not isinstance(entry, dict):
        return None
    number = entry.get("seasonNumber")
    return number if type(number) is int and not isinstance(number, bool) else None


def _monitored_seasons(series: Mapping[str, Any]) -> list[int]:
    seasons = series.get("seasons")
    if not isinstance(seasons, list):
        return []
    return sorted(
        number for entry in seasons
        if (number := _season_numbers(entry)) is not None and entry.get("monitored") is True
    )


class LibraryClient:
    """Add movies/series to a configured Arr instance, pinned by external id."""

    def __init__(
        self,
        service: str,
        base_url: str,
        api_key: str,
        *,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if service not in {"radarr", "sonarr"}:
            raise ValueError("invalid library client configuration")
        self.service = service
        self.sleep = sleep
        self.http = JsonClient(
            service, base_url, headers={"X-Api-Key": api_key},
            transport=transport, sleep=sleep, deadline=deadline, clock=clock,
        )

    # -- shared -----------------------------------------------------------

    def _existing(self, endpoint: str, id_field: str, operation: str) -> dict[int, dict[str, Any]]:
        current = self.http.request("GET", endpoint, operation=operation)
        if not isinstance(current, list):
            raise ApiError(self.service, operation, None, "invalid_response")
        found: dict[int, dict[str, Any]] = {}
        for item in current:
            if isinstance(item, dict) and type(item.get(id_field)) is int:
                found[item[id_field]] = item
        return found

    # -- movies -----------------------------------------------------------

    def add_movie(
        self, tmdb_id: int, *, quality_profile_id: int, root_folder_path: str,
        search: bool = True, minimum_availability: str = "released",
    ) -> dict[str, Any]:
        """Add one movie by TMDB id. Returns the outcome; never raises on re-add."""
        tmdb_id = _identifier(tmdb_id)
        present = self._existing("/api/v3/movie", "tmdbId", "list movies")
        if tmdb_id in present:
            existing = present[tmdb_id]
            return {"status": "present", "id": existing.get("id"), "title": existing.get("title"), "tmdbId": tmdb_id}

        lookup = self.http.request(
            "GET", f"/api/v3/movie/lookup/tmdb?{urlencode({'tmdbId': tmdb_id})}",
            operation="look up movie",
        )
        # Trust the id we asked for, never a title match.
        if not isinstance(lookup, dict) or lookup.get("tmdbId") != tmdb_id:
            raise ApiError(self.service, "look up movie", None, "lookup_mismatch")

        created = self.http.request("POST", "/api/v3/movie", operation="add movie", payload={
            "title": lookup.get("title"), "tmdbId": tmdb_id, "year": lookup.get("year"),
            "titleSlug": lookup.get("titleSlug"), "images": lookup.get("images", []),
            "qualityProfileId": quality_profile_id, "rootFolderPath": root_folder_path,
            "monitored": True, "minimumAvailability": minimum_availability,
            "addOptions": {"searchForMovie": bool(search)},
        })
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ApiError(self.service, "add movie", None, "invalid_response")
        return {"status": "added", "id": created["id"], "title": created.get("title"), "tmdbId": tmdb_id}

    # -- series -----------------------------------------------------------

    def _lookup_series(self, tvdb_id: int) -> dict[str, Any]:
        results = self.http.request(
            "GET", f"/api/v3/series/lookup?{urlencode({'term': f'tvdb:{tvdb_id}'})}",
            operation="look up series",
        )
        if not isinstance(results, list):
            raise ApiError(self.service, "look up series", None, "invalid_response")
        # Title-equality ranking is unreliable: an unrelated series can share a
        # title exactly and outrank the intended one. Match the id only.
        matches = [item for item in results if isinstance(item, dict) and item.get("tvdbId") == tvdb_id]
        if len(matches) != 1:
            raise ApiError(self.service, "look up series", None, "lookup_mismatch")
        return matches[0]

    def _settle_monitoring(self, series_id: int, wanted: Sequence[int]) -> dict[str, Any]:
        """Assert season monitoring and require it to hold across a delay."""
        target = sorted(set(wanted))
        confirmations = 0
        for attempt in range(SETTLE_ATTEMPTS):
            current = self.http.request("GET", f"/api/v3/series/{series_id}", operation="read series")
            if not isinstance(current, dict) or not isinstance(current.get("seasons"), list):
                raise ApiError(self.service, "read series", None, "invalid_response")
            if current.get("monitored") is True and _monitored_seasons(current) == target:
                confirmations += 1
                # One read straight after a write is not evidence; a second
                # read after the refresh window is.
                if confirmations >= 2:
                    return {"seasons": target, "settled": True, "attempts": attempt + 1}
            else:
                confirmations = 0
                desired = dict(current)
                desired["monitored"] = True
                desired["seasons"] = [
                    {**entry, "monitored": _season_numbers(entry) in target}
                    for entry in current["seasons"] if _season_numbers(entry) is not None
                ]
                self.http.request("PUT", f"/api/v3/series/{series_id}", operation="update series", payload=desired)
            if attempt + 1 < SETTLE_ATTEMPTS:
                self.sleep(SETTLE_DELAY)
        raise ApiError(self.service, "update series", None, "monitoring_unstable")

    def add_series(
        self, tvdb_id: int, seasons: Iterable[int], *, quality_profile_id: int,
        root_folder_path: str, search: bool = True, season_folder: bool = True,
    ) -> dict[str, Any]:
        """Add one series by TVDB id, monitoring exactly `seasons`."""
        tvdb_id = _identifier(tvdb_id)
        target = sorted({_season_number(number) for number in seasons})
        if not target:
            raise ValueError("at least one season must be selected")

        present = self._existing("/api/v3/series", "tvdbId", "list series")
        if tvdb_id in present:
            existing = present[tvdb_id]
            return {
                "status": "present", "id": existing.get("id"), "title": existing.get("title"),
                "tvdbId": tvdb_id, "seasons": _monitored_seasons(existing),
            }

        lookup = self._lookup_series(tvdb_id)
        available = {
            number for entry in lookup.get("seasons", [])
            if (number := _season_numbers(entry)) is not None
        }
        if missing := [number for number in target if number not in available]:
            raise ApiError(self.service, "add series", None, f"season_not_found:{missing[0]}")

        created = self.http.request("POST", "/api/v3/series", operation="add series", payload={
            "title": lookup.get("title"), "tvdbId": tvdb_id, "year": lookup.get("year"),
            "titleSlug": lookup.get("titleSlug"), "images": lookup.get("images", []),
            "seasons": [
                {"seasonNumber": number, "monitored": number in target}
                for number in sorted(available)
            ],
            "seriesType": "standard", "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path, "monitored": True, "seasonFolder": bool(season_folder),
            # Searching is issued per season *after* monitoring is confirmed, so
            # the add itself must not trigger one.
            "addOptions": {"monitor": "none", "searchForMissingEpisodes": False},
        })
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ApiError(self.service, "add series", None, "invalid_response")

        settled = self._settle_monitoring(created["id"], target)
        if search:
            for number in target:
                self.http.request("POST", "/api/v3/command", operation="search season", payload={
                    "name": "SeasonSearch", "seriesId": created["id"], "seasonNumber": number,
                })
        return {
            "status": "added", "id": created["id"], "title": created.get("title"),
            "tvdbId": tvdb_id, "seasons": settled["seasons"], "searched": bool(search),
        }
