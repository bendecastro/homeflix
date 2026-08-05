from __future__ import annotations

import json
from pathlib import Path
import unittest
from urllib import error

from scripts.homeflix_setup.api import ApiError, HttpResponse, JellyfinClient, JsonClient

FIXTURES = Path(__file__).parent / "fixtures" / "api"


class FixtureTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
    def __call__(self, outgoing, timeout):
        self.requests.append((outgoing, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return HttpResponse(response[0], json.dumps(response[1]).encode())


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class JsonClientTests(unittest.TestCase):
    def test_retries_only_safe_methods_with_bounded_timeout_and_rejects_nonlocal_bases(self):
        transport = FixtureTransport([(503, {}), (200, {"ready": True})])
        sleeps = []
        client = JsonClient("fixture", "http://127.0.0.1", transport=transport, timeout=2, attempts=2, sleep=sleeps.append)
        self.assertEqual(client.request("GET", "/status", operation="read status"), {"ready": True})
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(sleeps, [0.25])
        self.assertTrue(all(timeout <= 2 for _, timeout in transport.requests))
        for base in ("https://127.0.0.1", "http://example.invalid", "http://user:secret@127.0.0.1"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                JsonClient("fixture", base)

    def test_post_cannot_override_retry_policy(self):
        transport = FixtureTransport([error.URLError("fixture secret"), (200, {})])
        client = JsonClient("fixture", "http://127.0.0.1", transport=transport, attempts=2)
        with self.assertRaises(TypeError):
            client.request("POST", "/create", operation="create object", payload={}, retry=True)
        self.assertEqual(len(transport.requests), 0)


class JellyfinApiTests(unittest.TestCase):
    def test_startup_uses_official_sequence_authenticates_and_creates_libraries(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-incomplete.json")),
            (401, {}),
            (204, {}), (204, {}), (204, {}), (204, {}),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-empty.json")),
            (204, {}), (204, {}), (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        self.assertTrue(client.initialize("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL"))
        self.assertEqual(client.ensure_libraries(), ["Movies", "Shows", "Music"])
        paths = [request.full_url.split("127.0.0.1:8096", 1)[1] for request, _ in transport.requests]
        self.assertEqual(paths[:7], [
            "/System/Info/Public", "/Users/AuthenticateByName", "/Startup/Configuration",
            "/Startup/User", "/Startup/RemoteAccess", "/Startup/Complete", "/Users/AuthenticateByName",
        ])
        payloads = [json.loads(request.data) for request, _ in transport.requests if request.data]
        self.assertEqual(payloads[:6], [
            fixture("jellyfin-auth-request.json"),
            fixture("jellyfin-startup-configuration.json"),
            fixture("jellyfin-startup-user.json"),
            fixture("jellyfin-startup-remote.json"),
            fixture("jellyfin-startup-complete-request.json"),
            fixture("jellyfin-auth-request.json"),
        ])
        self.assertTrue(all(timeout <= 15 for _, timeout in transport.requests))

    def test_incomplete_wizard_with_existing_admin_authenticates_instead_of_recreating(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-incomplete.json")),
            (200, fixture("jellyfin-auth.json")),
            (204, {}), (204, {}), (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        self.assertFalse(client.initialize("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL"))
        self.assertFalse(any(request.full_url.endswith("/Startup/User") for request, _ in transport.requests))
        self.assertEqual(sum(request.full_url.endswith("/Users/AuthenticateByName") for request, _ in transport.requests), 1)

    def test_completed_startup_authenticates_without_recreating_admin_and_libraries_are_idempotent(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-complete.json")),
        ])
        client = JellyfinClient(transport=transport)
        self.assertFalse(client.initialize("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL"))
        self.assertEqual(client.ensure_libraries(), ["Movies", "Shows", "Music"])
        self.assertFalse(any(request.full_url.endswith("/Startup/User") for request, _ in transport.requests))
        self.assertFalse(any(request.method == "POST" and "/Library/VirtualFolders?" in request.full_url for request, _ in transport.requests))

    def test_library_duplicates_wrong_types_and_extra_locations_conflict_before_writes(self):
        conflicts = (
            "jellyfin-libraries-duplicate.json",
            "jellyfin-libraries-wrong-type.json",
            "jellyfin-libraries-extra-location.json",
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                transport = FixtureTransport([
                    (200, fixture("jellyfin-startup-complete.json")),
                    (200, fixture("jellyfin-auth.json")),
                    (200, fixture(conflict)),
                ])
                client = JellyfinClient(transport=transport)
                client.initialize("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
                with self.assertRaises(ApiError) as raised:
                    client.ensure_libraries()
                self.assertEqual(raised.exception.code, "library_conflict")
                self.assertFalse(any(request.method == "POST" and "/Library/VirtualFolders?" in request.full_url for request, _ in transport.requests))
                rendered = str(raised.exception)
                self.assertNotIn("/data", rendered)
                self.assertNotIn("FIXTURE", rendered)

    def test_non_idempotent_post_is_not_blindly_retried(self):
        transport = FixtureTransport([error.URLError("fixture secret")])
        client = JellyfinClient(transport=transport)
        with self.assertRaises(ApiError) as raised:
            client.http.request("POST", "/Startup/Complete", operation="complete startup", payload={})
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(raised.exception.code, "transport_error")


if __name__ == "__main__":
    unittest.main()
