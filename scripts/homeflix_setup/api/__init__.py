"""API-driven core service initialization."""

from .arr import ArrClient, read_api_key
from .client import ApiError, HttpResponse, JsonClient
from .jellyfin import JellyfinClient, read_jellyfin_api_key
from .seerr import SeerrClient, read_settings_api_key
from .library import LibraryClient

__all__ = ["ApiError", "ArrClient", "HttpResponse", "JellyfinClient", "SeerrClient", "JsonClient", "LibraryClient", "read_api_key", "read_jellyfin_api_key", "read_settings_api_key"]
