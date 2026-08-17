from __future__ import annotations

import json
import unittest

from urllib.parse import urlsplit

from scripts.homeflix_setup.api import ApiError, HttpResponse
from scripts.homeflix_setup.api.prowlarr import ProwlarrClient


PROWLARR_KEY = "PROWLARRKEY1234567890ABCD"
RADARR_KEY = "RADARRKEY1234567890ABCDEF"
SONARR_KEY = "SONARRKEY1234567890ABCDEF"


class ProwlarrTransport:
    def __init__(self, applications=None, indexers=None) -> None:
        self.applications = list(applications or [])
        self.indexers = list(indexers or [])
        self.next_id = 1 + max((item.get("id", 0) for item in self.applications), default=0)
        self.requests = []

    def __call__(self, outgoing, timeout):
        self.requests.append(outgoing)
        path = urlsplit(outgoing.full_url).path
        if outgoing.method == "GET" and path == "/api/v1/applications":
            return HttpResponse(200, json.dumps(self.applications).encode())
        if outgoing.method == "GET" and path == "/api/v1/indexer":
            return HttpResponse(200, json.dumps(self.indexers).encode())
        if outgoing.method == "POST" and path == "/api/v1/applications":
            payload = json.loads(outgoing.data.decode())
            payload["id"] = self.next_id
            self.next_id += 1
            self.applications.append(payload)
            return HttpResponse(200, json.dumps(payload).encode())
        if outgoing.method == "PUT" and path.startswith("/api/v1/applications/"):
            ident = int(path.rsplit("/", 1)[-1])
            payload = json.loads(outgoing.data.decode())
            payload["id"] = ident
            self.applications = [payload if item.get("id") == ident else item for item in self.applications]
            return HttpResponse(200, json.dumps(payload).encode())
        raise AssertionError(path)


def field(name, value):
    return {"name": name, "value": value}


class ProwlarrApiTests(unittest.TestCase):
    def test_creates_exactly_one_radarr_and_sonarr_app_via_service_addresses(self) -> None:
        transport = ProwlarrTransport()
        client = ProwlarrClient("http://127.0.0.1:9696", PROWLARR_KEY, transport=transport)
        result = client.ensure_applications(
            prowlarr_port=9696,
            arr_keys={"radarr": RADARR_KEY, "sonarr": SONARR_KEY},
        )
        rerun = client.ensure_applications(
            prowlarr_port=9696,
            arr_keys={"radarr": RADARR_KEY, "sonarr": SONARR_KEY},
        )
        rendered = json.dumps(result) + json.dumps(rerun)
        implementations = [item["implementation"] for item in transport.applications]
        self.assertEqual(sorted(implementations), ["Radarr", "Sonarr"])
        self.assertTrue(result["radarr_application"])
        self.assertTrue(result["sonarr_application"])
        self.assertTrue(result["radarr_changed"])
        self.assertFalse(rerun["radarr_changed"])
        self.assertFalse(result["indexer_credentials"])
        self.assertIn("provider/indexer credentials required", result["indexer_reason"])
        self.assertNotIn(PROWLARR_KEY, rendered)
        self.assertNotIn(RADARR_KEY, rendered)
        self.assertNotIn(SONARR_KEY, rendered)
        for item in transport.applications:
            fields = {field["name"]: field["value"] for field in item["fields"]}
            self.assertTrue(fields["prowlarrUrl"].startswith("http://gluetun:"))
            self.assertIn(fields["baseUrl"], {"http://radarr:7878", "http://sonarr:8989"})
            self.assertNotIn("localhost", fields["prowlarrUrl"])
            self.assertNotIn("prowlarr", fields["prowlarrUrl"])

    def test_repairs_drift_and_fails_on_duplicate_applications(self) -> None:
        drifted = {
            "id": 4,
            "implementation": "Radarr",
            "configContract": "RadarrSettings",
            "syncLevel": "fullSync",
            "fields": [
                field("prowlarrUrl", "http://localhost:9696"),
                field("baseUrl", "http://127.0.0.1:7878"),
                field("apiKey", RADARR_KEY),
            ],
        }
        sonarr = {
            "id": 5,
            "implementation": "Sonarr",
            "configContract": "SonarrSettings",
            "syncLevel": "fullSync",
            "fields": [
                field("prowlarrUrl", "http://gluetun:9696"),
                field("baseUrl", "http://sonarr:8989"),
                field("apiKey", SONARR_KEY),
            ],
        }
        transport = ProwlarrTransport(applications=[drifted, sonarr])
        client = ProwlarrClient("http://127.0.0.1:9696", PROWLARR_KEY, transport=transport)
        result = client.ensure_applications(
            prowlarr_port=9696,
            arr_keys={"radarr": RADARR_KEY, "sonarr": SONARR_KEY},
        )
        self.assertTrue(result["radarr_changed"])
        self.assertTrue(result["radarr_application"])
        fields = {item["name"]: item["value"] for item in transport.applications[0]["fields"]}
        self.assertEqual(fields["prowlarrUrl"], "http://gluetun:9696")
        self.assertEqual(fields["baseUrl"], "http://radarr:7878")

        transport.applications.append(dict(transport.applications[0], id=9))
        with self.assertRaises(ApiError) as raised:
            client.ensure_applications(
                prowlarr_port=9696,
                arr_keys={"radarr": RADARR_KEY, "sonarr": SONARR_KEY},
            )
        self.assertEqual(raised.exception.code, "application_conflict")

    def test_reports_usable_indexer_without_inventing_credentials(self) -> None:
        transport = ProwlarrTransport(indexers=[{"id": 1, "enable": True, "name": "fixture"}])
        client = ProwlarrClient("http://127.0.0.1:9696", PROWLARR_KEY, transport=transport)
        inspected = client.inspect(prowlarr_port=9696)
        self.assertTrue(inspected["indexer_credentials"])
        self.assertFalse(any(outgoing.method == "POST" for outgoing in transport.requests))
        self.assertNotIn(PROWLARR_KEY, json.dumps(inspected))
