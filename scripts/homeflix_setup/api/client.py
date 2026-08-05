"""Small bounded JSON HTTP client with deliberately redacted failures."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any, Callable, Mapping
from urllib import error, request
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class ApiError(Exception):
    service: str
    operation: str
    status: int | None
    code: str

    def __str__(self) -> str:
        suffix = f" (HTTP {self.status})" if self.status is not None else ""
        return f"{self.service} {self.operation} failed: {self.code}{suffix}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[request.Request, float], HttpResponse]


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(outgoing: request.Request, timeout: float) -> HttpResponse:
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    with opener.open(outgoing, timeout=timeout) as response:
        return HttpResponse(response.status, response.read(2 * 1024 * 1024 + 1))


class JsonClient:
    """JSON-only HTTP client; bases are restricted to local setup endpoints."""

    def __init__(
        self,
        service: str,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        transport: Transport = urllib_transport,
        timeout: float = 5.0,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("API base must be a plain-HTTP loopback address")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("API base contains unsupported components")
        self.service = service
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = dict(headers or {})
        self.transport = transport
        self.timeout = min(max(float(timeout), 0.1), 15.0)
        self.attempts = min(max(int(attempts), 1), 4)
        self.sleep = sleep
        self.clock = clock

    def request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        payload: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if any(ord(character) < 32 or ord(character) == 127 for character in path) or re.search(r"%(?![0-9A-Fa-f]{2})", path):
            raise ValueError("API path must be absolute and local")
        parsed_path = urlsplit(path)
        if parsed_path.scheme or parsed_path.netloc or parsed_path.fragment or not parsed_path.path.startswith("/") or parsed_path.path.startswith("//"):
            raise ValueError("API path must be absolute and local")
        decoded = parsed_path.path
        for _ in range(3):
            decoded = unquote(decoded)
            first = decoded[1:].split("/", 1)[0].casefold() if decoded.startswith("/") else ""
            segments = decoded.split("/")
            if (
                not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded
                or first in {"http:", "https:"} or any(segment in {".", ".."} for segment in segments)
                or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
            ):
                raise ValueError("API path must be absolute and local")
        method = method.upper()
        encoded = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        outgoing_headers = {"Accept": "application/json", **self.headers, **dict(headers or {})}
        if encoded is not None:
            outgoing_headers["Content-Type"] = "application/json"
        safe_retry = method in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
        count = self.attempts if safe_retry else 1
        deadline = self.clock() + self.timeout * count

        def wait_before_retry(attempt: int) -> bool:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.sleep(min(0.25 * (2 ** attempt), 1.0, remaining))
            return self.clock() < deadline

        for attempt in range(count):
            outgoing = request.Request(
                self.base_url.rstrip("/") + path, data=encoded,
                headers=outgoing_headers, method=method,
            )
            try:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError
                response = self.transport(outgoing, min(self.timeout, remaining))
                if len(response.body) > 2 * 1024 * 1024:
                    raise ApiError(self.service, operation, response.status, "response_too_large")
                if not 200 <= response.status < 300:
                    if response.status >= 500 and attempt + 1 < count and wait_before_retry(attempt):
                        continue
                    raise ApiError(self.service, operation, response.status, "http_error")
                if not response.body:
                    return None
                result = json.loads(response.body.decode("utf-8"))
                if not isinstance(result, (dict, list)):
                    raise ValueError
                return result
            except ApiError:
                raise
            except error.HTTPError as caught:
                status = caught.code if isinstance(caught.code, int) else None
                caught.close()
                if status is not None and status >= 500 and attempt + 1 < count and wait_before_retry(attempt):
                    continue
                raise ApiError(self.service, operation, status, "http_error") from None
            except (error.URLError, OSError, TimeoutError):
                if attempt + 1 < count and wait_before_retry(attempt):
                    continue
                raise ApiError(self.service, operation, None, "transport_error") from None
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise ApiError(self.service, operation, response.status, "invalid_response") from None
        raise AssertionError("unreachable")
