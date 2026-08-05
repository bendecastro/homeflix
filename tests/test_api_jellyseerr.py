from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.homeflix_setup.core import configure_core

from scripts.homeflix_setup.api import ApiError, HttpResponse, JellyseerrClient, read_settings_api_key

KEY = "FIXTURE_API_KEY_1234567890ABCDE"
FIXTURES = Path(__file__).parent / "fixtures" / "api"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
    def __call__(self, outgoing, timeout):
        self.requests.append(outgoing)
        status, payload = self.responses.pop(0)
        return HttpResponse(status, json.dumps(payload).encode())


def profile_root(service):
    return {"id": 41, "name": "Fixture HD"}, {"id": 8, "path": "/data/media/movies" if service == "radarr" else "/data/media/tv"}


class JellyseerrApiTests(unittest.TestCase):
    def test_initial_auth_uses_exact_docker_jellyfin_fields(self):
        transport = QueueTransport([(200, fixture("jellyseerr-public-new.json")), (200, {})])
        client = JellyseerrClient(transport=transport)
        self.assertFalse(client.initialized())
        client.authenticate_jellyfin("fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")
        request = transport.requests[1]
        self.assertEqual(request.full_url, "http://127.0.0.1/api/v1/auth/jellyfin")
        payload = json.loads(request.data)
        self.assertEqual(payload, {
            "username": "fixture-admin", "password": "FIXTURE_PASSWORD_NOT_REAL",
            "hostname": "jellyfin", "port": 8096, "urlBase": "", "useSsl": False,
            "email": "", "serverType": 2,
        })

    def test_authorized_jellyfin_connection_must_be_exact_internal_plain_http(self):
        for fixture_name, accepted in (
            ("jellyseerr-jellyfin-correct.json", True),
            ("jellyseerr-jellyfin-wrong.json", False),
            ("jellyseerr-jellyfin-missing.json", False),
        ):
            with self.subTest(fixture=fixture_name):
                transport = QueueTransport([
                    (200, fixture("jellyseerr-public-complete.json")),
                    (200, fixture(fixture_name)),
                ])
                client = JellyseerrClient(transport=transport)
                self.assertTrue(client.initialized())
                client.authorize(KEY)
                if accepted:
                    self.assertTrue(client.verify_jellyfin())
                else:
                    with self.assertRaises(ApiError) as raised:
                        client.verify_jellyfin()
                    self.assertEqual(raised.exception.code, "jellyfin_connection_conflict")
                self.assertEqual(transport.requests[1].full_url, "http://127.0.0.1/api/v1/settings/jellyfin")
                self.assertEqual(transport.requests[1].headers.get("X-api-key"), KEY)

    def test_jellyfin_connection_rejects_each_owned_field_conflict(self):
        correct = fixture("jellyseerr-jellyfin-correct.json")
        for field, value in (("hostname", "external.invalid"), ("port", 443), ("useSsl", True), ("urlBase", "/proxy"), ("serverType", 1)):
            with self.subTest(field=field):
                current = dict(correct); current[field] = value
                client = JellyseerrClient(transport=QueueTransport([(200, current)]))
                client.authorize(KEY)
                with self.assertRaises(ApiError) as raised:
                    client.verify_jellyfin()
                self.assertEqual(raised.exception.code, "jellyfin_connection_conflict")

    def test_arr_tests_and_defaults_use_internal_services_and_required_flags(self):
        transport = QueueTransport([(200, {"success": True}), (200, []), (200, {}), (200, {"success": True}), (200, []), (200, {})])
        client = JellyseerrClient(transport=transport)
        client.authorize(KEY)
        for service in ("radarr", "sonarr"):
            profile, root = profile_root(service)
            self.assertTrue(client.ensure_arr(service, KEY, profile, root))
        posts = [request for request in transport.requests if request.method == "POST"]
        self.assertEqual([request.full_url for request in posts], [
            "http://127.0.0.1/api/v1/settings/radarr/test",
            "http://127.0.0.1/api/v1/settings/radarr",
            "http://127.0.0.1/api/v1/settings/sonarr/test",
            "http://127.0.0.1/api/v1/settings/sonarr",
        ])
        radarr = json.loads(posts[1].data)
        sonarr = json.loads(posts[3].data)
        for service, payload, port in (("radarr", radarr, 7878), ("sonarr", sonarr, 8989)):
            self.assertEqual(payload["hostname"], service)
            self.assertEqual(payload["port"], port)
            self.assertFalse(payload["useSsl"])
            self.assertFalse(payload["is4k"])
            self.assertEqual(payload["minimumAvailability"], "released")
            self.assertTrue(payload["isDefault"])
            self.assertTrue(payload["syncEnabled"])
            self.assertFalse(payload["preventSearch"])
            self.assertEqual(payload["activeProfileName"], "Fixture HD")
        self.assertTrue(sonarr["enableSeasonFolders"])
        self.assertTrue(all(request.headers.get("X-api-key") == KEY for request in transport.requests))
        self.assertTrue(all("apikey=" not in request.full_url.casefold() for request in transport.requests))

    def test_equivalent_servers_and_initialized_state_are_rerun_noops(self):
        profile, root = profile_root("radarr")
        seed = JellyseerrClient()
        desired = seed._payload("radarr", KEY, profile, root)
        desired["id"] = 3
        transport = QueueTransport([(200, {"success": True}), (200, [desired]), (200, fixture("jellyseerr-public-complete.json"))])
        client = JellyseerrClient(transport=transport)
        client.authorize(KEY)
        self.assertFalse(client.ensure_arr("radarr", KEY, profile, root))
        self.assertFalse(client.finish())
        self.assertFalse(any(request.method == "PUT" for request in transport.requests))

    def test_owned_drift_updates_existing_server_and_multiple_defaults_refuse(self):
        profile, root = profile_root("sonarr")
        existing = JellyseerrClient()._payload("sonarr", KEY, profile, root)
        existing.update({"id": 9, "syncEnabled": False, "unowned": "keep"})
        transport = QueueTransport([(200, {"success": True}), (200, [existing]), (200, {})])
        client = JellyseerrClient(transport=transport)
        client.authorize(KEY)
        self.assertTrue(client.ensure_arr("sonarr", KEY, profile, root))
        updated = json.loads(transport.requests[-1].data)
        self.assertTrue(updated["syncEnabled"])
        self.assertEqual(updated["unowned"], "keep")

        transport = QueueTransport([(200, {"success": True}), (200, [{"id": 1, "isDefault": True}, {"id": 2, "isDefault": True}])])
        client = JellyseerrClient(transport=transport)
        client.authorize(KEY)
        with self.assertRaises(ApiError) as raised:
            client.ensure_arr("radarr", KEY, *profile_root("radarr"))
        self.assertEqual(raised.exception.code, "multiple_defaults")

    def test_settings_key_reader_accepts_0644_and_rejects_unsafe_traversal_permissions_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            service = root / "jellyseerr"
            service.mkdir(parents=True)
            path = service / "settings.json"
            path.write_text(json.dumps({"main": {"apiKey": KEY}}), encoding="utf-8")
            root.chmod(0o755); service.chmod(0o755); path.chmod(0o644)
            uid = os.getuid()
            self.assertEqual(read_settings_api_key(root, uid), KEY)
            for unsafe in (root, service, path):
                with self.subTest(unsafe=unsafe.name):
                    original = unsafe.stat().st_mode & 0o777
                    unsafe.chmod(original | 0o002)
                    with self.assertRaises(ValueError):
                        read_settings_api_key(root, uid)
                    unsafe.chmod(original)
            with self.assertRaises(ValueError):
                read_settings_api_key(root, uid + 100000)
            moved = service.with_name("real")
            service.rename(moved)
            service.symlink_to(moved, target_is_directory=True)
            with self.assertRaises(ValueError):
                read_settings_api_key(root, uid)


class ConfigureCoreTests(unittest.TestCase):
    def test_uses_effective_env_credentials_runtime_profiles_and_safe_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            (root / ".env").write_text(
                "JELLYFIN_ADMIN_USER=old\nJELLYFIN_ADMIN_USER=fixture-admin\n"
                "JELLYFIN_ADMIN_PASSWORD=FIXTURE_PASSWORD_NOT_REAL\n"
                f"CONFIG_ROOT={config_root}\nPUID=99999\nPUID={os.getuid()}\nQUALITY_PROFILE=Fixture HD\nDOMAIN=fixture.test\n",
                encoding="utf-8",
            )
            (root / ".env").chmod(0o600)
            state = root / ".homeflix" / "setup.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"schema_version": 1, "checkpoints": {"core_containers_started": True}, "host_facts": {}}), encoding="utf-8")
            calls = []
            class FakeJellyfin:
                def __init__(self, **kwargs): pass
                def initialize(self, username, password):
                    calls.append(("jf", username, password)); return True
                def ensure_libraries(self): return ["Movies", "Shows", "Music"]
            class FakeArr:
                def __init__(self, service, base, key, **kwargs):
                    self.service = service; calls.append((service, base, key, kwargs["headers"]))
                def configure(self, name, path):
                    self.selected_profile = {"id": 41, "name": name}
                    self.selected_root = {"id": 8, "path": path}
                    return {"service": self.service, "profile": name, "root": path, "media_management_changed": False, "completed_handling_changed": False}
            class FakeSeerr:
                def __init__(self, **kwargs): calls.append(("seerr-host", kwargs["headers"]))
                def initialized(self): return False
                def authenticate_jellyfin(self, username, password): calls.append(("seerr-auth", username, password))
                def authorize(self, key): calls.append(("seerr-key", key))
                def verify_jellyfin(self): calls.append(("verify-jellyfin",)); return True
                def ensure_arr(self, service, key, profile, media_root): calls.append(("connect", service, profile["name"], media_root["path"])); return True
                def finish(self): return True
            reader_uids = []
            def arr_key(config_root, service, uid): reader_uids.append(uid); return KEY
            def seerr_key(config_root, uid): reader_uids.append(uid); return KEY
            with patch("scripts.homeflix_setup.core.JellyfinClient", FakeJellyfin), patch("scripts.homeflix_setup.core.ArrClient", FakeArr), patch("scripts.homeflix_setup.core.JellyseerrClient", FakeSeerr):
                result = configure_core(root, api_key_reader=arr_key, settings_key_reader=seerr_key)
            self.assertEqual(reader_uids, [os.getuid()] * 3)
            self.assertEqual(calls[0][1], "fixture-admin")
            self.assertEqual(calls[1][1], "http://127.0.0.1")
            self.assertEqual(calls[1][3], {"Host": "radarr.fixture.test"})
            self.assertLess(calls.index(("seerr-auth", "fixture-admin", "FIXTURE_PASSWORD_NOT_REAL")), calls.index(("seerr-key", KEY)))
            self.assertLess(calls.index(("seerr-key", KEY)), calls.index(("verify-jellyfin",)))
            self.assertIn(("connect", "sonarr", "Fixture HD", "/data/media/tv"), calls)
            rendered = json.dumps(result)
            self.assertNotIn(KEY, rendered)
            self.assertNotIn("FIXTURE_PASSWORD", rendered)
            self.assertEqual(result["status"], "configured")

    def test_missing_credentials_fail_before_client_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("CONFIG_ROOT=/tmp/fixture\nQUALITY_PROFILE=Fixture HD\nDOMAIN=fixture.test\n", encoding="utf-8")
            (root / ".env").chmod(0o600)
            state = root / ".homeflix" / "setup.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"schema_version": 1, "checkpoints": {"core_containers_started": True}, "host_facts": {}}), encoding="utf-8")
            with patch("scripts.homeflix_setup.core.JellyfinClient", side_effect=AssertionError("must not request")):
                with self.assertRaises(ValueError):
                    configure_core(root)


if __name__ == "__main__":
    unittest.main()
