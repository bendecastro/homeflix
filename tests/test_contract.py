from __future__ import annotations

import copy
import inspect
import unittest
from typing import Mapping
from unittest.mock import patch

from scripts.homeflix_setup import contract as contract_module
from scripts.homeflix_setup.contract import evaluate_stack_contract


HEALTHCHECK = {
    "test": [
        "CMD-SHELL",
        "curl -sS -o /dev/null --connect-timeout 3 --max-time 5 http://127.0.0.1:9999/",
    ],
    "interval": "30s",
    "timeout": "10s",
    "retries": 3,
    "start_period": "1m30s",
}

CORE = ("traefik", "jellyfin", "jellyseerr", "radarr", "sonarr")
ACQUISITION = ("gluetun", "qbittorrent", "nzbget", "prowlarr", "lidarr", "bazarr")
VPN_NAMESPACE = ("qbittorrent", "nzbget", "prowlarr")
ARR_DATA = ("radarr", "sonarr", "lidarr")


def _bind(source: str, target: str, *, create_host_path: bool | None = False) -> dict[str, object]:
    volume: dict[str, object] = {"type": "bind", "source": source, "target": target}
    bind: dict[str, object] = {}
    if create_host_path is not None:
        bind["create_host_path"] = create_host_path
    volume["bind"] = bind
    return volume


def _vpn_service() -> dict[str, object]:
    return {
        "network_mode": "container:gluetun",
        "healthcheck": copy.deepcopy(HEALTHCHECK),
        "labels": {"deunhealth.restart.on.unhealthy": "true"},
        "volumes": [],
    }


def safe_mapping() -> dict[str, object]:
    services: dict[str, dict[str, object]] = {
        "traefik": {
            "command": ["--api.insecure=true", "--api.dashboard=true"],
            "labels": {"traefik.enable": "true"},
            "networks": {"traefik-network": None},
        },
        "jellyfin": {
            "networks": {"traefik-network": None},
            "volumes": [_bind("/srv/homeflix/data/media", "/data/media")],
        },
        "jellyseerr": {"networks": {"traefik-network": None}},
        "radarr": {
            "networks": {"traefik-network": None},
            "volumes": [_bind("/srv/homeflix/data", "/data")],
        },
        "sonarr": {
            "networks": {"traefik-network": None},
            "volumes": [_bind("/srv/homeflix/data", "/data")],
        },
        "lidarr": {
            "networks": {"traefik-network": None},
            "volumes": [_bind("/srv/homeflix/data", "/data")],
        },
        "bazarr": {
            "networks": {"traefik-network": None},
            "volumes": [_bind("/srv/homeflix/data/media", "/data/media")],
        },
        "gluetun": {
            "environment": {
                "FIREWALL_OUTBOUND_SUBNETS": "192.168.1.0/24,172.30.0.0/24",
                "VPN_PASSWORD": "",
                "OPENVPN_PASSWORD": "",
            },
            "labels": {
                "traefik.enable": "true",
                "traefik.http.routers.qbittorrent.rule": "Host(`qbittorrent.homeflix`)",
                "traefik.http.routers.nzbget.rule": "Host(`nzbget.homeflix`)",
                "traefik.http.routers.prowlarr.rule": "Host(`prowlarr.homeflix`)",
            },
            "networks": {"traefik-network": None},
        },
        "qbittorrent": _vpn_service(),
        "nzbget": _vpn_service(),
        "prowlarr": _vpn_service(),
        "glances": {"networks": {"traefik-network": None}},
        "watchtower": {"image": "containrrr/watchtower:latest"},
        "deunhealth": {"network_mode": "none"},
    }
    for name in CORE:
        services[name]["x-homeflix"] = {"phase": "core"}
    for name in ACQUISITION:
        services[name]["x-homeflix"] = {"phase": "acquisition"}
    return {
        "services": services,
        "networks": {
            "traefik-network": {"ipam": {"config": [{"subnet": "172.30.0.0/24"}]}}
        },
        "x-homeflix": {
            "phases": {
                "core": list(CORE),
                "acquisition": list(ACQUISITION),
            }
        },
    }


def _codes(report: Mapping[str, object]) -> list[str]:
    return [str(item["code"]) for item in report["findings"]]  # type: ignore[index]


def _services_for(report: Mapping[str, object], code: str) -> list[str]:
    return [
        str(item["service"])
        for item in report["findings"]  # type: ignore[union-attr]
        if item["code"] == code and item.get("service")
    ]


class SafeContractTests(unittest.TestCase):
    def test_safe_rendered_mapping_has_no_findings(self) -> None:
        report = evaluate_stack_contract(safe_mapping())
        self.assertTrue(report["passed"])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["findings"], [])


class VpnNamespaceTests(unittest.TestCase):
    def test_removing_only_prowlarr_network_mode_is_an_independent_finding(self) -> None:
        mapping = safe_mapping()
        del mapping["services"]["prowlarr"]["network_mode"]
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("vpn_namespace", _codes(report))
        self.assertEqual(_services_for(report, "vpn_namespace"), ["prowlarr"])
        self.assertNotIn("qbittorrent", _services_for(report, "vpn_namespace"))
        self.assertNotIn("nzbget", _services_for(report, "vpn_namespace"))

    def test_each_vpn_sibling_is_scanned_independently(self) -> None:
        for name in VPN_NAMESPACE:
            with self.subTest(service=name):
                mapping = safe_mapping()
                del mapping["services"][name]["network_mode"]
                report = evaluate_stack_contract(mapping)
                self.assertEqual(_services_for(report, "vpn_namespace"), [name])


class ArrDataRootTests(unittest.TestCase):
    def test_splitting_one_arr_data_root_is_an_independent_finding(self) -> None:
        mapping = safe_mapping()
        mapping["services"]["sonarr"]["volumes"] = [
            _bind("/srv/homeflix/data/torrents", "/data/torrents"),
            _bind("/srv/homeflix/data/media", "/data/media"),
        ]
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("arr_data_root", _codes(report))
        self.assertEqual(_services_for(report, "arr_data_root"), ["sonarr"])
        self.assertNotIn("radarr", _services_for(report, "arr_data_root"))
        self.assertNotIn("lidarr", _services_for(report, "arr_data_root"))

    def test_each_arr_sibling_is_scanned_independently(self) -> None:
        for name in ARR_DATA:
            with self.subTest(service=name):
                mapping = safe_mapping()
                mapping["services"][name]["volumes"] = [
                    _bind("/srv/homeflix/data/torrents", "/data/torrents"),
                    _bind("/srv/homeflix/data/media", "/data/media"),
                ]
                report = evaluate_stack_contract(mapping)
                self.assertEqual(_services_for(report, "arr_data_root"), [name])

    def test_omitted_or_true_create_host_path_is_fail_closed(self) -> None:
        for setting in (None, True):
            with self.subTest(create_host_path=setting):
                mapping = safe_mapping()
                mapping["services"]["radarr"]["volumes"] = [
                    _bind("/srv/homeflix/data", "/data", create_host_path=setting)
                ]
                report = evaluate_stack_contract(mapping)
                self.assertFalse(report["passed"])
                self.assertIn("arr_create_host_path", _codes(report))
                self.assertEqual(_services_for(report, "arr_create_host_path"), ["radarr"])
                self.assertNotIn("arr_data_root", _codes(report))

    def test_divergent_arr_data_root_sources_are_independent_findings(self) -> None:
        for name in ARR_DATA:
            with self.subTest(service=name):
                mapping = safe_mapping()
                mapping["services"][name]["volumes"] = [
                    _bind("/srv/homeflix/other-data", "/data"),
                ]
                report = evaluate_stack_contract(mapping)
                self.assertFalse(report["passed"])
                self.assertIn("arr_data_root", _codes(report))
                self.assertEqual(_services_for(report, "arr_data_root"), [name])


class SelfHealTests(unittest.TestCase):
    def test_removing_healthcheck_xor_deunhealth_label_is_independent(self) -> None:
        cases = (
            ("healthcheck", "self_heal_healthcheck"),
            ("labels", "self_heal_label"),
        )
        for field, code in cases:
            with self.subTest(field=field):
                mapping = safe_mapping()
                del mapping["services"]["nzbget"][field]
                report = evaluate_stack_contract(mapping)
                self.assertFalse(report["passed"])
                self.assertEqual(_services_for(report, code), ["nzbget"])
                other = "self_heal_label" if field == "healthcheck" else "self_heal_healthcheck"
                self.assertNotIn(other, _codes(report))
                self.assertNotIn("qbittorrent", _services_for(report, code))
                self.assertNotIn("prowlarr", _services_for(report, code))


class ProxyRouteTests(unittest.TestCase):
    def test_moving_vpn_router_onto_namespace_service_is_independent(self) -> None:
        mapping = safe_mapping()
        gluetun_labels = mapping["services"]["gluetun"]["labels"]
        moved = gluetun_labels.pop("traefik.http.routers.prowlarr.rule")
        mapping["services"]["prowlarr"].setdefault("labels", {})
        mapping["services"]["prowlarr"]["labels"]["traefik.http.routers.prowlarr.rule"] = moved
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("proxy_route_owner", _codes(report))
        self.assertEqual(_services_for(report, "proxy_route_owner"), ["prowlarr"])
        self.assertNotIn("qbittorrent", _services_for(report, "proxy_route_owner"))
        self.assertNotIn("nzbget", _services_for(report, "proxy_route_owner"))

    def test_deleting_only_the_host_rule_is_an_independent_finding(self) -> None:
        mapping = safe_mapping()
        labels = mapping["services"]["gluetun"]["labels"]
        labels["traefik.http.routers.prowlarr.service"] = "prowlarr"
        del labels["traefik.http.routers.prowlarr.rule"]
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("proxy_route_owner", _codes(report))
        self.assertEqual(_services_for(report, "proxy_route_owner"), ["prowlarr"])
        self.assertNotIn("qbittorrent", _services_for(report, "proxy_route_owner"))
        self.assertNotIn("nzbget", _services_for(report, "proxy_route_owner"))


class ProxySubnetTests(unittest.TestCase):
    def test_missing_proxy_subnet_allowlist_entry_is_independent(self) -> None:
        mapping = safe_mapping()
        mapping["services"]["gluetun"]["environment"]["FIREWALL_OUTBOUND_SUBNETS"] = "192.168.1.0/24"
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("proxy_subnet_allowlist", _codes(report))
        self.assertNotIn("proxy_lan_collapsed", _codes(report))

    def test_collapsed_proxy_and_lan_allowlist_is_independent(self) -> None:
        mapping = safe_mapping()
        mapping["services"]["gluetun"]["environment"]["FIREWALL_OUTBOUND_SUBNETS"] = "10.0.0.0/8"
        mapping["networks"]["traefik-network"]["ipam"]["config"][0]["subnet"] = "10.8.0.0/24"
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("proxy_lan_collapsed", _codes(report))
        self.assertIn("proxy_subnet_allowlist", _codes(report))


class PhaseAllowlistTests(unittest.TestCase):
    def test_phase_allowlist_overlap_is_an_independent_finding(self) -> None:
        mapping = safe_mapping()
        mapping["x-homeflix"]["phases"]["core"].append("prowlarr")
        mapping["services"]["prowlarr"]["x-homeflix"] = {"phase": "core"}
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("phase_overlap", _codes(report))
        self.assertEqual(_services_for(report, "phase_overlap"), ["prowlarr"])

    def test_missing_phase_on_classified_service_is_a_finding(self) -> None:
        mapping = safe_mapping()
        mapping["x-homeflix"]["phases"]["core"].remove("radarr")
        del mapping["services"]["radarr"]["x-homeflix"]
        report = evaluate_stack_contract(mapping)
        self.assertFalse(report["passed"])
        self.assertIn("phase_missing", _codes(report))
        self.assertEqual(_services_for(report, "phase_missing"), ["radarr"])


class OutOfScopeTests(unittest.TestCase):
    def test_unresolved_optional_and_open_decisions_do_not_fail(self) -> None:
        mapping = safe_mapping()
        mapping["services"]["traefik"]["command"] = ["--api.insecure=true"]
        mapping["services"]["watchtower"]["image"] = "containrrr/watchtower:latest"
        mapping["services"]["gluetun"]["environment"]["VPN_PASSWORD"] = ""
        mapping["services"]["gluetun"]["environment"]["OPENVPN_PASSWORD"] = ""
        mapping["services"]["jellyfin"]["environment"] = {"JELLYFIN_ADMIN_PASSWORD": ""}
        report = evaluate_stack_contract(mapping)
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["passed"])

    def test_optional_service_absence_does_not_fail(self) -> None:
        mapping = safe_mapping()
        for name in ("nzbget", "lidarr", "bazarr", "glances", "watchtower"):
            del mapping["services"][name]
        report = evaluate_stack_contract(mapping)
        self.assertTrue(report["passed"], report["findings"])


SECRETS = {
    "VPN_PASSWORD": "vpn-secret-value",
    "OPENVPN_PASSWORD": "openvpn-secret-value",
    "JELLYFIN_ADMIN_PASSWORD": "jellyfin-secret-value",
}


class FindingSafetyTests(unittest.TestCase):
    def test_findings_are_stable_bounded_and_secret_free(self) -> None:
        mapping = safe_mapping()
        mapping["services"]["gluetun"]["environment"].update(SECRETS)
        mapping["services"]["jellyfin"]["environment"] = {
            "JELLYFIN_ADMIN_PASSWORD": SECRETS["JELLYFIN_ADMIN_PASSWORD"]
        }
        del mapping["services"]["prowlarr"]["network_mode"]
        first = evaluate_stack_contract(mapping)
        second = evaluate_stack_contract(mapping)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first["findings"]), 8)
        rendered = str(first)
        for secret in SECRETS.values():
            self.assertNotIn(secret, rendered)
        for name in ("VPN_PASSWORD", "OPENVPN_PASSWORD", "JELLYFIN_ADMIN_PASSWORD"):
            self.assertNotIn(name, rendered)
        for finding in first["findings"]:
            self.assertLessEqual(set(finding), {"code", "service", "message"})
            self.assertIsInstance(finding["code"], str)
            self.assertIsInstance(finding["message"], str)


class PurityTests(unittest.TestCase):
    def test_module_does_not_invoke_docker_and_accepts_in_memory_mappings(self) -> None:
        source = inspect.getsource(contract_module)
        self.assertNotIn("docker", source)
        self.assertNotIn("subprocess", source)
        with patch("subprocess.run", side_effect=AssertionError("contract must not invoke subprocess")):
            report = evaluate_stack_contract(safe_mapping())
        self.assertTrue(report["passed"])
        self.assertEqual(report["findings"], [])
