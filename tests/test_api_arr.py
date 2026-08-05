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
        ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, rename: False, "copyUsingHardlinks": False, "unowned": "keep"}],
        ("PUT", "/api/v3/config/mediamanagement"): [{}],
        ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": False, "unowned": 7}],
        ("PUT", "/api/v3/config/downloadclient"): [{}],
    }


class ArrApiTests(unittest.TestCase):
    def test_reads_strict_protected_xml_and_rejects_symlink_world_readable_and_bad_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.xml"
            config.write_text(f"<Config><ApiKey>{FIXTURE_KEY}</ApiKey></Config>", encoding="utf-8")
            config.chmod(0o600)
            self.assertEqual(read_api_key(config), FIXTURE_KEY)
            link = root / "link.xml"
            link.symlink_to(config)
            with self.assertRaises(ValueError):
                read_api_key(link)
            config.chmod(0o604)
            with self.assertRaises(ValueError):
                read_api_key(config)
            config.chmod(0o600)
            config.write_text("<Config><ApiKey>short</ApiKey></Config>", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_api_key(config)

    def test_discovers_profile_by_name_creates_roots_and_preserves_unowned_config(self):
        for service, root in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
            with self.subTest(service=service):
                transport = RouteTransport(routes(service, root))
                client = ArrClient(service, "http://127.0.0.1", FIXTURE_KEY, transport=transport)
                result = client.configure("Fixture HD", root)
                self.assertEqual(result["profile"], "Fixture HD")
                self.assertEqual(result["root"], root)
                puts = [json.loads(request.data) for request in transport.requests if request.method == "PUT"]
                self.assertEqual(puts[0]["unowned"], "keep")
                self.assertTrue(puts[0]["copyUsingHardlinks"])
                self.assertTrue(puts[0]["renameMovies" if service == "radarr" else "renameEpisodes"])
                self.assertEqual(puts[1]["unowned"], 7)
                self.assertTrue(puts[1]["enableCompletedDownloadHandling"])
                self.assertTrue(all("apikey=" not in request.full_url.casefold() for request in transport.requests))
                self.assertTrue(all(request.headers.get("X-api-key") == FIXTURE_KEY for request in transport.requests))

    def test_equivalent_configuration_makes_no_writes(self):
        root = "/data/media/movies"
        transport = RouteTransport({
            ("GET", "/api/v3/qualityprofile"): [[{"id": 19, "name": "Fixture HD"}]],
            ("GET", "/api/v3/rootfolder"): [[{"id": 4, "path": root}]],
            ("GET", "/api/v3/config/mediamanagement"): [{"id": 1, "renameMovies": True, "copyUsingHardlinks": True}],
            ("GET", "/api/v3/config/downloadclient"): [{"id": 2, "enableCompletedDownloadHandling": True}],
        })
        result = ArrClient("radarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).configure("Fixture HD", root)
        self.assertFalse(result["media_management_changed"])
        self.assertFalse(result["completed_handling_changed"])
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_missing_profile_fails_safely_without_hardcoded_id(self):
        transport = RouteTransport({("GET", "/api/v3/qualityprofile"): [[{"id": 6, "name": "Unrelated"}]]})
        with self.assertRaises(ApiError) as raised:
            ArrClient("sonarr", "http://127.0.0.1", FIXTURE_KEY, transport=transport).profile("Fixture HD")
        self.assertEqual(raised.exception.code, "profile_not_found")
        self.assertNotIn("6", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
