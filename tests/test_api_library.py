from __future__ import annotations

import json
import unittest

from scripts.homeflix_setup.api import ApiError, HttpResponse, LibraryClient

FIXTURE_KEY = "FIXTURE_API_KEY_1234567890ABCDE"
PROFILE = 6
MOVIE_ROOT = "/data/media/movies"
TV_ROOT = "/data/media/tv"


class ScriptedTransport:
    """Serves queued responses per (method, path); records every request."""

    def __init__(self, routes):
        self.routes = {key: list(values) for key, values in routes.items()}
        self.requests = []

    def __call__(self, outgoing, timeout):
        path = outgoing.full_url.split("127.0.0.1", 1)[1]
        key = (outgoing.method, path)
        self.requests.append((outgoing.method, path, json.loads(outgoing.data) if outgoing.data else None))
        if key not in self.routes or not self.routes[key]:
            raise AssertionError(f"unexpected request {key}")
        payload = self.routes[key].pop(0)
        return HttpResponse(200, json.dumps(payload).encode())

    def sent(self, method, path):
        return [body for sent_method, sent_path, body in self.requests if (sent_method, sent_path) == (method, path)]


def client(service, transport):
    return LibraryClient(service, "http://127.0.0.1", FIXTURE_KEY, transport=transport, sleep=lambda _: None)


def series_payload(monitored_seasons, *, series_monitored=True):
    return {
        "id": 31, "title": "Fixture Series", "monitored": series_monitored,
        "seasons": [
            {"seasonNumber": number, "monitored": number in monitored_seasons}
            for number in (0, 1, 2)
        ],
    }


class MovieLibraryTests(unittest.TestCase):
    def test_adds_movie_pinned_to_requested_tmdb_id_and_requests_search(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/movie"): [[]],
            ("GET", "/api/v3/movie/lookup/tmdb?tmdbId=1233413"): [
                {"tmdbId": 1233413, "title": "Fixture Movie", "year": 2025, "titleSlug": "fixture-movie"}
            ],
            ("POST", "/api/v3/movie"): [{"id": 77, "title": "Fixture Movie", "tmdbId": 1233413}],
        })
        result = client("radarr", transport).add_movie(
            1233413, quality_profile_id=PROFILE, root_folder_path=MOVIE_ROOT)

        self.assertEqual(result["status"], "added")
        self.assertEqual(result["id"], 77)
        body = transport.sent("POST", "/api/v3/movie")[0]
        self.assertEqual(body["tmdbId"], 1233413)
        self.assertEqual(body["qualityProfileId"], PROFILE)
        self.assertEqual(body["rootFolderPath"], MOVIE_ROOT)
        self.assertTrue(body["monitored"])
        self.assertTrue(body["addOptions"]["searchForMovie"])

    def test_readding_an_existing_movie_is_a_no_op(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/movie"): [[{"id": 5, "tmdbId": 1233413, "title": "Fixture Movie"}]],
        })
        result = client("radarr", transport).add_movie(
            1233413, quality_profile_id=PROFILE, root_folder_path=MOVIE_ROOT)

        self.assertEqual(result["status"], "present")
        self.assertEqual(result["id"], 5)
        self.assertTrue(all(method == "GET" for method, _, _ in transport.requests))

    def test_lookup_returning_a_different_title_is_rejected(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/movie"): [[]],
            ("GET", "/api/v3/movie/lookup/tmdb?tmdbId=1233413"): [{"tmdbId": 999, "title": "Wrong Movie"}],
        })
        with self.assertRaises(ApiError) as raised:
            client("radarr", transport).add_movie(
                1233413, quality_profile_id=PROFILE, root_folder_path=MOVIE_ROOT)

        self.assertEqual(raised.exception.code, "lookup_mismatch")
        self.assertEqual(transport.sent("POST", "/api/v3/movie"), [])

    def test_api_key_travels_in_a_header_not_the_url(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/movie"): [[{"id": 5, "tmdbId": 1233413}]],
        })
        client("radarr", transport).add_movie(
            1233413, quality_profile_id=PROFILE, root_folder_path=MOVIE_ROOT)
        self.assertTrue(all("apikey=" not in path.casefold() for _, path, _ in transport.requests))


class SeriesLibraryTests(unittest.TestCase):
    def test_monitors_only_requested_seasons_and_searches_each_after_settling(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A393189"): [[
                {"tvdbId": 393189, "title": "Fixture Series", "year": 2022,
                 "seasons": [{"seasonNumber": n} for n in (0, 1, 2)]},
            ]],
            ("POST", "/api/v3/series"): [{"id": 31, "title": "Fixture Series"}],
            ("GET", "/api/v3/series/31"): [series_payload([1, 2]), series_payload([1, 2])],
            ("POST", "/api/v3/command"): [{"id": 1}, {"id": 2}],
        })
        result = client("sonarr", transport).add_series(
            393189, [1, 2], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(result["status"], "added")
        self.assertEqual(result["seasons"], [1, 2])
        add = transport.sent("POST", "/api/v3/series")[0]
        self.assertEqual(add["addOptions"], {"monitor": "none", "searchForMissingEpisodes": False})
        self.assertEqual(
            [entry["seasonNumber"] for entry in add["seasons"] if entry["monitored"]], [1, 2])
        self.assertEqual(
            [(body["name"], body["seasonNumber"]) for body in transport.sent("POST", "/api/v3/command")],
            [("SeasonSearch", 1), ("SeasonSearch", 2)])

    def test_reverted_monitoring_is_reasserted_before_any_search(self):
        """Sonarr's deferred refresh unmonitors the series after the add."""
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A393189"): [[
                {"tvdbId": 393189, "title": "Fixture Series",
                 "seasons": [{"seasonNumber": n} for n in (0, 1, 2)]},
            ]],
            ("POST", "/api/v3/series"): [{"id": 31, "title": "Fixture Series"}],
            ("GET", "/api/v3/series/31"): [
                series_payload([], series_monitored=False),   # refresh reverted it
                series_payload([1, 2]),                        # after the corrective PUT
                series_payload([1, 2]),                        # still true a delay later
            ],
            ("PUT", "/api/v3/series/31"): [{}],
            ("POST", "/api/v3/command"): [{"id": 1}, {"id": 2}],
        })
        result = client("sonarr", transport).add_series(
            393189, [1, 2], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(result["seasons"], [1, 2])
        corrected = transport.sent("PUT", "/api/v3/series/31")[0]
        self.assertTrue(corrected["monitored"])
        self.assertEqual(
            [entry["seasonNumber"] for entry in corrected["seasons"] if entry["monitored"]], [1, 2])
        # The corrective PUT must precede every search command.
        order = [(method, path) for method, path, _ in transport.requests]
        self.assertLess(order.index(("PUT", "/api/v3/series/31")), order.index(("POST", "/api/v3/command")))

    def test_a_single_good_read_is_not_trusted_when_it_later_regresses(self):
        """One read straight after the write is not evidence; it can regress."""
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A452467"): [[
                {"tvdbId": 452467, "title": "Fixture Series",
                 "seasons": [{"seasonNumber": n} for n in (0, 1, 2)]},
            ]],
            ("POST", "/api/v3/series"): [{"id": 31, "title": "Fixture Series"}],
            ("GET", "/api/v3/series/31"): [
                series_payload([1]),                          # looks correct...
                series_payload([], series_monitored=False),   # ...then the refresh reverts it
                series_payload([1]),
                series_payload([1]),
            ],
            ("PUT", "/api/v3/series/31"): [{}],
            ("POST", "/api/v3/command"): [{"id": 1}],
        })
        result = client("sonarr", transport).add_series(
            452467, [1], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(result["seasons"], [1])
        self.assertEqual(len(transport.sent("PUT", "/api/v3/series/31")), 1)
        self.assertEqual(len(transport.sent("POST", "/api/v3/command")), 1)

    def test_permanently_unstable_monitoring_fails_without_searching(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A393189"): [[
                {"tvdbId": 393189, "title": "Fixture Series",
                 "seasons": [{"seasonNumber": n} for n in (0, 1, 2)]},
            ]],
            ("POST", "/api/v3/series"): [{"id": 31, "title": "Fixture Series"}],
            ("GET", "/api/v3/series/31"): [series_payload([], series_monitored=False)] * 6,
            ("PUT", "/api/v3/series/31"): [{}] * 6,
        })
        with self.assertRaises(ApiError) as raised:
            client("sonarr", transport).add_series(
                393189, [1, 2], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(raised.exception.code, "monitoring_unstable")
        self.assertEqual(transport.sent("POST", "/api/v3/command"), [])

    def test_exact_title_match_on_a_different_series_is_never_selected(self):
        """A same-titled unrelated series can outrank the intended one."""
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A448147"): [[
                {"tvdbId": 409830, "title": "The Studio", "year": 2021,
                 "seasons": [{"seasonNumber": 1}]},
                {"tvdbId": 448147, "title": "The Studio (2025)", "year": 2025,
                 "seasons": [{"seasonNumber": n} for n in (0, 1, 2)]},
            ]],
            ("POST", "/api/v3/series"): [{"id": 31, "title": "The Studio (2025)"}],
            ("GET", "/api/v3/series/31"): [series_payload([1]), series_payload([1])],
            ("POST", "/api/v3/command"): [{"id": 1}],
        })
        result = client("sonarr", transport).add_series(
            448147, [1], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(result["tvdbId"], 448147)
        self.assertEqual(transport.sent("POST", "/api/v3/series")[0]["tvdbId"], 448147)

    def test_requesting_a_season_the_series_does_not_have_fails_before_adding(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A452467"): [[
                {"tvdbId": 452467, "title": "Fixture Series",
                 "seasons": [{"seasonNumber": n} for n in (0, 1)]},
            ]],
        })
        with self.assertRaises(ApiError) as raised:
            client("sonarr", transport).add_series(
                452467, [1, 4], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(raised.exception.code, "season_not_found:4")
        self.assertEqual(transport.sent("POST", "/api/v3/series"), [])

    def test_readding_an_existing_series_makes_no_writes(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[{
                "id": 9, "tvdbId": 393189, "title": "Fixture Series",
                "seasons": [{"seasonNumber": 1, "monitored": True}],
            }]],
        })
        result = client("sonarr", transport).add_series(
            393189, [1], quality_profile_id=PROFILE, root_folder_path=TV_ROOT)

        self.assertEqual(result["status"], "present")
        self.assertEqual(result["seasons"], [1])
        self.assertTrue(all(method == "GET" for method, _, _ in transport.requests))

    def test_search_can_be_withheld(self):
        transport = ScriptedTransport({
            ("GET", "/api/v3/series"): [[]],
            ("GET", "/api/v3/series/lookup?term=tvdb%3A448176"): [[
                {"tvdbId": 448176, "title": "Fixture Series",
                 "seasons": [{"seasonNumber": n} for n in (0, 1, 2)]},
            ]],
            ("POST", "/api/v3/series"): [{"id": 31, "title": "Fixture Series"}],
            ("GET", "/api/v3/series/31"): [series_payload([1]), series_payload([1])],
        })
        result = client("sonarr", transport).add_series(
            448176, [1], quality_profile_id=PROFILE, root_folder_path=TV_ROOT, search=False)

        self.assertFalse(result["searched"])
        self.assertEqual(transport.sent("POST", "/api/v3/command"), [])

    def test_rejects_empty_and_negative_season_selections(self):
        transport = ScriptedTransport({})
        for seasons in ([], [-1]):
            with self.subTest(seasons=seasons), self.assertRaises(ValueError):
                client("sonarr", transport).add_series(
                    393189, seasons, quality_profile_id=PROFILE, root_folder_path=TV_ROOT)


if __name__ == "__main__":
    unittest.main()
