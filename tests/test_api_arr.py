from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.homeflix_setup.api import ApiError, ArrClient, HttpResponse, read_api_key

FIXTURE_KEY = "FIXTURE_API_KEY_1234567890ABCDE"
FIXTURES = Path(__file__).parent / "fixtures" / "api"


class RouteTransport:
    def __init__(self, routes):
        self.routes = {key: list(values) for key, values in routes.items()}
        self.requests = []
    def __call__(self, outgoing, timeout):
        key = (outgoing.method, outgoing.full_url.split("127.0.0.1", 1)[1])
        self.requests.append(outgoing)
        payload = self.routes[key].pop(0)
        return HttpResponse(200, json.dumps(payload).encode())


def routes(service, root):
    rename = "renameMovies" if service == "radarr" else "renameEpisodes"
    return {
        ("GET", "/api/v3/qualityprofile"): [json.loads((FIXTURES / "arr-profiles.json").read_text(encoding="utf-8"))],
        ("GET", "/api/v3/rootfolder"): [[]],
        ("POST", "/api/v3/rootfolder"): [{"id": 12, "path": root}],
        ("GET", "/api/v3/config/naming"): [{"id": 1, rename: False, "unowned_naming": "keep"}],
        ("PUT", "/api/v3/config/naming"): [{}],
        ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": False, "unowned": "keep"}],
        ("PUT", "/api/v3/config/mediamanagement"): [{}],
        ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": False, "unowned": 7}],
        ("PUT", "/api/v3/config/downloadclient"): [{}],
    }


class ArrApiTests(unittest.TestCase):
    def test_reads_0644_config_and_rejects_unsafe_traversal_permissions_owner_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            service = root / "radarr"
            service.mkdir(parents=True)
            config = service / "config.xml"
            config.write_text(f"<Config><ApiKey>{FIXTURE_KEY}</ApiKey></Config>", encoding="utf-8")
            root.chmod(0o755); service.chmod(0o755); config.chmod(0o644)
            uid = os.getuid()
            self.assertEqual(read_api_key(root, "radarr", uid), FIXTURE_KEY)
            root_link = root.with_name("config-link")
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                read_api_key(root_link, "radarr", uid)
            for unsafe in (root, service, config):
                with self.subTest(unsafe=unsafe.name):
                    original = unsafe.stat().st_mode & 0o777
                    unsafe.chmod(original | 0o020)
                    with self.assertRaises(ValueError):
                        read_api_key(root, "radarr", uid)
                    unsafe.chmod(original)
            with self.assertRaises(ValueError):
                read_api_key(root, "radarr", uid + 100000)
            moved = service.with_name("real")
            service.rename(moved)
            service.symlink_to(moved, target_is_directory=True)
            with self.assertRaises(ValueError):
                read_api_key(root, "radarr", uid)
            service.unlink(); moved.rename(service)
            config = service / "config.xml"
            real_config = service / "real.xml"
            config.rename(real_config); config.symlink_to(real_config)
            with self.assertRaises(ValueError):
                read_api_key(root, "radarr", uid)
            config.unlink(); config.mkdir()
            with self.assertRaises(ValueError):
                read_api_key(root, "radarr", uid)
            config.rmdir(); real_config.rename(config)
            config.write_text("<Config><ApiKey>short</ApiKey></Config>", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_api_key(root, "radarr", uid)

    def test_discovers_profile_by_name_creates_roots_and_preserves_unowned_config(self):
        for service, root in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
            with self.subTest(service=service):
                transport = RouteTransport(routes(service, root))
                client = ArrClient(service, "http://127.0.0.1", FIXTURE_KEY, transport=transport)
                result = client.configure("Fixture HD", root)
                rename = "renameMovies" if service == "radarr" else "renameEpisodes"
                self.assertEqual(result["profile"], "Fixture HD")
                self.assertEqual(result["root"], root)
                by_path = {
                    request.full_url.split("127.0.0.1", 1)[1]: json.loads(request.data)
                    for request in transport.requests if request.method == "PUT"
                }
                # The rename flag lives in config/naming; config/mediamanagement
                # accepts it silently and drops it, leaving renaming off.
                self.assertTrue(by_path["/api/v3/config/naming"][rename])
                self.assertEqual(by_path["/api/v3/config/naming"]["unowned_naming"], "keep")
                self.assertNotIn(rename, by_path["/api/v3/config/mediamanagement"])
                self.assertTrue(by_path["/api/v3/config/mediamanagement"]["copyUsingHardlinks"])
                self.assertEqual(by_path["/api/v3/config/mediamanagement"]["unowned"], "keep")
                self.assertEqual(by_path["/api/v3/config/downloadclient"]["unowned"], 7)
                self.assertTrue(by_path["/api/v3/config/downloadclient"]["enableCompletedDownloadHandling"])
                self.assertTrue(all("apikey=" not in request.full_url.casefold() for request in transport.requests))
                self.assertTrue(all(request.headers.get("X-api-key") == FIXTURE_KEY for request in transport.requests))

    def test_configure_creates_one_targeted_and_one_refresh_connection(self):
        jellyfin_key = "JELLYFIN_DEDICATED_KEY_NOT_REAL"
        for service, root in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
            with self.subTest(service=service):
                table = routes(service, root)
                table[("GET", "/api/v3/notification")] = [[]]
                table[("POST", "/api/v3/notification")] = [
                    {"id": 21, "implementation": "MediaBrowser"},
                    {"id": 22, "implementation": "Webhook"},
                ]
                transport = RouteTransport(table)
                client = ArrClient(service, "http://127.0.0.1", FIXTURE_KEY, transport=transport)
                result = client.configure("Fixture HD", root, jellyfin_api_key=jellyfin_key)
                self.assertTrue(result["targeted_connection_changed"])
                self.assertTrue(result["refresh_connection_changed"])
                created = [json.loads(request.data) for request in transport.requests if request.method == "POST" and request.full_url.endswith("/api/v3/notification")]
                self.assertEqual(len(created), 2)
                implementations = [item["implementation"] for item in created]
                self.assertEqual(implementations, ["MediaBrowser", "Webhook"])
                targeted, refresh = created
                fields = {field["name"]: field["value"] for field in targeted["fields"]}
                self.assertEqual(fields["host"], "jellyfin")
                self.assertEqual(fields["port"], 8096)
                self.assertFalse(fields["useSsl"])
                self.assertFalse(fields["notify"])
                self.assertTrue(fields["updateLibrary"])
                self.assertEqual(fields["apiKey"], jellyfin_key)
                refresh_fields = {field["name"]: field["value"] for field in refresh["fields"]}
                self.assertEqual(refresh_fields["url"], "http://jellyfin:8096/Library/Refresh")
                self.assertEqual(refresh_fields["method"], 1)
                headers = refresh_fields["headers"]
                self.assertEqual(headers, [{"key": "X-Emby-Token", "value": jellyfin_key}])
                if service == "radarr":
                    self.assertTrue(targeted["onDownload"] and targeted["onUpgrade"] and targeted["onRename"])
                    self.assertTrue(refresh["onDownload"] and refresh["onUpgrade"] and refresh["onRename"])
                else:
                    self.assertTrue(targeted["onImportComplete"] and targeted["onRename"])
                    self.assertFalse(targeted["onDownload"])
                    self.assertTrue(refresh["onImportComplete"] and refresh["onRename"])
                    self.assertFalse(refresh["onDownload"])
                rendered = json.dumps(result)
                self.assertNotIn(jellyfin_key, rendered)
                self.assertNotIn(FIXTURE_KEY, rendered)
                self.assertTrue(all("apikey=" not in request.full_url.casefold() for request in transport.requests))

    def _owned_notifications(self, service, jellyfin_key):
        events = {
            "onGrab": False,
            "onDownload": service == "radarr",
            "onUpgrade": service == "radarr",
            "onRename": True,
            "onHealthIssue": False,
            "includeHealthWarnings": False,
            "onHealthRestored": False,
            "onApplicationUpdate": False,
            "onManualInteractionRequired": False,
        }
        if service == "radarr":
            events.update({
                "onMovieAdded": False, "onMovieDelete": False,
                "onMovieFileDelete": False, "onMovieFileDeleteForUpgrade": False,
            })
        else:
            events.update({
                "onImportComplete": True, "onSeriesAdd": False, "onSeriesDelete": False,
                "onEpisodeFileDelete": False, "onEpisodeFileDeleteForUpgrade": False,
            })
        targeted = {
            "id": 21,
            "implementation": "MediaBrowser",
            "configContract": "MediaBrowserSettings",
            "name": "Renamed by operator",
            "fields": [
                {"name": "host", "value": "jellyfin"},
                {"name": "port", "value": 8096},
                {"name": "useSsl", "value": False},
                {"name": "urlBase", "value": ""},
                {"name": "apiKey", "value": jellyfin_key},
                {"name": "notify", "value": False},
                {"name": "updateLibrary", "value": True},
            ],
            **events,
        }
        refresh = {
            "id": 22,
            "implementation": "Webhook",
            "configContract": "WebhookSettings",
            "name": "Also renamed",
            "fields": [
                {"name": "url", "value": "http://jellyfin:8096/Library/Refresh"},
                {"name": "method", "value": 1},
                {"name": "headers", "value": [{"key": "X-Emby-Token", "value": jellyfin_key}]},
            ],
            **events,
        }
        return targeted, refresh

    def test_equivalent_discovery_connections_make_no_writes(self):
        jellyfin_key = "JELLYFIN_DEDICATED_KEY_NOT_REAL"
        root = "/data/media/movies"
        targeted, refresh = self._owned_notifications("radarr", jellyfin_key)
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[targeted, refresh]],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).configure(
            "Fixture HD", root, jellyfin_api_key=jellyfin_key,
        )
        self.assertFalse(result["targeted_connection_changed"])
        self.assertFalse(result["refresh_connection_changed"])
        self.assertTrue(all(request.method == "GET" for request in transport.requests))
        inspected = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[targeted, refresh]],
        })).inspect("Fixture HD", root)
        self.assertTrue(inspected["targeted_connection_exact"])
        self.assertTrue(inspected["refresh_connection_exact"])
        self.assertTrue(inspected["targeted_connection"]["events_exact"])
        self.assertTrue(inspected["targeted_connection"]["address_exact"])
        self.assertTrue(inspected["targeted_connection"]["update_library"])
        self.assertFalse(inspected["targeted_connection"]["notify"])
        self.assertTrue(inspected["refresh_connection"]["url_exact"])
        self.assertTrue(inspected["refresh_connection"]["method_exact"])
        self.assertTrue(inspected["refresh_connection"]["token_header"])
        rendered = json.dumps(inspected)
        self.assertNotIn(jellyfin_key, rendered)
        self.assertNotIn("apiKey", rendered)

    def test_duplicate_or_conflicting_owned_connections_fail_without_writes(self):
        jellyfin_key = "JELLYFIN_DEDICATED_KEY_NOT_REAL"
        root = "/data/media/movies"
        targeted, refresh = self._owned_notifications("radarr", jellyfin_key)
        conflicting = dict(targeted)
        conflicting["fields"] = [dict(field) for field in targeted["fields"]]
        for field in conflicting["fields"]:
            if field["name"] == "notify":
                field["value"] = True
        duplicate = dict(targeted)
        duplicate["id"] = 23
        cases = (
            [targeted, dict(targeted, id=23), refresh],
            [targeted, refresh, dict(refresh, id=24)],
            [conflicting, refresh],
            [targeted, dict(refresh, fields=[{"name": "url", "value": "http://jellyfin:8096/Library/Refresh"}, {"name": "method", "value": 2}, {"name": "headers", "value": [{"key": "X-Emby-Token", "value": jellyfin_key}]}])],
            [dict(refresh, fields=[{"name": "url", "value": "http://jellyfin:8096/Library/Refresh"}, {"name": "method", "value": 2}, {"name": "headers", "value": [{"key": "X-Emby-Token", "value": jellyfin_key}]}])],
        )
        for existing in cases:
            with self.subTest(existing=[item.get("id") for item in existing]):
                table = {
                    ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
                    ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
                    ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
                    ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
                    ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
                    ("GET", "/api/v3/notification"): [existing],
                    ("POST", "/api/v3/notification"): [{"id": 99, "implementation": "MediaBrowser"}],
                    ("PUT", "/api/v3/notification/21"): [{}],
                }
                transport = RouteTransport(table)
                with self.assertRaises(ApiError) as raised:
                    ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).configure(
                        "Fixture HD", root, jellyfin_api_key=jellyfin_key,
                    )
                self.assertEqual(raised.exception.code, "notification_conflict")
                self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_notification_create_resumes_after_transport_error(self):
        jellyfin_key = "JELLYFIN_DEDICATED_KEY_NOT_REAL"
        root = "/data/media/movies"
        targeted, refresh = self._owned_notifications("radarr", jellyfin_key)
        created = []

        class ResumeTransport:
            def __init__(self):
                self.requests = []
            def __call__(self, outgoing, timeout):
                self.requests.append(outgoing)
                path = outgoing.full_url.split("127.0.0.1", 1)[1]
                if outgoing.method == "GET" and path == "/api/v3/qualityprofile":
                    return HttpResponse(200, json.dumps([{"id": 19, "name": "Fixture HD"}]).encode())
                if outgoing.method == "GET" and path == "/api/v3/rootfolder":
                    return HttpResponse(200, json.dumps([{"id": 4, "path": root}]).encode())
                if outgoing.method == "GET" and path.startswith("/api/v3/config/"):
                    payload = {"id": 1, "renameMovies": True, "copyUsingHardlinks": True, "enableCompletedDownloadHandling": True}
                    return HttpResponse(200, json.dumps(payload).encode())
                if outgoing.method == "GET" and path == "/api/v3/notification":
                    return HttpResponse(200, json.dumps(list(created)).encode())
                if outgoing.method == "POST" and path == "/api/v3/notification":
                    body = json.loads(outgoing.data)
                    item = targeted if body["implementation"] == "MediaBrowser" else refresh
                    created.append(item)
                    raise OSError("bounded notification fixture interruption")
                raise AssertionError(f"unexpected {outgoing.method} {path}")

        transport = ResumeTransport()
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).configure(
            "Fixture HD", root, jellyfin_api_key=jellyfin_key,
        )
        self.assertTrue(result["targeted_connection_changed"])
        self.assertTrue(result["refresh_connection_changed"])
        self.assertEqual([item["implementation"] for item in created], ["MediaBrowser", "Webhook"])
        self.assertTrue(all(request.method != "PUT" for request in transport.requests))

    def test_unowned_notifications_are_preserved_and_deadline_fails_closed(self):
        jellyfin_key = "JELLYFIN_DEDICATED_KEY_NOT_REAL"
        root = "/data/media/movies"
        targeted, refresh = self._owned_notifications("radarr", jellyfin_key)
        other = {
            "id": 7,
            "implementation": "CustomScript",
            "name": "operator script",
            "fields": [{"name": "path", "value": "/usr/local/bin/notify"}],
        }
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[other, targeted, refresh]],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).configure(
            "Fixture HD", root, jellyfin_api_key=jellyfin_key,
        )
        self.assertFalse(result["targeted_connection_changed"])
        self.assertFalse(result["refresh_connection_changed"])
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

        clock = lambda: 10.0
        exhausted = ArrClient(
            "radarr", "http://127.0.0.1", FIXTURE_KEY,
            transport=RouteTransport({("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]]}),
            deadline=10.0, clock=clock,
        )
        with self.assertRaises(ApiError) as raised:
            exhausted.inspect("Fixture HD", root)
        self.assertEqual(raised.exception.code, "deadline_exhausted")

    def test_equivalent_configuration_makes_no_writes(self):
        root = "/data/media/movies"
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).configure("Fixture HD", root)
        self.assertFalse(result["media_management_changed"])
        self.assertFalse(result["naming_changed"])
        self.assertFalse(result["completed_handling_changed"])
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_inspection_is_get_only_and_checks_exact_root_and_owned_settings(self):
        root = "/data/media/movies"
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[]],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).inspect("Fixture HD", root)
        self.assertTrue(all(result[name] for name in ("profile_exact", "root_exact", "media_settings", "completed_handling")))
        self.assertFalse(result["targeted_connection_exact"])
        self.assertFalse(result["refresh_connection_exact"])
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_inspect_reports_missing_discovery_without_returning_credentials(self):
        root = "/data/media/movies"
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[]],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).inspect("Fixture HD", root)
        self.assertFalse(result["targeted_connection_exact"])
        self.assertFalse(result["refresh_connection_exact"])
        targeted = result["targeted_connection"]
        refresh = result["refresh_connection"]
        self.assertFalse(targeted["present"])
        self.assertFalse(targeted["events_exact"])
        self.assertFalse(targeted["address_exact"])
        self.assertFalse(targeted["update_library"])
        self.assertFalse(refresh["present"])
        self.assertFalse(refresh["events_exact"])
        self.assertFalse(refresh["url_exact"])
        self.assertFalse(refresh["method_exact"])
        self.assertFalse(refresh["token_header"])
        rendered = json.dumps(result)
        self.assertNotIn(FIXTURE_KEY, rendered)
        self.assertNotIn("apiKey", rendered)
        self.assertNotIn("X-Emby-Token", rendered)
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_rename_left_off_in_naming_is_reported_unconfigured(self):
        """Hardlinks on but renaming off must not pass inspection."""
        root = "/data/media/movies"
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": False}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[]],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).inspect("Fixture HD", root)
        self.assertFalse(result["media_settings"])

    def test_targeted_test_can_pass_while_new_title_discovery_is_absent(self):
        test_notify = json.loads((FIXTURES / "mediabrowser-test-notify.json").read_text(encoding="utf-8"))
        unknown = json.loads((FIXTURES / "jellyfin-items-unknown-title.json").read_text(encoding="utf-8"))
        updated = json.loads((FIXTURES / "jellyfin-library-media-updated.json").read_text(encoding="utf-8"))
        refresh = json.loads((FIXTURES / "jellyfin-library-refresh.json").read_text(encoding="utf-8"))
        self.assertEqual(test_notify["path"], "/Notifications/Admin")
        self.assertNotEqual(test_notify["path"], "/Library/Refresh")
        self.assertNotEqual(test_notify["path"], "/Library/Media/Updated")
        self.assertEqual(unknown["Items"], [])
        self.assertFalse(unknown["skips_update"])
        self.assertEqual(updated["path"], "/Library/Media/Updated")
        self.assertTrue(updated["query"]["path"])
        self.assertTrue(updated["path_targeted"])
        self.assertFalse(updated["full_library_scan"])
        self.assertNotEqual(updated["path"], refresh["path"])
        self.assertEqual(refresh["path"], "/Library/Refresh")
        self.assertTrue(refresh["full_library_scan"])
        self.assertIn("ValidateMediaLibrary", refresh["comment"])
        self.assertEqual(refresh["headers"]["X-Emby-Token"], "REDACTED")
        self.assertFalse(refresh["token_in_url"])

        jellyfin_key = "JELLYFIN_DEDICATED_KEY_NOT_REAL"
        root = "/data/media/movies"
        targeted, _refresh = self._owned_notifications("radarr", jellyfin_key)
        targeted_only = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[targeted]],
        })
        before = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=targeted_only).inspect("Fixture HD", root)
        self.assertTrue(before["targeted_connection_exact"])
        self.assertFalse(before["refresh_connection_exact"])
        self.assertNotIn(jellyfin_key, json.dumps(before))

        table = routes("radarr", root)
        table[("GET", "/api/v3/qualityprofile")] = [[{"id": 19, "name": "Fixture HD"}]]
        table[("GET", "/api/v3/rootfolder")] = [[{"id": 4, "path": root}]]
        table[("GET", "/api/v3/config/naming")] = [{"id": 1, "renameMovies": True}]
        table[("GET", "/api/v3/config/mediamanagement")] = [{"id": 1, "copyUsingHardlinks": True}]
        table[("GET", "/api/v3/config/downloadclient")] = [{"id": 2, "enableCompletedDownloadHandling": True}]
        table[("GET", "/api/v3/notification")] = [[targeted], [targeted, _refresh]]
        table[("POST", "/api/v3/notification")] = [{"id": 22, "implementation": "Webhook"}]
        after_configure = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=RouteTransport(table)).configure(
            "Fixture HD", root, jellyfin_api_key=jellyfin_key,
        )
        self.assertFalse(after_configure["targeted_connection_changed"])
        self.assertTrue(after_configure["refresh_connection_changed"])
        inspected = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/naming"): [{"id": 1, "renameMovies": True}],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
            ("GET", "/api/v3/notification"): [[targeted, _refresh]],
        })).inspect("Fixture HD", root)
        self.assertTrue(inspected["targeted_connection_exact"])
        self.assertTrue(inspected["refresh_connection_exact"])
        self.assertEqual(inspected["refresh_connection"]["url_exact"], True)
        self.assertNotIn(jellyfin_key, json.dumps(inspected))
        self.assertNotIn(jellyfin_key, json.dumps(after_configure))

    def test_missing_profile_fails_safely_without_hardcoded_id(self):
        transport = RouteTransport({("GET", "/api/v3/qualityprofile"): [[{"id": 6, "name": "Unrelated"}]]})
        with self.assertRaises(ApiError) as raised:
            ArrClient("sonarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).profile("Fixture HD")
        self.assertEqual(raised.exception.code, "profile_not_found")
        self.assertNotIn("6", str(raised.exception))

    def test_creates_one_qbittorrent_client_at_gluetun_and_rejects_direct_hosts(self) -> None:
        created = []

        def routes_for(existing=None):
            table = {
                ("GET", "/api/v3/downloadclient"): [existing if existing is not None else []],
                ("POST", "/api/v3/downloadclient"): [{"id": 8, "implementation": "QBittorrent"}],
                ("PUT", "/api/v3/downloadclient/3"): [{}],
            }
            return table

        transport = RouteTransport(routes_for())
        client = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport)
        changed = client.ensure_qbittorrent_client(
            host="gluetun", port=6969, username="admin", password="secret-pass"
        )
        self.assertTrue(changed)
        posted = json.loads(transport.requests[-1].data.decode())
        fields = {item["name"]: item["value"] for item in posted["fields"]}
        self.assertEqual(fields["host"], "gluetun")
        self.assertEqual(fields["port"], 6969)
        self.assertEqual(fields["movieCategory"], "movies")
        self.assertFalse(posted["removeCompletedDownloads"])
        created.append(posted)

        for host in ("localhost", "127.0.0.1", "::1", "qbittorrent"):
            with self.subTest(host=host):
                with self.assertRaises(ApiError) as raised:
                    ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=RouteTransport(routes_for())).ensure_qbittorrent_client(
                        host=host, port=6969, username="admin", password="secret-pass"
                    )
                self.assertEqual(raised.exception.code, "invalid_download_client_host")

        existing = [{
            "id": 3,
            "implementation": "QBittorrent",
            "fields": [
                {"name": "host", "value": "localhost"},
                {"name": "port", "value": 8080},
                {"name": "movieCategory", "value": "radarr"},
                {"name": "username", "value": "admin"},
            ],
        }]
        repair = RouteTransport({
            ("GET", "/api/v3/downloadclient"): [existing, existing],
            ("PUT", "/api/v3/downloadclient/3"): [{}],
        })
        repaired = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=repair).ensure_qbittorrent_client(
            host="gluetun", port=6969, username="admin", password="secret-pass"
        )
        self.assertTrue(repaired)
        payload = json.loads(repair.requests[-1].data.decode())
        repaired_fields = {item["name"]: item["value"] for item in payload["fields"]}
        self.assertEqual(repaired_fields["host"], "gluetun")
        self.assertEqual(repaired_fields["movieCategory"], "movies")

        already = [{
            "id": 4,
            "implementation": "QBittorrent",
            "fields": [
                {"name": "host", "value": "gluetun"},
                {"name": "port", "value": 6969},
                {"name": "movieCategory", "value": "movies"},
                {"name": "username", "value": "admin"},
                {"name": "password", "value": "old-pass"},
            ],
        }]
        rotated = RouteTransport({
            ("GET", "/api/v3/downloadclient"): [already, already],
            ("PUT", "/api/v3/downloadclient/4"): [{}],
        })
        rewritten = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=rotated).ensure_qbittorrent_client(
            host="gluetun", port=6969, username="admin", password="new-pass", force_password=True
        )
        self.assertTrue(rewritten)
        rotated_fields = {item["name"]: item["value"] for item in json.loads(rotated.requests[-1].data.decode())["fields"]}
        self.assertEqual(rotated_fields["password"], "new-pass")

        conflict = RouteTransport({
            ("GET", "/api/v3/downloadclient"): [[
                {"id": 1, "implementation": "QBittorrent", "fields": []},
                {"id": 2, "implementation": "QBittorrent", "fields": []},
            ]],
        })
        with self.assertRaises(ApiError) as raised:
            ArrClient("sonarr", "http://127.0.0.1", FIXTURE_KEY, transport=conflict).ensure_qbittorrent_client(
                host="gluetun", port=6969, username="admin", password="secret-pass"
            )
        self.assertEqual(raised.exception.code, "download_client_conflict")
        inspected = ArrClient(
            "radarr",
            "http://127.0.0.1",
            FIXTURE_KEY,
            transport=RouteTransport({
                ("GET", "/api/v3/downloadclient"): [[{
                    "id": 8,
                    "implementation": "QBittorrent",
                    "fields": [
                        {"name": "host", "value": "gluetun"},
                        {"name": "port", "value": 6969},
                        {"name": "movieCategory", "value": "movies"},
                        {"name": "password", "value": "secret-pass"},
                    ],
                }]],
            }),
        ).inspect_download_client(host="gluetun", port=6969)
        self.assertTrue(inspected["exact"])
        self.assertNotIn("secret-pass", json.dumps(inspected))
        self.assertNotIn("secret-pass", str(raised.exception))

    def test_creates_one_nzbget_client_at_gluetun_and_rejects_direct_hosts(self) -> None:
        transport = RouteTransport({
            ("GET", "/api/v3/downloadclient"): [[]],
            ("POST", "/api/v3/downloadclient"): [{"id": 9, "implementation": "Nzbget"}],
        })
        client = ArrClient("sonarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport)
        changed = client.ensure_nzbget_client(
            host="gluetun", port=6789, username="nzbget", password="nzb-secret"
        )
        self.assertTrue(changed)
        posted = json.loads(transport.requests[-1].data.decode())
        fields = {item["name"]: item["value"] for item in posted["fields"]}
        self.assertEqual(posted["implementation"], "Nzbget")
        self.assertEqual(posted["configContract"], "NzbgetSettings")
        self.assertEqual(fields["host"], "gluetun")
        self.assertEqual(fields["port"], 6789)
        self.assertEqual(fields["tvCategory"], "tv")
        self.assertFalse(posted["removeCompletedDownloads"])

        for host in ("localhost", "127.0.0.1", "::1", "nzbget", "0.0.0.0", "qbittorrent"):
            with self.subTest(host=host):
                with self.assertRaises(ApiError) as raised:
                    ArrClient(
                        "radarr",
                        "http://127.0.0.1",
                        FIXTURE_KEY,
                        transport=RouteTransport({("GET", "/api/v3/downloadclient"): [[]]}),
                    ).ensure_nzbget_client(host=host, port=6789, username="nzbget", password="nzb-secret")
                self.assertEqual(raised.exception.code, "invalid_download_client_host")

        already = [{
            "id": 5,
            "implementation": "Nzbget",
            "fields": [
                {"name": "host", "value": "gluetun"},
                {"name": "port", "value": 6789},
                {"name": "tvCategory", "value": "tv"},
                {"name": "username", "value": "nzbget"},
            ],
        }]
        noop = RouteTransport({("GET", "/api/v3/downloadclient"): [already, already]})
        rewritten = ArrClient("sonarr", "http://127.0.0.1", FIXTURE_KEY, transport=noop).ensure_nzbget_client(
            host="gluetun", port=6789, username="nzbget", password="nzb-secret"
        )
        self.assertFalse(rewritten)
        self.assertFalse(any(request.method == "POST" for request in noop.requests))
        inspected = ArrClient(
            "sonarr",
            "http://127.0.0.1",
            FIXTURE_KEY,
            transport=RouteTransport({("GET", "/api/v3/downloadclient"): [already]}),
        ).inspect_download_client(host="gluetun", port=6789, implementation="Nzbget")
        self.assertTrue(inspected["exact"])
        self.assertNotIn("nzb-secret", json.dumps(inspected))


if __name__ == "__main__":
    unittest.main()
