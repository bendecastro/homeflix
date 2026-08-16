from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
import tempfile
import unittest
from unittest.mock import patch
from urllib import error

from scripts.homeflix_setup.api import ApiError, HttpResponse, JellyfinClient, JsonClient, read_jellyfin_api_key

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


class CountingHTTPServer(ThreadingHTTPServer):
    def get_request(self):
        connection = super().get_request()
        self.connection_count += 1
        return connection


class WireHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.events.append((self.path, dict(self.headers)))
        status, headers, body = self.server.routes[self.path]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        pass


@contextmanager
def wire_server(routes):
    server = CountingHTTPServer(("127.0.0.1", 0), WireHandler)
    server.connection_count = 0
    server.routes = routes
    server.events = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); server.server_close(); thread.join()


class FixtureContractTests(unittest.TestCase):
    def test_fixture_contracts_identify_current_endpoint_versions(self):
        contracts = fixture("contracts.json")
        self.assertIn("Startup and Library", contracts["jellyfin"])
        self.assertEqual(contracts["servarr"], "API v3")
        self.assertEqual(contracts["jellyseerr"], "API v1 settings and auth endpoints")


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

    def test_backoff_is_capped_to_remaining_deadline_and_stops_exhausted(self):
        now = [0.0]
        calls = []
        sleeps = []
        def transport(outgoing, timeout):
            calls.append(timeout)
            now[0] += 1.9
            return HttpResponse(503, b"{}")
        def sleep(seconds):
            sleeps.append(seconds); now[0] += seconds
        client = JsonClient("fixture", "http://127.0.0.1", transport=transport, timeout=1, attempts=2, sleep=sleep, clock=lambda: now[0])
        with self.assertRaises(ApiError) as raised:
            client.request("GET", "/status", operation="bounded retry")
        self.assertEqual(raised.exception.code, "http_error")
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(sleeps[0], 0.1)

    def test_wire_transport_ignores_all_proxy_environment_for_loopback(self):
        with wire_server({"/direct": (200, {}, b'{"direct":true}')}) as (direct, direct_base):
            proxy_path = direct_base + "/direct"
            with wire_server({proxy_path: (200, {}, b'{"via":"proxy"}')}) as (proxy, proxy_base):
                proxy_environment = {
                    "HTTP_PROXY": proxy_base, "http_proxy": proxy_base,
                    "ALL_PROXY": proxy_base, "all_proxy": proxy_base,
                    "NO_PROXY": "", "no_proxy": "",
                }
                with patch.dict(os.environ, proxy_environment, clear=False):
                    client = JsonClient("fixture", direct_base, headers={
                        "X-Api-Key": "FIXTURE_API_KEY_NOT_REAL", "Authorization": "Fixture auth",
                    }, attempts=1)
                    self.assertEqual(client.request("GET", "/direct", operation="direct wire check"), {"direct": True})
        self.assertEqual(len(direct.events), 1)
        self.assertEqual(direct.connection_count, 1)
        self.assertEqual(proxy.events, [])
        self.assertEqual(proxy.connection_count, 0)

    def test_wire_transport_rejects_redirect_without_contacting_target(self):
        with wire_server({"/target": (200, {}, b"{}")}) as (target, target_base):
            with wire_server({"/redirect": (302, {"Location": target_base + "/target"}, b"FIXTURE_REDIRECT_BODY")}) as (source, source_base):
                client = JsonClient("fixture", source_base, headers={
                    "X-Api-Key": "FIXTURE_API_KEY_NOT_REAL", "Authorization": "Fixture auth", "Host": "fixture.local",
                }, attempts=1)
                with self.assertRaises(ApiError) as raised:
                    client.request("GET", "/redirect", operation="wire check")
        self.assertEqual(raised.exception.code, "http_error")
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(len(source.events), 1)
        self.assertEqual(target.events, [])
        self.assertNotIn("FIXTURE_REDIRECT_BODY", str(raised.exception))

    def test_wire_transport_caps_bodies_parses_json_and_sends_headers_only_locally(self):
        routes = {
            "/json": (200, {"Content-Type": "application/json"}, b'{"ok":true}'),
            "/error": (418, {}, b"FIXTURE_PRIVATE_BODY"),
            "/large": (200, {}, b"x" * (2 * 1024 * 1024 + 1)),
        }
        with wire_server(routes) as (server, base):
            client = JsonClient("fixture", base, headers={"X-Fixture": "present"}, attempts=1)
            self.assertEqual(client.request("GET", "/json", operation="read json"), {"ok": True})
            for path, code in (("/error", "http_error"), ("/large", "response_too_large")):
                with self.assertRaises(ApiError) as raised:
                    client.request("GET", path, operation="wire check")
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("FIXTURE_PRIVATE_BODY", str(raised.exception))
        self.assertEqual([path for path, _ in server.events], ["/json", "/error", "/large"])
        self.assertTrue(all(headers.get("X-Fixture") == "present" for _, headers in server.events))

    def test_request_paths_cannot_escape_loopback_base(self):
        client = JsonClient("fixture", "http://127.0.0.1")
        invalid = (
            "http://example.invalid/x", "//example.invalid/x", "/http://example.invalid/x",
            "/%2f%2fexample.invalid/x", "/\\\\example.invalid/x", "/../secret", "/%2e%2e/secret",
            "/bad%target", "/ok#fragment", "/ok\nx",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request("GET", path, operation="validate path")

    def test_local_budget_exhaustion_without_shared_deadline_is_transport_error(self):
        now = [0.0]
        def transport(outgoing, timeout):
            now[0] += timeout
            raise TimeoutError
        client = JsonClient("fixture", "http://127.0.0.1", transport=transport, timeout=1, attempts=1, clock=lambda: now[0])
        with self.assertRaises(ApiError) as raised:
            client.request("GET", "/local", operation="local budget")
        self.assertEqual(raised.exception.code, "transport_error")

    def test_local_budget_exhaustion_before_shared_deadline_is_transport_error(self):
        now = [0.0]
        def transport(outgoing, timeout):
            now[0] += timeout
            raise TimeoutError
        client = JsonClient("fixture", "http://127.0.0.1", transport=transport, timeout=1, attempts=1, clock=lambda: now[0], deadline=10.0)
        with self.assertRaises(ApiError) as raised:
            client.request("GET", "/local", operation="local budget")
        self.assertEqual(raised.exception.code, "transport_error")
        self.assertLess(now[0], 10.0)

    def test_shared_absolute_deadline_caps_sequential_requests_without_reset(self):
        now = [0.0]; timeouts = []
        def transport(outgoing, timeout):
            timeouts.append(timeout); now[0] += 0.6
            return HttpResponse(200, b"{}")
        client = JsonClient("fixture", "http://127.0.0.1", transport=transport, timeout=5, attempts=1, clock=lambda: now[0], deadline=1.0)
        client.request("GET", "/one", operation="one")
        client.request("GET", "/two", operation="two")
        with self.assertRaises(ApiError) as raised:
            client.request("GET", "/three", operation="three")
        self.assertEqual(raised.exception.code, "deadline_exhausted")
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts, [1.0, 0.4])

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
            (200, fixture("jellyfin-libraries-options.json")),
            (200, fixture("jellyfin-auth-keys.json")),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        created, libraries = client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertTrue(created)
        self.assertEqual(libraries, ["Movies", "Shows", "Music"])
        self.assertIsNone(client.token)
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
            (200, fixture("jellyfin-libraries-complete.json")),
            (200, fixture("jellyfin-libraries-options.json")),
            (200, fixture("jellyfin-auth-keys.json")),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        created, _ = client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertFalse(created)
        self.assertIsNone(client.token)
        self.assertFalse(any(request.full_url.endswith("/Startup/User") for request, _ in transport.requests))
        self.assertEqual(sum(request.full_url.endswith("/Users/AuthenticateByName") for request, _ in transport.requests), 1)

    def test_completed_startup_authenticates_without_recreating_admin_and_libraries_are_idempotent(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-complete.json")),
            (200, fixture("jellyfin-libraries-options.json")),
            (200, fixture("jellyfin-auth-keys.json")),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        created, libraries = client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertFalse(created)
        self.assertEqual(libraries, ["Movies", "Shows", "Music"])
        self.assertIsNone(client.token)
        self.assertEqual(client.application_key, "JELLYFIN_DEDICATED_KEY_NOT_REAL")
        self.assertFalse(any(request.full_url.endswith("/Startup/User") for request, _ in transport.requests))
        self.assertFalse(any(request.method == "POST" and "/Library/VirtualFolders?" in request.full_url for request, _ in transport.requests))

    def test_library_options_stop_writes_into_the_read_only_media_mount(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-complete.json")),
            (200, fixture("jellyfin-libraries-options-writing.json")),
            (204, {}), (204, {}),
            (200, fixture("jellyfin-auth-keys.json")),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        updates = [
            json.loads(request.data) for request, _ in transport.requests
            if request.data and request.full_url.endswith("/Library/VirtualFolders/LibraryOptions")
        ]
        self.assertEqual([update["Id"] for update in updates], [
            "f137a2dd21bbc1b99aa5c0f6bf02a805", "a656b907eb3a73532e40e44b968d0225",
        ])
        for update in updates:
            self.assertEqual(update["LibraryOptions"]["SaveLocalMetadata"], False)
            self.assertEqual(update["LibraryOptions"]["MetadataSavers"], [])
            self.assertEqual(update["LibraryOptions"]["SaveTrickplayWithMedia"], False)
            self.assertEqual(update["LibraryOptions"]["PreferredMetadataLanguage"], "en")

    def test_compliant_library_options_are_left_untouched(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-complete.json")),
            (200, fixture("jellyfin-libraries-options.json")),
            (200, fixture("jellyfin-auth-keys.json")),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertFalse(any(
            request.full_url.endswith("/Library/VirtualFolders/LibraryOptions")
            for request, _ in transport.requests
        ))

    def test_library_duplicates_wrong_types_and_extra_locations_conflict_before_writes(self):
        conflicts = (
            "jellyfin-libraries-duplicate.json",
            "jellyfin-libraries-wrong-type.json",
            "jellyfin-libraries-extra-location.json",
            "jellyfin-libraries-duplicate-locations.json",
            "jellyfin-libraries-empty-location.json",
            "jellyfin-libraries-non-string-location.json",
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                transport = FixtureTransport([
                    (200, fixture("jellyfin-startup-complete.json")),
                    (200, fixture("jellyfin-auth.json")),
                    (200, fixture(conflict)),
                    (204, {}),
                ])
                client = JellyfinClient(transport=transport)
                with self.assertRaises(ApiError) as raised:
                    client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
                self.assertIsNone(client.token)
                self.assertEqual(raised.exception.code, "library_conflict")
                self.assertFalse(any(request.method == "POST" and "/Library/VirtualFolders?" in request.full_url for request, _ in transport.requests))
                rendered = str(raised.exception)
                self.assertNotIn("/data", rendered)
                self.assertNotIn("FIXTURE", rendered)

    def test_inspection_uses_only_authentication_post_then_gets(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-complete.json")),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        result = client.inspect("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertEqual(result, {"initialized": True, "libraries_exact": True})
        methods_paths = [(request.method, request.full_url) for request, _ in transport.requests]
        posts = [(method, path) for method, path in methods_paths if method != "GET"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[0][1].endswith("/Users/AuthenticateByName"))
        self.assertTrue(posts[1][1].endswith("/Sessions/Logout"))
        self.assertIsNone(client.token)
        self.assertFalse(any("Startup/" in path or "Library/VirtualFolders?" in path for _, path in methods_paths))

    def test_partial_startup_failure_logs_out_without_masking_primary(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-incomplete.json")),
            (200, fixture("jellyfin-auth.json")),
            (500, {}),
            (204, {}),
        ])
        client = JellyfinClient(transport=transport)
        with self.assertRaises(ApiError) as raised:
            client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertEqual(raised.exception.operation, "set startup configuration")
        self.assertTrue(transport.requests[-1][0].full_url.endswith("/Sessions/Logout"))
        self.assertIsNone(client.token)

    def test_work_deadline_reserves_outer_budget_for_logout(self):
        now = [0.0]; events = []
        responses = iter((
            HttpResponse(200, json.dumps(fixture("jellyfin-startup-complete.json")).encode()),
            HttpResponse(200, json.dumps(fixture("jellyfin-auth.json")).encode()),
            HttpResponse(200, json.dumps(fixture("jellyfin-libraries-complete.json")).encode()),
            HttpResponse(200, json.dumps(fixture("jellyfin-libraries-options.json")).encode()),
            HttpResponse(200, json.dumps(fixture("jellyfin-auth-keys.json")).encode()),
            HttpResponse(204, b""),
        ))
        def transport(outgoing, timeout):
            events.append((outgoing.full_url, timeout))
            increments = (0.3, 0.3, 0.2, 0.1, 0.05, 0.1)
            now[0] += increments[len(events) - 1]
            return next(responses)
        client = JellyfinClient(transport=transport, deadline=2.0, clock=lambda: now[0])
        created, _ = client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertFalse(created)
        self.assertTrue(events[-1][0].endswith("/Sessions/Logout"))
        self.assertLessEqual(now[0], 2.0)
        self.assertGreater(events[-1][1], 0)
        self.assertIsNone(client.token)

    def test_inspection_logout_failure_is_fail_closed_and_token_is_cleared(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, fixture("jellyfin-libraries-complete.json")),
            (500, {}),
        ])
        client = JellyfinClient(transport=transport)
        with self.assertRaises(ApiError) as raised:
            client.inspect("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertEqual(raised.exception.operation, "close verification session")
        self.assertIsNone(client.token)
        self.assertNotIn("MEMORY_ONLY", str(raised.exception))

    def test_primary_inspection_failure_is_not_masked_by_logout_failure(self):
        transport = FixtureTransport([
            (200, fixture("jellyfin-startup-complete.json")),
            (200, fixture("jellyfin-auth.json")),
            (200, {"malformed": True}),
            (500, {}),
        ])
        client = JellyfinClient(transport=transport)
        with self.assertRaises(ApiError) as raised:
            client.inspect("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        self.assertEqual(raised.exception.operation, "inspect libraries")
        self.assertIsNone(client.token)

    def test_repeated_reconcile_closes_every_ephemeral_session(self):
        responses = []
        for _ in range(2):
            responses.extend(((200, fixture("jellyfin-startup-complete.json")), (200, fixture("jellyfin-auth.json")), (200, fixture("jellyfin-libraries-complete.json")), (200, fixture("jellyfin-libraries-options.json")), (200, fixture("jellyfin-auth-keys.json")), (204, {})))
        transport = FixtureTransport(responses)
        client = JellyfinClient(transport=transport)
        for _ in range(2):
            created, libraries = client.reconcile("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
            self.assertFalse(created); self.assertEqual(libraries, ["Movies", "Shows", "Music"])
        self.assertEqual(sum(request.full_url.endswith("/Sessions/Logout") for request, _ in transport.requests), 2)
        self.assertIsNone(client.token)

    def test_repeated_inspection_closes_every_ephemeral_session(self):
        responses = []
        for _ in range(2):
            responses.extend(((200, fixture("jellyfin-startup-complete.json")), (200, fixture("jellyfin-auth.json")), (200, fixture("jellyfin-libraries-complete.json")), (204, {})))
        transport = FixtureTransport(responses)
        client = JellyfinClient(transport=transport)
        for _ in range(2):
            self.assertTrue(client.inspect("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")["libraries_exact"])
        self.assertEqual(sum(request.full_url.endswith("/Sessions/Logout") for request, _ in transport.requests), 2)
        self.assertIsNone(client.token)

    def test_non_idempotent_post_is_not_blindly_retried(self):
        transport = FixtureTransport([error.URLError("fixture secret")])
        client = JellyfinClient(transport=transport)
        with self.assertRaises(ApiError) as raised:
            client.http.request("POST", "/Startup/Complete", operation="complete startup", payload={})
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(raised.exception.code, "transport_error")

    def test_creates_dedicated_application_key_and_does_not_duplicate(self):
        created = fixture("jellyfin-auth-keys.json")
        transport = FixtureTransport([
            (200, fixture("jellyfin-auth-keys-empty.json")),
            (204, {}),
            (200, created),
            (200, created),
        ])
        client = JellyfinClient(transport=transport)
        client.token = "MEMORY_ONLY_TOKEN"
        first = client.ensure_application_key()
        second = client.ensure_application_key()
        self.assertEqual(first, second)
        self.assertEqual(client.application_key, first)
        posts = [request for request, _ in transport.requests if request.method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn("app=Radarr+and+Sonarr", posts[0].full_url)
        self.assertTrue(all(request.headers.get("X-emby-token") == "MEMORY_ONLY_TOKEN" for request, _ in transport.requests))
        self.assertNotIn("JELLYFIN_DEDICATED_KEY_NOT_REAL", json.dumps([request.full_url for request, _ in transport.requests]))

    def test_duplicate_application_keys_conflict_without_create(self):
        duplicate = {
            "Items": [
                {"AccessToken": "JELLYFIN_DEDICATED_KEY_NOT_REAL", "AppName": "Radarr and Sonarr"},
                {"AccessToken": "OTHER_DEDICATED_KEY_NOT_REAL", "Name": "Radarr and Sonarr"},
            ],
            "TotalRecordCount": 2,
        }
        transport = FixtureTransport([(200, duplicate), (204, {})])
        client = JellyfinClient(transport=transport)
        client.token = "MEMORY_ONLY_TOKEN"
        with self.assertRaises(ApiError) as raised:
            client.ensure_application_key()
        self.assertEqual(raised.exception.code, "application_key_conflict")
        self.assertTrue(all(request.method == "GET" for request, _ in transport.requests))

    def test_reads_dedicated_jellyfin_key_from_config_root_and_rejects_unsafe_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            db_dir = root / "jellyfin" / "data" / "data"
            db_dir.mkdir(parents=True)
            db = db_dir / "jellyfin.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE ApiKeys (Id INTEGER PRIMARY KEY, AccessToken TEXT, Name TEXT)")
            connection.execute(
                "INSERT INTO ApiKeys (AccessToken, Name) VALUES (?, ?)",
                ("JELLYFIN_DEDICATED_KEY_NOT_REAL", "Radarr and Sonarr"),
            )
            connection.commit()
            connection.close()
            root.chmod(0o755)
            (root / "jellyfin").chmod(0o755)
            (root / "jellyfin" / "data").chmod(0o755)
            db_dir.chmod(0o755)
            db.chmod(0o644)
            uid = os.getuid()
            self.assertEqual(read_jellyfin_api_key(root, uid), "JELLYFIN_DEDICATED_KEY_NOT_REAL")
            original = db.stat().st_mode & 0o777
            db.chmod(original | 0o020)
            with self.assertRaises(ValueError):
                read_jellyfin_api_key(root, uid)
            db.chmod(original)
            with self.assertRaises(ValueError):
                read_jellyfin_api_key(root, uid + 100000)


if __name__ == "__main__":
    unittest.main()
